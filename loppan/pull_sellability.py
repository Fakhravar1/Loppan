"""Collect Sellpy's own p(sell) for the cohort.

`sellabilityEstimate` is the most valuable field found so far and the one this
project cannot compute for itself: Sellpy trains it on millions of listed items
and their outcomes, then hands it over for free.

    {"score": 0.984, "isReliable": true, "cutoff": 0.44, "version": "3-mla"}

The point is the correlation, not the number. §3 establishes that sell-through is
the binding constraint and the hardest term in the buy rule. If Sellpy's score
predicts *our* Circle sell-through, that term is solved on day one — and the
cohort is the only place it can be tested, because only the cohort has outcomes.

Why this is cheap. The field lives on the Parse `Item`, not in the search index,
so a naive read costs one request per item — 23 hours for the whole catalogue.
But a MarketOffer query accepts a *set* of item pointers and embeds the full item
with `include=item`, so the cohort costs about 22 requests instead of 1,300.

Why `version` is stored. Two model versions are live at once ('3' and '3-mla').
Scores are only comparable within a version, so anything that pools them is
measuring a model rollout as if it were a market effect.

Run after enrolment, and again whenever you want a fresh reading:

    python loppan/pull_sellability.py
    python loppan/pull_sellability.py --all   # every cohort item, not just missing
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db, sellpy

BATCH = 60  # 60 pointers per query is verified; larger risks a server-side timeout
DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def targets(everything: bool) -> list[str]:
    q = "cohort_items?select=item_id"
    if not everything:
        q += "&sellability_score=is.null"
    return [r["item_id"] for r in db.query(q)]


def fetch(item_ids: list[str]) -> list[dict]:
    """One request per batch. Items with no live SE offer simply do not come back."""
    pointers = [
        {"__type": "Pointer", "className": "Item", "objectId": i} for i in item_ids
    ]
    offers = sellpy.find(
        "MarketOffer",
        {"item": {"$in": pointers}, "region": "SE", "latest": True},
        limit=200,
        include="item",
    )

    today = dt.date.today().isoformat()
    rows = []
    for offer in offers:
        item = offer.get("item") or {}
        est = item.get("sellabilityEstimate")
        if not est or not item.get("objectId"):
            continue
        rows.append(
            {
                "item_id": item["objectId"],
                "sellability_score": est.get("score"),
                "sellability_cutoff": est.get("cutoff"),
                "sellability_reliable": est.get("isReliable"),
                "sellability_version": est.get("version"),
                "sellability_seen_on": today,
            }
        )
    return rows


def main() -> None:
    everything = "--all" in sys.argv
    todo = targets(everything)
    if not todo:
        print("nothing to fetch")
        return

    print(f"{len(todo)} items, ~{(len(todo) + BATCH - 1) // BATCH} requests")

    written, missing = 0, 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        try:
            rows = fetch(chunk)
        except Exception as exc:  # never let one bad batch kill a long run
            print(f"  batch at {i}: {type(exc).__name__}", file=sys.stderr)
            continue
        missing += len(chunk) - len(rows)
        if rows:
            db.update("cohort_items", rows, "item_id")
            written += len(rows)
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)} scanned, {written} written")

    print(f"\n{written} written, {missing} had no sellabilityEstimate on a live SE offer")
    print("compare against outcomes with:\n  select * from public.v_sellability_check;")


if __name__ == "__main__":
    main()
