"""Pull the complete markdown history for items found by scan_live.py.

One request per item, so this is the expensive step — but it is the only way to
learn an item's *opening* ask, which is the quantity the whole strategy turns on.
The `first: true` filter that would give openings in bulk is unindexed and times
out server-side, so there is no shortcut.

Output: data/ladders_<timestamp>.jsonl, one item per line with its full ladder.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import cohort, sellpy

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def _day(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("iso")
    return value[:10] if value else None


def summarise_ladder(item_id: str, item: dict, offers: list[dict]) -> dict | None:
    """Collapse one item's price history into the features a buy rule would use."""
    if not offers:
        return None

    prices = [o["pricing"]["amount"] for o in offers]
    opening, final = prices[0], prices[-1]
    started = _day(offers[0]["createdAt"])
    ended = _day(offers[-1].get("endedAt"))

    days = None
    if started and ended:
        days = (dt.date.fromisoformat(ended) - dt.date.fromisoformat(started)).days

    meta = item.get("metadata") or {}
    score = item.get("sellabilityEstimate") or {}

    dwell = None
    assorted, shelved = _day(item.get("assortedAt")), _day(item.get("putOnShelfAt"))
    if assorted and shelved:
        dwell = (dt.date.fromisoformat(shelved) - dt.date.fromisoformat(assorted)).days

    return {
        "item_id": item_id,
        "brand": meta.get("brand"),
        "type": meta.get("type"),
        "condition": meta.get("condition"),
        "demography": meta.get("demography"),
        "season": meta.get("season"),
        "has_defect": bool(meta.get("defects")),
        "product_id": meta.get("productId"),
        "status": item.get("itemStatus"),
        # `betald` is sold AND paid out; `såld` is the same sale with the payout
        # still pending, which for Circle is 21-24 days later. Testing only the
        # former recorded two real sales here as unsold. cohort.STATUS_OUTCOME is
        # the single mapping — the raw status stays above, so nothing is lost.
        "sold": cohort.STATUS_OUTCOME.get(item.get("itemStatus")) == "sold",
        "score": score.get("score"),
        "score_version": score.get("version"),
        "cutoff": score.get("cutoff"),
        "dwell_days": dwell,
        # the features the strategy actually argues about
        "opening_ask": opening,
        "final_price": final,
        "rungs": len(offers),
        "decay": round(1 - final / opening, 3) if opening else None,
        "listed_on": started,
        "ended_on": ended,
        "days_on_market": days,
        "ladder": [
            {"price": o["pricing"]["amount"], "from": _day(o["createdAt"]), "to": _day(o.get("endedAt"))}
            for o in offers
        ],
    }


def main() -> None:
    scans = sorted(DATA.glob("scan_*.jsonl"))
    if not scans:
        sys.exit("no scan file found — run scan_live.py first")

    offers = [json.loads(line) for line in scans[-1].open(encoding="utf-8")]
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150

    # Random, not cherry-picked: a cohort selected on taste measures taste.
    random.seed(20260804)
    chosen = random.sample(offers, min(limit, len(offers)))

    out = []
    for n, offer in enumerate(chosen, 1):
        item = offer.get("item") or {}
        item_id = item.get("objectId")
        if not item_id:
            continue
        try:
            row = summarise_ladder(item_id, item, sellpy.ladder(item_id))
        except Exception as exc:  # never let one bad item kill a long run
            print(f"  {item_id}: {type(exc).__name__}", file=sys.stderr)
            continue
        if row:
            out.append(row)
        if n % 25 == 0:
            print(f"  {n}/{len(chosen)}", file=sys.stderr)

    path = DATA / f"ladders_{dt.datetime.now():%Y%m%dT%H%M%S}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in out:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(out)} ladders to {path}")


if __name__ == "__main__":
    main()
