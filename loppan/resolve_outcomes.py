"""Follow the whole catalogue to its outcome, not just the 1,300-item cohort.

This exists to answer one question the project cannot currently answer: does a low
`price_to_estimate` actually predict a good outcome? The 0.84 buy threshold assumes
it does, and that assumption has never been tested.

It could not be tested retrospectively. `priceToEstimateRatio` lives only in the
Typesense index, which carries on-shelf items only, and Parse has no valuation
field at all — `currentValue` is the current ask, `estimateBidV3` is the
sellability score under a misleading name. So once an item resolves, the estimate
it had is gone forever. The ratio has to be frozen on the way in (a trigger does
that) and the outcome collected on the way out (this does that).

Why it is affordable at 84,000 items. An item that still appears in the daily
sweep is, by definition, still listed — that costs no request at all. Only items
that VANISHED since the last sweep need checking, and a MarketOffer query takes a
batch of 60 item pointers at once. So the daily cost is proportional to churn,
not to catalogue size.

One confound this handles deliberately: the sweep only covers items at or above
200 kr, so an item marked down past that floor leaves the sweep without having
resolved. Those come back from Parse as still listed, and are skipped rather than
recorded as an outcome — otherwise ordinary markdowns would be logged as failures,
which is exactly backwards for a project studying markdowns.

    python loppan/resolve_outcomes.py
    python loppan/resolve_outcomes.py --limit 500
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import cohort, db, sellpy

BATCH = 60  # verified; larger risks a server-side timeout on the $in query


def _day(value):
    if isinstance(value, dict):
        value = value.get("iso")
    return value[:10] if value else None


def vanished(limit: int | None) -> list[str]:
    """Items the last sweep did not see, and that we have not already resolved."""
    resolved = {r["item_id"] for r in db.query("item_outcomes?select=item_id")}
    fresh = db.query("catalogue?select=last_seen&order=last_seen.desc&limit=1")
    if not fresh:
        return []
    newest = fresh[0]["last_seen"]

    rows = db.query(f"catalogue?select=item_id&last_seen=lt.{newest}")
    todo = [r["item_id"] for r in rows if r["item_id"] not in resolved]
    return todo[:limit] if limit else todo


def resolve(item_ids: list[str]) -> tuple[list[dict], int]:
    pointers = [
        {"__type": "Pointer", "className": "Item", "objectId": i} for i in item_ids
    ]
    offers = sellpy.find(
        "MarketOffer",
        {"item": {"$in": pointers}, "region": "SE", "latest": True},
        limit=200,
        include="item",
    )

    rows, still_listed = [], 0
    for offer in offers:
        item = offer.get("item") or {}
        item_id = item.get("objectId")
        if not item_id:
            continue

        status = item.get("itemStatus")
        outcome = cohort.STATUS_OUTCOME.get(status, "unknown")

        # Fell below the 200 kr sweep floor rather than resolving. Not an outcome.
        if outcome == "still_listed":
            still_listed += 1
            continue

        listed_on, ended_on = _day(offer.get("createdAt")), _day(offer.get("endedAt"))
        days = None
        if listed_on and ended_on:
            days = (dt.date.fromisoformat(ended_on) - dt.date.fromisoformat(listed_on)).days

        rows.append(
            {
                "item_id": item_id,
                "resolved_on": dt.date.today().isoformat(),
                "outcome": outcome,
                "final_price": (offer.get("pricing") or {}).get("amount"),
                "days_on_market": days,
                "last_status": status,
            }
        )
    return rows, still_listed


def main() -> None:
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    todo = vanished(limit)
    if not todo:
        print("nothing to resolve — every item was seen in the last sweep")
        return

    print(f"{len(todo)} items vanished since the last sweep, "
          f"~{(len(todo) + BATCH - 1) // BATCH} requests")

    written, listed, unknown = 0, 0, 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        try:
            rows, still = resolve(chunk)
        except Exception as exc:  # never let one bad batch kill a long run
            print(f"  batch at {i}: {type(exc).__name__}", file=sys.stderr)
            continue
        listed += still
        unknown += sum(1 for r in rows if r["outcome"] == "unknown")
        if rows:
            db.upsert("item_outcomes", rows, "item_id")
            written += len(rows)
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)} checked, {written} resolved")

    print(f"\n{written} resolved, {listed} still listed (below the sweep floor), "
          f"{unknown} unknown status")
    print("the question this was built for:\n  select * from public.v_ratio_vs_outcome;")


if __name__ == "__main__":
    main()
