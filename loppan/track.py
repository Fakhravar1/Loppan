"""Follow enrolled items to their outcome, and record the price path on the way.

How an outcome is detected. Sold items are **deleted** from the Algolia index —
verified: 0 of 200 known-sold items remained, while 8 of 8 expired ones did. So an
item that disappears has probably sold. "Probably" is not good enough for a label
the whole model depends on, so every disappearance is adjudicated against Parse,
where `itemStatus` is authoritative. That distinguishes a sale from a delisting, a
return to the seller, or a recategorisation.

Why weekly rather than daily. Median time to sell is ~60 days, so weekly checks give
±7-day resolution on duration — ample for a regression — at a seventh of the requests.
Nothing is lost by looking late: Parse retains an item's full ladder and final state
long after it ends.

Why the price is recorded on every observation. Disappearance alone tells you an item
sold. It does not tell you **what it sold for**, and the final price is the target
variable. Each observation appends to the packed `history` array on the item row;
appending 8 bytes to an array is roughly a tenth the cost of a child-table row once
Postgres tuple headers and index entries are counted.

    python loppan/track.py                  # one pass over everything live
    python loppan/track.py --limit 5000
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, cohort, db, sellpy

BATCH = 100          # Algolia getObjects ceiling
ADJUDICATE = 60      # MarketOffer $in ceiling, verified

OUTCOME = {"sold": 1, "expired": 2, "unknown": 3, "still_listed": None}


def live_items(limit: int | None) -> list[dict]:
    q = ("items?select=item_id,first_seen,price_ore,history"
         "&outcome=is.null&order=last_seen.asc.nullsfirst")
    rows = db.query(q)
    return rows[:limit] if limit else rows


def adjudicate(item_ids: list[str]) -> dict[str, tuple[int, int | None]]:
    """Ask Parse what actually happened. Returns item_id -> (outcome, final_ore).

    A disappearance is not self-explanatory: an item can leave the index by selling,
    by being delisted, or by being returned to its seller. Only Parse can tell them
    apart, and mislabelling failures as sales would bias sell-through upward — the
    error this project exists to avoid.
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
            code = OUTCOME.get(verdict, 3)
            price = (offer.get("pricing") or {}).get("amount")
            out[item_id] = (code, int(price * 100) if price else None)
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
    print(f"{len(todo):,} live items, ~{(len(todo)+BATCH-1)//BATCH:,} requests")

    seen, gone, changed = 0, [], 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        results = algolia.get_objects([r["item_id"] for r in chunk])

        updates = []
        for row, got in zip(chunk, results):
            if got is None:
                gone.append(row)
                continue
            seen += 1
            price = (got.get("price_SE") or {}).get("amount")
            upd = {
                "item_id": row["item_id"],
                "price_ore": price,
                "old_price_ore": ((got.get("priceDrop_SE") or {}).get("oldPrice") or {}).get("amount"),
                "favourites": got.get("favouriteCount"),
                "is_reserved": got.get("isReserved"),
                "last_chance": got.get("lastChance"),
                "last_seen": today.isoformat(),
            }
            # Append to the packed history only when the price actually moved. The
            # ladder is what carries information; an unchanged price does not.
            if price is not None and price != row.get("price_ore"):
                day = (today - dt.date.fromisoformat(row["first_seen"])).days
                upd["history"] = (row.get("history") or []) + [day, price]
                changed += 1
            updates.append(upd)

        if updates:
            db.update("items", updates, "item_id")
        if (i // BATCH) % 20 == 0:
            print(f"  {min(i+BATCH, len(todo)):,}/{len(todo):,} checked, "
                  f"{len(gone):,} gone", file=sys.stderr)

    print(f"\n{seen:,} still listed, {changed:,} price changes recorded, "
          f"{len(gone):,} disappeared")

    if gone:
        print(f"adjudicating {len(gone):,} disappearances against Parse "
              f"(~{(len(gone)+ADJUDICATE-1)//ADJUDICATE:,} requests)")
        verdicts = adjudicate([r["item_id"] for r in gone])
        rows, unresolved = [], 0
        for r in gone:
            v = verdicts.get(r["item_id"])
            if v is None:
                unresolved += 1
                continue
            code, final = v
            if code is None:      # Parse says still listed: it left the sampled
                continue          # population, not the market. Leave it live.
            rows.append({"item_id": r["item_id"], "outcome": code,
                         "resolved_on": today.isoformat(),
                         "final_price_ore": final or r.get("price_ore")})
        if rows:
            for j in range(0, len(rows), 200):
                db.update("items", rows[j:j + 200], "item_id")
        sold = sum(1 for r in rows if r["outcome"] == 1)
        print(f"  resolved {len(rows):,} ({sold:,} sold), "
              f"{unresolved:,} Parse could not account for")

    print("\n  select outcome, count(*) from public.items group by 1;")


if __name__ == "__main__":
    main()
