"""Follow enrolled items to their outcome, and record the price path on the way.

How an outcome is detected. Sold items are **deleted** from the Algolia index —
verified: 0 of 200 known-sold items remained, while 8 of 8 expired ones did. So an
item that disappears has probably sold. "Probably" is not good enough for the label
the whole model rests on, so every disappearance is adjudicated against Parse, where
`itemStatus` is authoritative. That separates a sale from a delisting, a return to the
seller, or a recategorisation. Counting those as sales would bias sell-through upward,
which is the one error that would make the project worthless.

Why every other day. Weekly is enough to catch outcomes, but four things need finer
resolution and Parse cannot supply any of them: how many likes an item gathers in its
first week, which items sell within days of listing, whether a sale follows shortly
after a markdown, and how listing inflow varies (which sets how crowded the shelf is).
Favourites in particular exist **only** in Algolia — Parse does not store them.

Daily was the intent, but a full pass measures ~66 min, which is ~1,980 of the 2,000
free GitHub Actions minutes per month — inside the limit with nothing left for the
cohort job or a retry. Every other day costs ~990 min and still places a sale within
two days, against a ~60-day median time to sell.

What makes the pass affordable at all. Three things, in order of effect:

  1. **Bulk writes.** The original wrote one HTTP PATCH per row: 54 ms each, so a full
     pass over 666k items was ~10 hours. Batched upsert is 1.5 ms/row — measured 37x —
     and a partial payload leaves untouched columns alone, because `items` has a
     DEFAULT on its only NOT NULL column.
  2. **Only writing rows that changed.** Most items are identical day to day. Skipping
     the rest cuts both the runtime and the dead-tuple churn about fivefold.
  3. **Parallel reads.** The work is pure I/O, so threads turn a serial hour into
     minutes. Note this applies to Algolia only — `sellpy.py` still talks to Sellpy's
     own backend one request per second, serially, because there the risk is the
     account rather than the server.

A note on `last_seen`: it is written only when something else changes, which is
deliberate. Sale-date precision comes from the sweep *cadence*, not from the stored
field — if the sweep runs daily and an item is gone today, it was present yesterday
whether or not we wrote that down.

    python loppan/track.py
    python loppan/track.py --limit 5000
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, cohort, db, sellpy

WRITE_BATCH = 500
ADJUDICATE = 60      # MarketOffer $in ceiling, verified

OUTCOME = {"sold": 1, "expired": 2, "unknown": 3, "still_listed": None}


def live_items(limit: int | None) -> list[dict]:
    """Deliberately does NOT fetch `history`.

    The array is only needed for the ~5% of rows whose price moved, but pulling it
    for all 666k inflates the response several-fold and dominated the runtime. It is
    fetched afterwards, for just those rows, in `histories_for`.
    """
    q = ("items?select=item_id,first_seen,price_ore,favourites,is_reserved,"
         "last_chance&outcome=is.null")
    rows = db.query(q)
    return rows[:limit] if limit else rows


def histories_for(item_ids: list[str]) -> dict[str, list]:
    """Fetch existing price paths for the handful of items that moved."""
    out: dict[str, list] = {}
    for i in range(0, len(item_ids), 200):
        chunk = ",".join(item_ids[i:i + 200])
        for r in db.query(f"items?select=item_id,history&item_id=in.({chunk})"):
            out[r["item_id"]] = r.get("history") or []
    return out


def changed(row: dict, got: dict, today: dt.date) -> dict | None:
    """Build an update only if something actually moved."""
    price = (got.get("price_SE") or {}).get("amount")
    fav = got.get("favouriteCount")
    reserved = got.get("isReserved")
    last_chance = got.get("lastChance")

    price_moved = price is not None and price != row.get("price_ore")
    if not (price_moved
            or fav != row.get("favourites")
            or reserved != row.get("is_reserved")
            or last_chance != row.get("last_chance")):
        return None

    upd = {
        "item_id": row["item_id"],
        "price_ore": price,
        "old_price_ore": ((got.get("priceDrop_SE") or {}).get("oldPrice") or {}).get("amount"),
        "favourites": fav,
        "is_reserved": reserved,
        "last_chance": last_chance,
        "last_seen": today.isoformat(),
    }
    # Flagged here, filled in later: the existing array is fetched only for the
    # rows that actually moved, not for all 666k.
    if price_moved:
        upd["_day"] = (today - dt.date.fromisoformat(row["first_seen"])).days
        upd["_new_price"] = price
    return upd


def attach_histories(rows: list[dict]) -> None:
    """Fill in the price path for rows whose price moved, in one extra round trip."""
    movers = [r for r in rows if "_day" in r]
    if not movers:
        return
    existing = histories_for([r["item_id"] for r in movers])
    for r in movers:
        r["history"] = existing.get(r["item_id"], []) + [r.pop("_day"), r.pop("_new_price")]


def flush(rows: list[dict]) -> int:
    """Write in batches, grouped by column set.

    PostgREST rejects a batch whose objects differ in keys ("All object keys must
    match"), and here they legitimately do: only rows whose price moved carry
    `history`. Padding the others with null would erase their price path, so the
    rows are grouped by signature and each group written separately.
    """
    attach_histories(rows)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(sorted(r)), []).append(r)
    for batch in groups.values():
        for i in range(0, len(batch), WRITE_BATCH):
            db.upsert("items", batch[i:i + WRITE_BATCH], "item_id")
    return len(rows)


def adjudicate(item_ids: list[str]) -> dict[str, tuple[int, int | None]]:
    """Ask Parse what actually happened. Returns item_id -> (outcome, final_ore).

    Serial and throttled on purpose: this is Sellpy's own backend, not a CDN.
    """
    out: dict[str, tuple[int, int | None]] = {}
    for i in range(0, len(item_ids), ADJUDICATE):
        chunk = item_ids[i:i + ADJUDICATE]
        pointers = [{"__type": "Pointer", "className": "Item", "objectId": x} for x in chunk]
        try:
            offers = sellpy.find(
                "MarketOffer",
                {"item": {"$in": pointers}, "region": "SE", "latest": True},
                limit=200, include="item")
        except Exception as exc:
            print(f"  adjudication batch {i}: {type(exc).__name__}", file=sys.stderr)
            continue
        for offer in offers:
            item = offer.get("item") or {}
            item_id = item.get("objectId")
            if not item_id:
                continue
            verdict = cohort.STATUS_OUTCOME.get(item.get("itemStatus"), "unknown")
            price = (offer.get("pricing") or {}).get("amount")
            out[item_id] = (OUTCOME.get(verdict, 3),
                            int(price * 100) if price else None)
    return out


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

    todo = live_items(limit)
    if not todo:
        print("nothing live to track")
        return
    today = dt.date.today()
    by_id = {r["item_id"]: r for r in todo}
    print(f"{len(todo):,} live items, {(len(todo)+99)//100:,} read requests "
          f"across {algolia.MAX_WORKERS} workers")

    pending, gone, seen, written, done = [], [], 0, 0, 0
    for chunk_ids, results in algolia.get_objects_parallel([r["item_id"] for r in todo]):
        for item_id, got in zip(chunk_ids, results):
            done += 1
            if got is None:
                gone.append(item_id)
                continue
            seen += 1
            upd = changed(by_id[item_id], got, today)
            if upd:
                pending.append(upd)
        if len(pending) >= WRITE_BATCH * 4:
            written += flush(pending)
            pending = []
        if done % 50000 < 100:
            print(f"  {done:,}/{len(todo):,} checked, {written:,} written, "
                  f"{len(gone):,} gone", file=sys.stderr)
    written += flush(pending)

    print(f"\n{seen:,} still listed, {written:,} rows changed and written, "
          f"{len(gone):,} disappeared")

    if not gone:
        return
    print(f"adjudicating {len(gone):,} disappearances against Parse "
          f"(~{(len(gone)+ADJUDICATE-1)//ADJUDICATE:,} requests, serial)")
    verdicts = adjudicate(gone)

    rows, unaccounted = [], 0
    for item_id in gone:
        v = verdicts.get(item_id)
        if v is None:
            unaccounted += 1
            continue
        code, final = v
        if code is None:      # Parse says still listed: it left our sampled slice,
            continue          # not the market. Leave it live.
        rows.append({"item_id": item_id, "outcome": code,
                     "resolved_on": today.isoformat(),
                     "final_price_ore": final or by_id[item_id].get("price_ore")})
    if rows:
        flush(rows)
    sold = sum(1 for r in rows if r["outcome"] == 1)
    print(f"  {len(rows):,} given an outcome ({sold:,} sold), "
          f"{unaccounted:,} Parse could not account for")
    print("\n  select outcome, count(*) from public.items group by 1;")


if __name__ == "__main__":
    main()
