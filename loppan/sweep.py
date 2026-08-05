"""Daily sweep: score the catalogue so it can be curated instead of browsed.

Sellpy's real problem is that half a million items are unbrowsable. This walks the
part worth walking, stores current state, and lets the database rank it.

Scope is items at or above 200 kr. That is ~84,000 of the 529,000 on shelf, and
the cut is not a compromise: 92% of the catalogue sits under 400 kr, where the
absolute margin cannot justify a shipping slot — and shipping slots, not capital,
are the binding constraint.

Cost: 250 items per request is Sellpy's maximum, so ~335 requests, about 10
minutes. Well inside GitHub Actions' free allowance, unlike the 63 minutes a
full-catalogue sweep would take.

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

MIN_PRICE_ORE = 20000  # 200 kr
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
    while True:
        hits = search.search(filter_by=FILTER, per_page=PAGE, page=page).get("hits", [])
        if not hits:
            break
        batch += [row_of(h["document"]) for h in hits]

        if len(batch) >= BATCH:
            written += db.upsert("catalogue", batch, "item_id")
            batch = []
            print(f"  {written}/{total}", file=sys.stderr)
        page += 1

    if batch:
        written += db.upsert("catalogue", batch, "item_id")

    print(f"  wrote {written}")
    return written


def refresh_brands() -> None:
    """Rebuilt in the database — it is an aggregate over data already there."""
    result = db.rpc("refresh_brand_stats")
    print(f"  brand stats rebuilt: {result} brands")


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    if "--brands" not in sys.argv:
        sweep()
    print("rebuilding brand stats...")
    refresh_brands()
    print("\ncheck:  select * from public.v_candidates order by score desc limit 20;")


if __name__ == "__main__":
    main()
