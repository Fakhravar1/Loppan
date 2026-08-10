"""Sweep one rotation bucket of brands and refresh its slice of the undervalued pool.

    python loppan/sweep_pool.py                 # today's bucket
    python loppan/sweep_pool.py --bucket 2
    python loppan/sweep_pool.py --dry-run

## The idea

You do not have to *keep* a peer group, only to know it long enough to rank against it.
Levels 1 and 2 group on (brand, item_type, condition) and (brand, category) — both
entirely inside a brand — so if a sweep takes **whole brands**, every usable group is
complete while it is in `sweep_staging`. The cheap tail is copied into the pool and the
rest is thrown away.

That is what makes the full market affordable. Storing every item needed to compute the
comparison would be ~560 MB; holding a quarter of it transiently is ~146 MB, and the
pool it leaves behind is a fraction of that.

Buckets come from `crc32(brand) % 4`. No mapping table to maintain, new brands assign
themselves, and it splits the target market 24.4 / 24.4 / 24.7 / 26.6 % — measured, not
assumed. `public.crc32()` in Postgres reproduces `zlib.crc32` exactly (verified on ASCII
and UTF-8 brand names), so the two sides can never drift apart.

## What it does not touch

`items`, and therefore the entire statistical layer. That sample is stratified with known
inclusion probabilities and `brand_daily`, `predictor_daily` and every sell rate rest on
it. This population is deliberately biased — a quarter of the brands, and only sizes that
fit two specific people — so pooling the two would silently turn every market estimate
into "…among things that happen to fit us". `analytics.md` §8.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, db, enrol, search, sizes

# 12, not 4. A bucket is held in `sweep_staging` in full while its peer groups are
# computed, and at 433 bytes a staged row a quarter of the market is ~142 MB against a
# 500 MB tier -- measured 2026-08-10 by running it and watching it climb. Twelve puts
# the peak at ~47 MB, which leaves room for the pool to grow into full coverage.
#
# Raising this does not slow the cycle down if the job runs more than once a day: at
# four runs a day, twelve buckets is a three-day rotation. It also makes each run
# shorter -- ~1,500 brands instead of ~4,500 -- so a failure costs less.
BUCKETS = 12
MIN_PRICE_KR = 200      # the shortlist floor; below it the peer signal inverts
MIN_BRAND_ITEMS = 8     # a group needs 8 to exist, so thinner brands cannot produce one
PER_SHAPE = 1000        # Algolia's hard ceiling per request

# Columns sweep_staging accepts. row_of() builds an `items` row, which is a superset.
STAGING_COLS = {
    "item_id", "brand_id", "category_id", "item_type_id", "size_id", "condition_id",
    "demography_id", "pattern_id", "season_mask", "material_mask", "colour_mask",
    "weight_g", "has_defect", "first_price_ore", "price_ore", "favourites",
    "fav_nordic", "fav_eu", "fav_dach", "first_offered", "is_reserved", "p2p",
    "last_chance",
}


def _arg(flag: str, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def bucket_of(brand: str) -> int:
    """Matches `public.crc32(brand) % 4` in Postgres, verified on ASCII and UTF-8 names.

    Not `hash()`: Python salts string hashing per process, so it would reshuffle every
    brand on every run and the rotation would re-sweep some brands while starving
    others. Not `hashtext()` either — that one cannot be reproduced outside Postgres.
    """
    return zlib.crc32(brand.encode()) % BUCKETS


def next_bucket() -> int:
    """Whichever bucket has gone longest without a sweep.

    Not a formula over the date. The job runs four times a day, so `date % BUCKETS`
    would pick the same bucket every time and never advance — and any date-and-hour
    formula silently skips a bucket whenever a run fails, with nothing to notice.

    Asking the data is self-correcting: a bucket that failed stays the oldest and is
    retried first, and one never swept sorts ahead of everything.
    """
    return int(db.rpc("next_sweep_bucket", {"p_buckets": BUCKETS}))


def brands_in_bucket(bucket: int) -> list[str]:
    rows = db.query("brands?select=name&order=name.asc")
    return [r["name"] for r in rows if r["name"] and bucket_of(r["name"]) == bucket]


# A single Algolia query shape stops paginating at roughly this many results, whatever
# nbHits claims. Hit it and the rest of the shape is simply invisible.
SHAPE_CEILING = 2000
CEILING_KR = 20000   # treated as "no upper bound" when splitting; nothing here is dearer
MAX_SPLITS = 8       # 2^8 slices is far past the point any brand still overflows


def _fetch_shape(ff: list[list[str]], brand: str, lo_kr: int, hi_kr: int | None,
                 depth: int = 0) -> list[dict]:
    """Every listing for one brand in one size shape, splitting on price when capped.

    The cap is the reason this is recursive rather than a loop. Ten brands market-wide
    hold more than ~6,000 target-size items -- Zara alone has ~21,000 -- and for those a
    flat query returns the first 2,000 and silently drops the rest.

    That is not a random 2,000. `enrol.py` measured Algolia's relevance order favouring
    expensive items: on COS it put 15% of the sample under 200 kr where the population
    is 35%. So a truncated brand skews expensive, its median comes out too high, and its
    items look cheaper than they are -- the same false-bargain direction a thin peer
    group produces, and the whole point of the rotation is that the group is COMPLETE.

    Splitting by price is the fix `enrol.py` already uses. Recursive rather than a fixed
    band list because the price distribution is heavily skewed to the cheap end: a fixed
    200-299 band would itself overflow for Zara, and only splitting where it actually
    overflows keeps the extra requests on the handful of brands that need them.
    """
    price = f"price_SE.amount>={lo_kr * 100}"
    if hi_kr is not None:
        price += f" AND price_SE.amount<{hi_kr * 100}"

    out: list[dict] = []
    page = 0
    while True:
        r = algolia.search(filters=f"isForSale:true AND {price}",
                           facet_filters=list(ff) + [[f"metadata.brand:{brand}"]],
                           hits_per_page=PER_SHAPE, page=page)
        got = r.get("hits", [])
        out.extend(got)
        if len(got) < PER_SHAPE or len(out) >= SHAPE_CEILING:
            break
        page += 1

    if len(out) < SHAPE_CEILING or depth >= MAX_SPLITS:
        return out

    # Capped. Throw this slice away and re-fetch it in halves -- keeping it would mean
    # mixing a biased 2,000 with the complete halves that follow.
    hi = hi_kr if hi_kr is not None else CEILING_KR
    if hi - lo_kr <= 1:
        return out                      # cannot split a 1 kr range any further
    mid = lo_kr + (hi - lo_kr) // 2
    return (_fetch_shape(ff, brand, lo_kr, mid, depth + 1)
            + _fetch_shape(ff, brand, mid, hi_kr, depth + 1))


def sweep(bucket: int, dry: bool) -> int:
    """Fetch every target-size listing for this bucket's brands into staging."""
    shapes = sizes.shapes(sizes.load())
    brands = brands_in_bucket(bucket)
    limit = _arg("--limit")
    if limit:
        brands = brands[: int(limit)]
        print(f"  --limit {limit}: sweeping a slice only, NOT a usable pool refresh")
    print(f"bucket {bucket}: {len(brands):,} brands x {len(shapes)} size shapes")
    if dry:
        return 0

    enrol._load_caches()
    today = dt.date.today().isoformat()
    staged = seen_brands = 0

    for i, brand in enumerate(brands, 1):
        hits: list[dict] = []
        for _demo, ff in shapes:
            hits.extend(_fetch_shape(ff, brand, MIN_PRICE_KR, None))

        # A brand too thin to form a group of 8 cannot produce a peer comparison at any
        # level we use, so staging it would only cost a round trip.
        if len(hits) < MIN_BRAND_ITEMS:
            continue

        enrol._prepare(hits)
        rows = []
        for h in hits:
            row = {k: v for k, v in enrol.row_of(h, "P", 1.0, today).items() if k in STAGING_COLS}
            row["bucket"] = bucket
            row["image_paths"] = search.image_paths(h.get("images")) or None
            rows.append(row)

        db.upsert("sweep_staging", rows, "item_id")
        staged += len(rows)
        seen_brands += 1
        # flush=True because this runs for ~20 minutes and Python buffers stdout when
        # it is not a terminal — under nohup, a CI log or a background shell the
        # progress would otherwise appear only when the job finished, which is exactly
        # when nobody needs it.
        if i % 250 == 0:
            print(f"  {i:,}/{len(brands):,} brands · {staged:,} staged", flush=True)

    print(f"  staged {staged:,} items from {seen_brands:,} brands with enough depth")
    return staged


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")

    dry = "--dry-run" in sys.argv
    today = dt.date.today()
    bucket = int(_arg("--bucket")) if _arg("--bucket") else next_bucket()
    print(f"pool sweep for {today.isoformat()}, bucket {bucket} of {BUCKETS}")

    # Staging is transient and single-tenant. A leftover from a crashed run would be
    # mixed into this bucket's peer groups, which is exactly the kind of silent
    # contamination that is impossible to spot afterwards.
    leftover = db.count("sweep_staging?select=item_id")
    if leftover:
        print(f"  clearing {leftover:,} rows left over from an interrupted run")
        if not dry:
            db.delete("sweep_staging?item_id=not.is.null")

    staged = sweep(bucket, dry)
    if dry:
        return
    if not staged:
        sys.exit("  nothing staged — check the size shapes and the brand list")

    written = db.rpc("refresh_pool_bucket",
                     {"p_bucket": bucket, "p_as_of": today.isoformat()})
    print(f"  pool: {written:,} items kept for bucket {bucket}")
    print(f"  staging truncated — the groups were used, not stored")


if __name__ == "__main__":
    main()
