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

BUCKETS = 4
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


def todays_bucket(today: dt.date) -> int:
    """Rotate one bucket a day, so the whole market is covered every BUCKETS days."""
    return today.toordinal() % BUCKETS


def brands_in_bucket(bucket: int) -> list[str]:
    rows = db.query("brands?select=name&order=name.asc")
    return [r["name"] for r in rows if r["name"] and bucket_of(r["name"]) == bucket]


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
    price_filter = f"isForSale:true AND price_SE.amount>={MIN_PRICE_KR * 100}"
    today = dt.date.today().isoformat()
    staged = seen_brands = 0

    for i, brand in enumerate(brands, 1):
        hits: list[dict] = []
        for _demo, ff in shapes:
            page = 0
            while True:
                r = algolia.search(filters=price_filter,
                                   facet_filters=list(ff) + [[f"metadata.brand:{brand}"]],
                                   hits_per_page=PER_SHAPE, page=page)
                got = r.get("hits", [])
                hits.extend(got)
                # Algolia stops paginating a single shape after ~2,000 results, so a
                # short page is the end whether or not nbHits agrees.
                if len(got) < PER_SHAPE or len(hits) >= 2000:
                    break
                page += 1

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
    bucket = int(_arg("--bucket", todays_bucket(today)))
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
