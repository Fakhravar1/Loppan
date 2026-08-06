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
    """Items the last sweep did not see, and that we have not already resolved.

    Gated on the sweep ledger, because "did not see" is only meaningful if the
    sweep actually finished. A sweep that died at 60% leaves ~66,000 live items
    with a stale last_seen; without this check they would all be queried against
    Parse and written as below_floor — mislabelling live inventory and putting a
    20x traffic spike on Sellpy, which §8 says is the risk that actually matters.
    Normal churn is ~0.4%, so the 5% ceiling is twelve times headroom.
    """
    guard = db.rpc("resolve_precheck", {"p_max_pct": 5.0})
    if not guard.get("ok"):
        sys.exit(f"refusing to resolve: {guard['reason']}\n"
                 f"  {guard['stale']} rows ({guard['pct']}%) look vanished\n"
                 f"  fix the sweep first — these are almost certainly still listed")
    print(f"sweep ledger ok ({guard['last_sweep']}); "
          f"{guard['stale']} items ({guard['pct']}%) vanished since it ran")

    resolved = {r["item_id"] for r in db.query("item_outcomes?select=item_id")}
    fresh = db.query("catalogue?select=last_seen&order=last_seen.desc&limit=1")
    if not fresh:
        return []
    newest = fresh[0]["last_seen"]

    rows = db.query(f"catalogue?select=item_id&last_seen=lt.{newest}")
    todo = [r["item_id"] for r in rows if r["item_id"] not in resolved]
    return todo[:limit] if limit else todo


def first_listed(pointers: list[dict]) -> dict[str, str]:
    """When each item was FIRST listed, keyed by item id.

    The `latest: true` offer is only the last rung of the markdown ladder, so its
    createdAt is when the final price was set — often days before the sale, and
    nothing to do with how long the item was on the market. Taking the difference
    against that would report "days at the final price" while calling it
    days_on_market. One extra batched request per 60 items buys the real number.
    """
    offers = sellpy.find(
        "MarketOffer",
        {"item": {"$in": pointers}, "region": "SE", "first": True},
        limit=200,
    )
    out = {}
    for offer in offers:
        item_id = (offer.get("item") or {}).get("objectId")
        if item_id:
            out[item_id] = _day(offer.get("createdAt"))
    return out


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
    listed = first_listed(pointers)

    rows, still_listed = [], 0
    for offer in offers:
        item = offer.get("item") or {}
        item_id = item.get("objectId")
        if not item_id:
            continue

        status = item.get("itemStatus")
        outcome = cohort.STATUS_OUTCOME.get(status, "unknown")
        price = (offer.get("pricing") or {}).get("amount")
        today = dt.date.today().isoformat()

        # Marked down past the sweep floor rather than resolving. Still alive, so it
        # is NOT an outcome — but it must be recorded, or it gets re-queried every
        # day forever and its eventual fate is never observed. check_stragglers.py
        # follows these to the end.
        # listed_on comes from the FIRST offer, not this one — see first_listed().
        listed_on, ended_on = listed.get(item_id), _day(offer.get("endedAt"))
        days = None
        if listed_on and ended_on:
            days = (dt.date.fromisoformat(ended_on) - dt.date.fromisoformat(listed_on)).days

        alive = outcome == "still_listed"
        if alive:
            still_listed += 1

        # Every row carries the same keys: PostgREST rejects a batch upsert whose
        # objects differ in shape ("All object keys must match"), so the live rows
        # spell out final_price and days_on_market as null rather than omitting them.
        rows.append(
            {
                "item_id": item_id,
                "resolved_on": today,
                "outcome": "below_floor" if alive else outcome,
                "final_price": None if alive else price,
                "days_on_market": None if alive else days,
                "last_status": status,
                "last_checked_on": today,
                "last_price_kr": price,
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

    print(f"\n{written} written: {written - listed} resolved, {listed} below the floor "
          f"and still alive, {unknown} unknown status")
    if listed:
        print(f"  the {listed} below-floor items are now tracked, not skipped — "
              "check_stragglers.py follows them to the end")
    print("the question this was built for:\n  select * from public.v_ratio_vs_outcome;")


if __name__ == "__main__":
    main()
