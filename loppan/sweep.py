"""Daily sweep: score the catalogue so it can be curated instead of browsed.

Sellpy's real problem is that half a million items are unbrowsable. This walks the
part worth walking, stores current state, and lets the database rank it.

Scope is items at or above 100 kr: ~165,000 of the 529,000 on shelf.

The floor was 200 kr, justified by shipping slots rather than capital being the
binding constraint. That logic priced a slot by the item's PRICE when what
actually matters is profit per slot — and on the 5x thesis the arithmetic runs the
other way. A 100 kr buy at 5x returns ~320 kr after fees, which beats a 400 kr buy
at 2x.

The decisive evidence: both 5x trades in the entire evidence base were bought at
55 kr and 170 kr (§3.5). Both sat below the old floor, so the two items that
produced the target return were invisible to the tool that was meant to find them.

Cost: 250 items per request is Sellpy's maximum, so ~660 requests, about 20
minutes. Still well inside GitHub Actions' allowance, unlike the 63 minutes a
full-catalogue sweep would take — and storage is the real limit below this, since
a 50 kr floor would put the catalogue alone at ~470 MB of a 500 MB tier.

Price history is written by a database trigger, so this can upsert blindly rather
than reading 84,000 current prices back out first.

    python loppan/sweep.py            # sweep, then rebuild brand stats
    python loppan/sweep.py --brands   # brand stats only
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db, search

MIN_PRICE_ORE = 10000  # 100 kr — see the module docstring for why this moved
PAGE = 250             # Sellpy's hard maximum
BATCH = 500            # rows per database write
FILTER = f"isOnShelf:true && price_SE.amount:>={MIN_PRICE_ORE}"

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def row_of(doc: dict) -> dict:
    s = search.summarise(doc)
    return {
        "item_id": s["item_id"],
        "last_seen": dt.date.today().isoformat(),
        "brand": s["brand"],
        "item_type": s["type"],
        "category": s["category"],
        "demography": s["demography"],
        "condition": s["condition"],
        "has_defect": s["has_defect"],
        "materials": s["materials"],
        "sizes": s["sizes"],
        "season": s["season"],
        "price_kr": s["price_kr"],
        "price_to_estimate": s["price_to_estimate"],
        "favourites": s["favourites"],
        "brand_tier": s["brand_tier"],
        "last_chance": s["last_chance"],
        "is_circle": s["is_circle"],
        "sale_started_at": s["sale_started_at"],
        "image_paths": s["image_paths"],
    }


def sweep() -> int:
    total = search.count(FILTER)
    print(f"sweeping {total} items at {MIN_PRICE_ORE//100} kr and above "
          f"(~{total//PAGE + 1} requests)")

    written, batch, page = 0, [], 1
    # A live index shifts under a 300-page walk: items sell, prices change, and
    # unsorted pagination then returns some items twice and skips others. Sorting
    # on a value that does not change makes the walk stable, and the seen set
    # catches whatever slips through — Postgres rejects a batch outright if it
    # contains the same key twice.
    seen: set[str] = set()

    while True:
        hits = search.search(
            filter_by=FILTER, per_page=PAGE, page=page, sort_by="saleStartedAt:asc"
        ).get("hits", [])
        if not hits:
            break

        for hit in hits:
            row = row_of(hit["document"])
            if row["item_id"] in seen:
                continue
            seen.add(row["item_id"])
            batch.append(row)

        if len(batch) >= BATCH:
            written += db.upsert("catalogue", batch, "item_id")
            batch = []
            print(f"  {written}/{total}", file=sys.stderr)
        page += 1

    if batch:
        written += db.upsert("catalogue", batch, "item_id")

    print(f"  wrote {written} distinct items")
    return written


def refresh_brands() -> None:
    """Rebuilt in the database — it is an aggregate over data already there."""
    result = db.rpc("refresh_brand_stats")
    print(f"  brand stats rebuilt: {result} brands")


def refresh_scores() -> None:
    """Materialise score, expected_profit and the flags into `item_scores`.

    These used to be computed inside v_candidates, which meant ORDER BY score had
    to evaluate two functions and a join across every row and then sort — 3.2 s
    against a 3 s statement timeout, so the dashboard's default view simply failed.
    A computed column in a view cannot be indexed; storing it can.

    Must run AFTER refresh_brands(): score multiplies by brand_stats.demand_index,
    so scoring first would bake in yesterday's brand demand. `out_of_season_now`
    also depends on CURRENT_DATE, which is the other reason this is recomputed
    daily rather than cached indefinitely.
    """
    result = db.rpc("refresh_item_scores")
    print(f"  scores rebuilt: {result} eligible candidates")


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    if "--brands" not in sys.argv:
        sweep()
    print("rebuilding brand stats...")
    refresh_brands()
    print("rescoring candidates...")
    refresh_scores()
    print("\ncheck:  select * from public.v_candidates order by score desc limit 20;")


if __name__ == "__main__":
    main()
