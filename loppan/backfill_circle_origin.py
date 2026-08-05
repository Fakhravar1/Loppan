"""Record what each tracked Circle seller PAID for their item.

Time-sensitive, which is why it runs as its own job rather than waiting.

A Circle listing points back, via `preceding`, to the item its seller originally
bought from Sellpy. Once a tracked Circle item sells we will see what it fetched —
but the purchase price lives on the *original* listing, and there is no guarantee
that stays reachable. Capturing the link now is what turns "did it sell" into the
full round trip: paid P, sold for S, so the multiple was S/P.

Without this the Circle stratum measures sell-through and nothing else, and
sell-through alone cannot say whether the trade is profitable.

Two Parse requests per item, one request per second.

    python loppan/backfill_circle_origin.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db, sellpy


def _day(value):
    if isinstance(value, dict):
        value = value.get("iso")
    return value[:10] if value else None


# PostgREST rejects a batch whose objects do not all carry the same keys
# ("All object keys must match"), so every row is built from this shape with
# explicit nulls rather than by omitting fields.
FIELDS = ("item_id", "original_id", "bought_price", "bought_on",
          "original_opening", "original_rungs", "bought_discount")


def origin_of(circle_id: str) -> dict | None:
    """What the seller paid, and how marked-down the item was when they bought."""
    circle = sellpy.item(circle_id)
    preceding = circle.get("preceding")
    if not preceding:
        return None

    row = dict.fromkeys(FIELDS)
    row["item_id"] = circle_id
    row["original_id"] = preceding["objectId"]

    ladder = sellpy.ladder(row["original_id"])
    if not ladder:
        return row  # linked, but the original's price history is gone

    opening = ladder[0]["pricing"]["amount"]
    paid = ladder[-1]["pricing"]["amount"]
    row.update({
        "bought_price": paid,
        "bought_on": _day(ladder[-1].get("endedAt")),
        "original_opening": opening,
        "original_rungs": len(ladder),
        "bought_discount": round(1 - paid / opening, 3) if opening else None,
    })
    return row


DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
CACHE = DATA / "circle_origins.jsonl"


def _cached() -> dict[str, dict]:
    """Origins already fetched from Sellpy, keyed by Circle item id.

    Fetching costs two Sellpy requests per item and roughly 17 minutes for the
    whole stratum. A database error should not make us pay that again, so every
    origin is written here the moment it is known, and a re-run resumes from it.
    """
    if not CACHE.exists():
        return {}
    return {
        row["item_id"]: row
        for row in (json.loads(line) for line in CACHE.open(encoding="utf-8"))
    }


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")

    todo = db.query("cohort_items?select=item_id&stratum=eq.circle&original_id=is.null")
    if not todo:
        print("nothing to backfill — every Circle item already has its origin")
        return

    cache = _cached()
    fetch = [r for r in todo if r["item_id"] not in cache]
    print(f"{len(todo)} to backfill | {len(cache)} already fetched | "
          f"{len(fetch)} to pull from Sellpy (~{max(1, len(fetch)*2//60)} min)")

    DATA.mkdir(parents=True, exist_ok=True)
    missing, failed = 0, 0

    with CACHE.open("a", encoding="utf-8") as fh:
        for n, row in enumerate(fetch, 1):
            try:
                origin = origin_of(row["item_id"])
            except Exception as exc:
                failed += 1
                print(f"  {row['item_id']}: {type(exc).__name__}", file=sys.stderr)
                continue
            if origin is None:
                missing += 1  # a Circle listing with no preceding pointer
                continue
            fh.write(json.dumps(origin, ensure_ascii=False) + "\n")
            fh.flush()
            cache[origin["item_id"]] = origin
            if n % 50 == 0:
                print(f"  fetched {n}/{len(fetch)}", file=sys.stderr)

    wanted = {r["item_id"] for r in todo}
    rows = [v for k, v in cache.items() if k in wanted]
    print(f"\nwriting {len(rows)} rows to Postgres...")
    print(f"  updated {db.update('cohort_items', rows, 'item_id')}")

    print(f"\ndone. no preceding pointer: {missing} | fetch errors: {failed}")
    print("check:  select * from public.v_circle_outcomes limit 5;")


if __name__ == "__main__":
    main()
