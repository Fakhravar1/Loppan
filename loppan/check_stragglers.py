"""Follow items that fell below the sweep floor, all the way to their fate.

The problem this fixes. The sweep is scoped to items at or above 100 kr, so an item
marked down past that floor silently leaves the collection — while remaining
perfectly alive on the shelf. Measured against the 1,747 resolved items with real
ladders, that is not a rare edge case:

    of items that OPENED at or above 100 kr...
      43% of the ones that SOLD ended below 100 kr   (median final price: 80 kr)
      83% of the ones that FAILED ended below 100 kr (median final price: 35 kr)

So the floor truncates the end of nearly every item's life, and it discards
failures at roughly twice the rate of successes. Left alone, that biases measured
sell-through UPWARD — you keep the items that sold early and high, and lose the
ones that ground down and quietly expired. Those quiet expiries are the entire
point of the measurement (§11).

Why this is cheap. The floor governs DISCOVERY, not FOLLOW-UP. Once an item's id
is known, MarketOffer returns its state and its whole ladder regardless of price,
and it keeps doing so long after the item ends (§5.2). Nothing about a cheap item
is unreadable — it is only undiscoverable. So these can be checked slowly: weekly
is plenty, because a late look loses no information.

    python loppan/check_stragglers.py
    python loppan/check_stragglers.py --limit 600
    python loppan/check_stragglers.py --ladders 100   # also recover truncated ladders
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import cohort, db, resolve_outcomes, sellpy

BATCH = 60        # verified safe for a $in query with include=item
LADDER_BATCH = 20  # full ladders are ~6 offers each; 20 x 6 stays under limit=200


def _day(value):
    if isinstance(value, dict):
        value = value.get("iso")
    return value[:10] if value else None


def stragglers(limit: int | None) -> list[dict]:
    """Below-floor items, least recently checked first."""
    q = ("item_outcomes?select=item_id,checks,last_price_kr"
         "&outcome=eq.below_floor&order=last_checked_on.asc.nullsfirst")
    rows = db.query(q)
    return rows[:limit] if limit else rows


def check(batch: list[dict]) -> tuple[list[dict], list[dict], int]:
    """Returns (rows to write, newly resolved rows, count still below floor)."""
    by_id = {r["item_id"]: r for r in batch}
    pointers = [
        {"__type": "Pointer", "className": "Item", "objectId": i} for i in by_id
    ]
    offers = sellpy.find(
        "MarketOffer",
        {"item": {"$in": pointers}, "region": "SE", "latest": True},
        limit=200,
        include="item",
    )
    # The latest offer's createdAt is when the FINAL price was set, not when the item
    # was listed. Use the first offer for that, or days_on_market silently becomes
    # "days at the final price".
    listed = resolve_outcomes.first_listed(pointers)

    today = dt.date.today().isoformat()
    rows, resolved, still_below = [], [], 0

    for offer in offers:
        item = offer.get("item") or {}
        item_id = item.get("objectId")
        prior = by_id.get(item_id)
        if not item_id or not prior:
            continue

        status = item.get("itemStatus")
        outcome = cohort.STATUS_OUTCOME.get(status, "unknown")
        price = (offer.get("pricing") or {}).get("amount")

        listed_on, ended_on = listed.get(item_id), _day(offer.get("endedAt"))
        days = None
        if listed_on and ended_on:
            days = (dt.date.fromisoformat(ended_on)
                    - dt.date.fromisoformat(listed_on)).days

        alive = outcome == "still_listed"
        if alive:
            still_below += 1

        # Uniform keys across the batch: PostgREST rejects a mixed-shape upsert.
        row = {
            "item_id": item_id,
            "resolved_on": today,
            "outcome": "below_floor" if alive else outcome,
            "final_price": None if alive else price,
            "days_on_market": None if alive else days,
            "last_status": status,
            "last_checked_on": today,
            "checks": (prior.get("checks") or 0) + 1,
            "last_price_kr": price,
        }
        if not alive:
            resolved.append(row)
        rows.append(row)

    return rows, resolved, still_below


def keep_the_ladder_moving(rows: list[dict]) -> None:
    """Write the new below-floor price back to catalogue so the ladder keeps accruing.

    The existing price-change trigger does the logging, so this recovers the part of
    the markdown curve that lives under the floor. `last_seen` is deliberately NOT
    touched: resolve_outcomes.py uses it to detect what vanished from the sweep, and
    refreshing it here would hide these items from that check.
    """
    priced = [
        {"item_id": r["item_id"], "price_kr": r["last_price_kr"]}
        for r in rows
        if r.get("last_price_kr") is not None
    ]
    for i in range(0, len(priced), 200):
        db.update("catalogue", priced[i : i + 200], "item_id")


def recover_ladders(item_ids: list[str]) -> int:
    """Pull the full markdown history for items that just resolved.

    Costs one request per item, so it is opt-in and bounded. Everything else here
    batches; this cannot, because a ladder is many offers for one item.
    """
    written = 0
    for item_id in item_ids:
        try:
            ladder = sellpy.ladder(item_id)
        except Exception as exc:  # never let one bad item kill a long run
            print(f"  ladder {item_id}: {type(exc).__name__}", file=sys.stderr)
            continue
        path = cohort._path(ladder)
        if not path:
            continue
        db.upsert("item_ladders", [{
            "item_id": item_id,
            "sampled_on": dt.date.today().isoformat(),
            "status": "below_floor_recovered",
            **{k: v for k, v in path.items() if k != "ladder"},
            "ladder": path.get("ladder"),
        }], "item_id")
        written += 1
    return written


def main() -> None:
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    ladders = 0
    if "--ladders" in sys.argv:
        ladders = int(sys.argv[sys.argv.index("--ladders") + 1])

    todo = stragglers(limit)
    if not todo:
        print("no below-floor items to follow")
        return

    print(f"{len(todo)} below-floor items, ~{(len(todo) + BATCH - 1) // BATCH} requests")

    all_resolved, still, checked = [], 0, 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        try:
            rows, resolved, below = check(chunk)
        except Exception as exc:
            print(f"  batch at {i}: {type(exc).__name__}", file=sys.stderr)
            continue
        if rows:
            db.update("item_outcomes", rows, "item_id")
            keep_the_ladder_moving(rows)
            checked += len(rows)
        all_resolved.extend(resolved)
        still += below
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)} checked, "
              f"{len(all_resolved)} resolved")

    sold = sum(1 for r in all_resolved if r["outcome"] == "sold")
    expired = sum(1 for r in all_resolved if r["outcome"] == "expired")
    print(f"\n{checked} checked: {len(all_resolved)} resolved "
          f"({sold} sold, {expired} expired), {still} still below the floor")

    if ladders and all_resolved:
        ids = [r["item_id"] for r in all_resolved][:ladders]
        print(f"\nrecovering full ladders for {len(ids)} newly resolved items")
        print(f"  wrote {recover_ladders(ids)}")

    print("\nsell-through including below-floor outcomes:"
          "\n  select * from public.v_ratio_vs_outcome;")


if __name__ == "__main__":
    main()
