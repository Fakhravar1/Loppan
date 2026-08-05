"""Retroactive seasonality: what cleared, in which month, at what discount.

The forward cohort cannot answer this. It was enrolled on a single date, so it can
only show behaviour from that date onward. Sellpy's price history reaches back
years and covers every month, so the seasonal question — is a winter coat cheaper
in July than in November — is answerable today rather than in a year.

Two stages, both resumable:

  1. Enumerate the most recent offer of many items, with the full item inlined.
     Parse allows roughly 9,000 rows through this window; beyond that it times out.
  2. For those carrying a season tag and already resolved, pull the full markdown
     ladder. That gives `decay` — how far the price fell before it cleared — which
     is the measure that matters, because expressing it as a ratio cancels out
     brand and quality mix between months.

Both stages cache to disk. Ladder pulls cost one request each and there are
thousands, so a failure must never mean starting over.

    python loppan/season_history.py            # scan + pull + load
    python loppan/season_history.py --load     # just re-load the cache to Postgres
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import cohort, db, search, sellpy

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
OFFERS = DATA / "season_offers.jsonl"
LADDERS = DATA / "season_ladders.jsonl"

# Parse times out past roughly this depth on the `latest` window.
MAX_SKIP = 8900
PAGE = 100


def _load_jsonl(path: pathlib.Path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    out = {}
    for line in path.open(encoding="utf-8"):
        row = json.loads(line)
        out[row[key]] = row
    return out


def scan() -> dict[str, dict]:
    """Walk the offer window, keeping the inlined item. Resumes where it left off."""
    seen = _load_jsonl(OFFERS, "objectId")

    # Reuse anything an earlier scan already collected.
    for path in sorted(glob.glob(str(DATA / "scan_*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            row = json.loads(line)
            seen.setdefault(row["objectId"], row)

    print(f"  starting from {len(seen)} offers already collected")
    DATA.mkdir(parents=True, exist_ok=True)

    with OFFERS.open("a", encoding="utf-8") as fh:
        skip = 0
        while skip <= MAX_SKIP:
            try:
                batch = sellpy.find(
                    "MarketOffer",
                    {"region": "SE", "latest": True},
                    limit=PAGE,
                    skip=skip,
                    include="item",
                )
            except sellpy.QueryTooSlow:
                print(f"  skip={skip}: timed out, stopping the scan", file=sys.stderr)
                break
            if not batch:
                break
            fresh = 0
            for offer in batch:
                if offer["objectId"] not in seen:
                    seen[offer["objectId"]] = offer
                    fh.write(json.dumps(offer, ensure_ascii=False) + "\n")
                    fresh += 1
            fh.flush()
            if skip % 1000 == 0:
                print(f"  skip={skip:5d} (+{fresh}) total={len(seen)}", file=sys.stderr)
            skip += PAGE

    return seen


def candidates(offers: dict[str, dict]) -> list[dict]:
    """Season-tagged items that have already reached a terminal state."""
    out, seen_items = [], set()
    for offer in offers.values():
        item = offer.get("item") or {}
        meta = item.get("metadata") or {}
        if not meta.get("season") or not offer.get("endedAt"):
            continue
        if item.get("objectId") in seen_items:
            continue
        seen_items.add(item["objectId"])
        out.append(item)
    return out


def pull_ladders(items: list[dict]) -> None:
    """One request per item. Append as we go so a crash costs nothing."""
    done = _load_jsonl(LADDERS, "item_id")
    todo = [i for i in items if i["objectId"] not in done]
    print(f"  {len(items)} season-tagged & resolved | {len(done)} already pulled | "
          f"{len(todo)} to fetch (~{max(1, len(todo)//60)} min)")

    with LADDERS.open("a", encoding="utf-8") as fh:
        for n, item in enumerate(todo, 1):
            try:
                path = cohort._path(sellpy.ladder(item["objectId"]))
            except Exception as exc:
                print(f"  {item['objectId']}: {type(exc).__name__}", file=sys.stderr)
                continue
            if not path:
                continue
            meta = item.get("metadata") or {}
            fh.write(json.dumps({
                "item_id": item["objectId"],
                "season": meta.get("season"),
                "brand": meta.get("brand"),
                "item_type": meta.get("type"),
                "condition": meta.get("condition"),
                "materials": meta.get("material"),
                "image_paths": search.image_paths(item.get("images")),
                "demography": meta.get("demography"),
                "has_defect": bool(meta.get("defects")),
                "status": item.get("itemStatus"),
                "sold": item.get("itemStatus") in ("betald", "såld"),
                **path,
            }, ensure_ascii=False) + "\n")
            fh.flush()
            if n % 100 == 0:
                print(f"  pulled {n}/{len(todo)}", file=sys.stderr)


def load() -> None:
    rows = list(_load_jsonl(LADDERS, "item_id").values())
    if not rows:
        print("  nothing cached to load")
        return
    print(f"  upserting {len(rows)} rows")
    print(f"  wrote {db.upsert('season_clearings', rows, 'item_id')}")


def main() -> None:
    if "--load" not in sys.argv:
        print("scanning offers...")
        offers = scan()
        print("\nselecting candidates...")
        items = candidates(offers)
        print("\npulling ladders...")
        pull_ladders(items)
    print("\nloading to Postgres...")
    load()
    print("\ncheck:  select * from public.v_season_by_month;")


if __name__ == "__main__":
    main()
