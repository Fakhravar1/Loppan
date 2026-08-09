"""Reconstruct complete buy-and-resell round trips from other people's Circle sales.

This is the measurement the project has been missing. Until now the only end-to-end
data was four hand-picked trades by one person. Two facts make a real sample possible:

  1. Circle listings carry `preceding`, a pointer to the item the seller originally
     bought. Verified against four known pairs.
  2. Both items' full price ladders are readable from Parse long after the sale.

So for any stranger's Circle sale we can recover what they paid, what they asked,
what they got, and how long it took. Nobody is identified — only item ids are used.

Cost: four Parse requests per round trip, at one request per second.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import cohort, search, sellpy

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
KEEP_SHARE = 0.84  # Circle payout taken as Sellpy credit (+5% on the 80% share)


def _day(value):
    if isinstance(value, dict):
        value = value.get("iso")
    return value[:10] if value else None


def _gap(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def roundtrip(circle_id: str) -> dict | None:
    circle = sellpy.item(circle_id)
    preceding = circle.get("preceding")
    if not preceding:
        return None
    original_id = preceding["objectId"]

    circle_ladder = sellpy.ladder(circle_id)
    original_ladder = sellpy.ladder(original_id)
    if not circle_ladder or not original_ladder:
        return None

    # What the reseller paid: the last price the original item ever carried.
    bought_price = original_ladder[-1]["pricing"]["amount"]
    bought_on = _day(original_ladder[-1].get("endedAt"))

    asked = circle_ladder[0]["pricing"]["amount"]
    listed_on = _day(circle_ladder[0]["createdAt"])
    final = circle_ladder[-1]["pricing"]["amount"]
    ended_on = _day(circle_ladder[-1].get("endedAt"))

    # Not `== "betald"`. That is sold AND paid out, and the Circle payout lands
    # 21-24 days after the sale (same day for consignment) — so testing it alone
    # silently misses roughly every Circle sale of the last three weeks, which is
    # exactly the window this file exists to measure. `såld` is the same sale with
    # the payout still pending. One mapping, in cohort.STATUS_OUTCOME, so a new
    # status is learned once rather than in each caller.
    sold = cohort.STATUS_OUTCOME.get(circle.get("itemStatus")) == "sold"
    original = sellpy.item(original_id)
    meta = original.get("metadata") or {}
    score = original.get("sellabilityEstimate") or {}
    original_opening = original_ladder[0]["pricing"]["amount"]

    return {
        "circle_id": circle_id,
        "original_id": original_id,
        "brand": meta.get("brand"),
        "type": meta.get("type"),
        "condition": meta.get("condition"),
        "has_defect": bool(meta.get("defects")),
        "season": meta.get("season"),
        "score": score.get("score"),
        # the original listing, as Sellpy sold it
        "original_opening": original_opening,
        "original_rungs": len(original_ladder),
        "bought_price": bought_price,
        "bought_discount": round(1 - bought_price / original_opening, 3) if original_opening else None,
        "bought_on": bought_on,
        # the resale
        "circle_ask": asked,
        "circle_final": final,
        "circle_rungs": len(circle_ladder),
        "listed_on": listed_on,
        "ended_on": ended_on,
        "sold": sold,
        "status": circle.get("itemStatus"),
        # outcome
        "multiple": round(final / bought_price, 2) if bought_price else None,
        "profit": round(final * KEEP_SHARE - bought_price, 1) if sold else None,
        "days_held": _gap(bought_on, listed_on),
        "days_on_circle": _gap(listed_on, ended_on),
    }


def main() -> None:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    flt = sys.argv[2] if len(sys.argv) > 2 else "p2p:true && isOnShelf:true"

    # NB: items that SOLD are removed from the search index entirely — verified
    # against four known sold Circle listings, none of which are present. So the
    # index can supply live listings and dormant ones, but never a completed sale.
    # Sampling `isOnShelf:false` looks like it finds finished trades and does not:
    # it returns the 363-document scrap heap of expired listings, all `vilande`.
    print(f"finding Circle listings: {flt}", file=sys.stderr)
    docs = list(search.iterate(flt, limit=want * 3))
    print(f"  {len(docs)} candidates", file=sys.stderr)

    random.seed(20260804)
    random.shuffle(docs)

    rows = []
    for n, doc in enumerate(docs, 1):
        if len(rows) >= want:
            break
        try:
            row = roundtrip(doc["id"])
        except Exception as exc:
            print(f"  {doc['id']}: {type(exc).__name__}", file=sys.stderr)
            continue
        if row:
            rows.append(row)
        if n % 20 == 0:
            print(f"  scanned {n}, kept {len(rows)}", file=sys.stderr)

    path = DATA / f"roundtrips_{dt.datetime.now():%Y%m%dT%H%M%S}.jsonl"
    DATA.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} round trips to {path}")


if __name__ == "__main__":
    main()
