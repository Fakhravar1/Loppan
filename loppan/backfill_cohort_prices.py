"""Repair cohort rows whose price never landed.

The bug. `baseline` is the only stratum enrolled with no filter at all, and the
unfiltered index contains items with no `price_SE` block. `search.price_kr()`
returns None for those, so 203 of 250 baseline rows were written with a null
price. Every other stratum filtered on something that implies a Swedish price
(`priceToEstimateRatio`, `price_SE.amount`, `p2p`) and so came through intact.

This repairs attributes for items that were already enrolled. It does NOT change
which items are in the cohort — selection stays exactly as frozen on 2026-08-04,
which is the property the whole experiment depends on. Nothing here re-picks,
drops or replaces an item.

Two sources, in order:
  1. The search index, which still carries most of these ids and sometimes now has
     the `price_SE` that was missing at enrolment.
  2. MarketOffer, which has `pricing.amount` in KRONOR for every item, including
     the ones the index never priced. This is the reliable path.

What cannot be repaired: `price_to_estimate`. The estimate lives only in the
Typesense index, so where it was absent at enrolment it is gone for good (§5.3).
Those stay null, deliberately, rather than being filled with a later value that
would misrepresent what was known at enrolment.

    python loppan/backfill_cohort_prices.py
    python loppan/backfill_cohort_prices.py --dry-run
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db, search, sellpy

BATCH = 60


def _meta(item: dict) -> dict:
    return item.get("metadata") or {}


def from_index(item_ids: list[str]) -> dict[str, dict]:
    """Cheap pass: whatever the search index will still give us."""
    out: dict[str, dict] = {}
    for i in range(0, len(item_ids), 50):
        chunk = item_ids[i : i + 50]
        hits = search.search(
            filter_by="id:[" + ",".join(chunk) + "]", per_page=100
        ).get("hits", [])
        for hit in hits:
            doc = hit["document"]
            row = {}
            price = search.price_kr(doc)
            if price is not None:
                row["price_kr"] = price
            # Only fill what is genuinely missing; never overwrite enrolment data.
            summary = search.summarise(doc)
            for field in ("brand", "category", "demography", "condition", "materials",
                          "season", "sizes", "favourites", "brand_tier"):
                key = "item_type" if field == "type" else field
                if summary.get(field) is not None:
                    row[key] = summary[field]
            if row:
                out[doc["id"]] = row
    return out


def from_parse(item_ids: list[str]) -> dict[str, dict]:
    """Reliable pass: MarketOffer prices everything, in kronor."""
    out: dict[str, dict] = {}
    for i in range(0, len(item_ids), BATCH):
        chunk = item_ids[i : i + BATCH]
        pointers = [
            {"__type": "Pointer", "className": "Item", "objectId": x} for x in chunk
        ]
        offers = sellpy.find(
            "MarketOffer",
            {"item": {"$in": pointers}, "region": "SE", "latest": True},
            limit=200,
            include="item",
        )
        for offer in offers:
            item = offer.get("item") or {}
            item_id = item.get("objectId")
            amount = (offer.get("pricing") or {}).get("amount")
            if not item_id or amount is None:
                continue
            meta = _meta(item)
            row = {"price_kr": amount}
            if meta.get("brand"):
                row["brand"] = meta["brand"]
            if meta.get("condition"):
                row["condition"] = meta["condition"]
            if meta.get("demography"):
                row["demography"] = meta["demography"]
            if meta.get("material"):
                row["materials"] = meta["material"]
            if meta.get("size"):
                row["sizes"] = [meta["size"]]
            out[item_id] = row
    return out


def main() -> None:
    dry = "--dry-run" in sys.argv

    broken = db.query("cohort_items?select=item_id,stratum&price_kr=is.null")
    if not broken:
        print("every cohort row has a price — nothing to repair")
        return

    ids = [r["item_id"] for r in broken]
    by_stratum: dict[str, int] = {}
    for r in broken:
        by_stratum[r["stratum"]] = by_stratum.get(r["stratum"], 0) + 1
    print(f"{len(ids)} cohort rows missing a price: "
          + ", ".join(f"{k} {v}" for k, v in sorted(by_stratum.items())))

    print("\npass 1 — search index")
    found = from_index(ids)
    priced = {k: v for k, v in found.items() if "price_kr" in v}
    print(f"  {len(found)} rows enriched, {len(priced)} of them with a price")

    still = [i for i in ids if i not in priced]
    print(f"\npass 2 — MarketOffer for the remaining {len(still)}")
    parse_rows = from_parse(still)
    print(f"  {len(parse_rows)} priced from Parse")

    merged: dict[str, dict] = {}
    for source in (found, parse_rows):
        for item_id, row in source.items():
            merged.setdefault(item_id, {}).update(row)

    rows = [{"item_id": k, **v} for k, v in merged.items() if v]
    unfixed = [i for i in ids if "price_kr" not in merged.get(i, {})]

    if dry:
        print(f"\nDRY RUN — would write {len(rows)} rows; "
              f"{len(unfixed)} would remain without a price")
        return

    for i in range(0, len(rows), 200):
        db.update("cohort_items", rows[i : i + 200], "item_id")
    print(f"\nwrote {len(rows)} rows; {len(unfixed)} still have no price anywhere")
    print("price_to_estimate is NOT backfilled — see docs/handover.md §5.3")


if __name__ == "__main__":
    main()
