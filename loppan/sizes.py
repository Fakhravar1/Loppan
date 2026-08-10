"""The sizes worth crawling, and the Algolia shapes that fetch exactly those.

    python loppan/sizes.py            # what the target set covers, live from Algolia
    python loppan/sizes.py --all      # include the disabled candidate rows

Only ~37% of Sellpy's wearable market can ever fit the two people this is being bought
for. `target_sizes` in Postgres is that decision; this module turns it into queries.

⚠️ **This must never be applied to `enrol.py`.** That job builds the stratified sample
the whole statistical layer rests on — `brand_daily`, `predictor_daily`, `sample_weight`,
every sell rate and lift on the board. Restricting it by size would silently make every
one of those numbers "…among items that happen to fit two particular people". The size
restriction belongs to the candidate sweep, which is a separate population with a
separate purpose. See `analytics.md` §8 on why the two are never pooled.

## Why the shapes are plural

Algolia's `facetFilters` is AND across the outer list and OR within each inner list, so
one query cannot express "(this size AND women) OR (that size AND men)". Two of Sellpy's
size fields carry no gender at all — `SHOES-EU-*` and `PANTS-INCH-*` are shared — and
without the pairing, women's shoes 40–41 also returns 3,148 men's shoes and men's W32–33
returns 896 women's trousers (measured 2026-08-10).

So the target set becomes one shape per demography scope: an unpaired shape for codes
that already imply their wearer (`MEN-*`, `WMN-*`, `ONE SIZE`), plus one shape per
demography for the shared fields.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, db

MIN_PRICE_KR = 200  # the shortlist floor; below it the peer signal inverts


def load(only_enabled: bool = True) -> list[dict]:
    """Target sizes, newest decision first. Ordered so output is stable."""
    path = "target_sizes?select=size_code,demography,wearer,bucket,enabled,note&order=bucket.asc,size_code.asc"
    rows = db.query(path)
    return [r for r in rows if r["enabled"]] if only_enabled else rows


def shapes(rows: list[dict]) -> list[tuple[str | None, list[list[str]]]]:
    """Group the target sizes into (demography, facet_filters) query shapes.

    Returns the `facet_filters` argument for `algolia.search` directly — wearable
    categories, the size OR-group, and the demography constraint where one applies.
    """
    by_demo: dict[str | None, list[str]] = {}
    for r in rows:
        by_demo.setdefault(r["demography"], []).append(r["size_code"])

    wear = [f"categories.lvl1:{c}" for c in algolia.WEARABLE]
    out = []
    for demo, codes in sorted(by_demo.items(), key=lambda kv: (kv[0] or "")):
        ff = [wear, [f"sizes:{c}" for c in sorted(codes)]]
        if demo:
            ff.append([f"metadata.demography:{demo}"])
        out.append((demo, ff))
    return out


def market_count(ff: list[list[str]], min_price_kr: int = MIN_PRICE_KR) -> tuple[int, bool]:
    """How many live listings a shape matches, market-wide. (count, is_exact).

    `hits_per_page=0` so nothing is transferred — this asks the index to count, not to
    send anything. Read the exhaustive flag: Algolia's nbHits becomes an estimate once
    the result set is large, and a filtered count that looks precise often is not.
    """
    r = algolia.search(filters=f"isForSale:true AND price_SE.amount>={min_price_kr * 100}",
                       facet_filters=ff, hits_per_page=0)
    return r.get("nbHits", 0), bool(r.get("exhaustiveNbHits"))


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")

    rows = load(only_enabled="--all" not in sys.argv)
    print(f"{len(rows)} target size codes at {MIN_PRICE_KR} kr and above\n")

    # Per bucket, so a thin one is visible before it disappoints someone browsing.
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r["bucket"], []).append(r)

    wear = [f"categories.lvl1:{c}" for c in algolia.WEARABLE]
    total_note = []
    print(f"{'bucket':18s} {'codes':>5} {'live market':>12}")
    print("-" * 40)
    for bucket, brs in sorted(buckets.items()):
        ff = [wear, [f"sizes:{r['size_code']}" for r in brs]]
        demos = {r["demography"] for r in brs if r["demography"]}
        if len(demos) == 1:
            ff.append([f"metadata.demography:{demos.pop()}"])
        n, exact = market_count(ff)
        total_note.append(exact)
        print(f"{bucket:18s} {len(brs):5d} {n:12,}{'' if exact else '  (estimate)'}")

    print()
    for demo, ff in shapes(rows):
        n, exact = market_count(ff)
        label = f"demography={demo}" if demo else "size implies wearer"
        print(f"  shape [{label:24s}] {n:>10,}{'' if exact else '  (estimate)'}")

    print("\nBucket counts overlap where an item carries several size codes, so they do "
          "not sum to the shape totals.")


if __name__ == "__main__":
    main()
