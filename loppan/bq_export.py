"""Export BigQuery's shortlist_candidates to Supabase's shortlist (docs/bigquery.md §7).

    python loppan/bq_export.py --candidates candidates.json [--summary export.json]

`candidates.json` is the output of one query, written by daily.sh:
    bq query --format=json 'select * from loppan.shortlist_candidates'

Three steps:
  1. Attach image paths for these ids only, from the search index (100 ids a request,
     `images` field only). Images are not stored in BigQuery (schema.md, "Deliberately
     not collected"). An id the index no longer returns has sold since the morning
     run, and is dropped rather than shown.
  2. Load the rows into shortlist_staging in batches.
  3. promote_shortlist() swaps them into shortlist in one transaction. It refuses an
     empty swap, so a failed export never blanks the dashboard.

Standard library only. Needs LOPPAN_SUPABASE_KEY (the service-role key).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, db, search

INT = ("weight_g", "price_ore", "n_sales", "expected_now_ore", "expected_peak_ore",
       "peak_month", "months_to_peak", "gross_margin_ore", "expected_profit_ore")
FLOAT = ("sell_through", "pct_of_expected")
BOOL = ("p2p", "history_complete")
TEXT = ("item_id", "brand", "category", "item_type", "size_code", "condition",
        "demography", "as_of")


def _typed(raw: dict) -> dict:
    """The bq CLI's JSON renders every value as a string. Type them for Postgres."""
    row = {}
    for k in TEXT:
        row[k] = raw.get(k)
    for k in INT:
        v = raw.get(k)
        row[k] = None if v in (None, "") else int(float(v))
    for k in FLOAT:
        v = raw.get(k)
        row[k] = None if v in (None, "") else float(v)
    for k in BOOL:
        v = raw.get(k)
        row[k] = None if v is None else str(v).lower() == "true"
    return row


def image_paths_for(ids: list[str]) -> dict[str, list[str] | None]:
    """item_id -> image paths, or None when the index no longer has the item."""
    out: dict[str, list[str] | None] = {}
    for chunk, results in algolia.get_objects_parallel(ids, attributes=["images"]):
        for item_id, obj in zip(chunk, results):
            out[item_id] = None if obj is None else search.image_paths(obj.get("images"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--summary")
    a = ap.parse_args()

    t0 = time.time()
    raw = json.loads(pathlib.Path(a.candidates).read_text(encoding="utf-8") or "[]")
    rows = [_typed(r) for r in raw]
    if not rows:
        print("no candidates: leaving the current shortlist in place", file=sys.stderr)
        return 0

    images = image_paths_for([r["item_id"] for r in rows])
    gone = [r["item_id"] for r in rows if images.get(r["item_id"]) is None]
    rows = [dict(r, image_paths=images[r["item_id"]]) for r in rows
            if images.get(r["item_id"]) is not None]

    db.rpc("clear_shortlist_staging")
    staged = db.upsert("shortlist_staging", rows, on_conflict="item_id")
    promoted = db.rpc("promote_shortlist")

    summary = {"candidates": len(raw), "sold_since_run": len(gone), "staged": staged,
               "promoted": promoted,
               "with_images": sum(1 for r in rows if r["image_paths"]),
               "seconds": round(time.time() - t0, 1)}
    text = json.dumps(summary, indent=1)
    print(text)
    if a.summary:
        pathlib.Path(a.summary).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
