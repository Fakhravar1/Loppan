"""Move everything collected locally into Postgres.

Idempotent — every table upserts on its primary key, so re-running is safe and is
the intended way to sync after a fresh pull.

    python loppan/load_to_db.py
"""

from __future__ import annotations

import glob
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def _read(pattern: str, newest_only: bool = True) -> list[dict]:
    files = sorted(glob.glob(str(DATA / pattern)))
    if not files:
        return []
    chosen = files[-1:] if newest_only else files
    rows = []
    for path in chosen:
        rows += [json.loads(line) for line in open(path, encoding="utf-8")]
    return rows


def _pick(row: dict, mapping: dict[str, str]) -> dict:
    """Rename local field names to column names, dropping anything unmapped."""
    return {col: row.get(src) for col, src in mapping.items()}


def load_cohort() -> None:
    rows = _read("cohort_baseline.jsonl")
    if not rows:
        print("  cohort_items      : no baseline file")
        return
    mapped = [
        _pick(r, {
            "item_id": "item_id", "stratum": "stratum", "enrolled_on": "enrolled_on",
            "url": "url", "brand": "brand", "item_type": "type",
            "condition": "condition", "has_defect": "has_defect",
            "price_kr": "price_kr", "price_to_estimate": "price_to_estimate",
            "favourites": "favourites", "brand_tier": "brand_tier",
            "last_chance": "last_chance", "is_circle": "is_circle",
            "on_shelf": "on_shelf", "reserved": "reserved",
            "sale_started_at": "sale_started_at",
        })
        for r in rows
    ]
    print(f"  cohort_items      : {db.upsert('cohort_items', mapped, 'item_id')}")


def load_checks() -> None:
    # Every check ever run, not just the newest — the time series IS the result.
    rows = _read("cohort_checks/check_*.jsonl", newest_only=False)
    if not rows:
        print("  cohort_checks     : none")
        return
    mapped = [
        _pick(r, {
            "item_id": "item_id", "checked_on": "checked_on", "outcome": "outcome",
            "status": "status", "final_price": "final_price", "rungs": "rungs",
        })
        for r in rows
    ]
    for row in mapped:
        if row["outcome"] and str(row["outcome"]).startswith("error"):
            row["outcome"] = "error"
    print(f"  cohort_checks     : {db.upsert('cohort_checks', mapped, 'item_id,checked_on')}")


def load_roundtrips() -> None:
    rows = _read("roundtrips_*.jsonl") + _read("dormant_circle_*.jsonl")
    if not rows:
        print("  circle_roundtrips : none")
        return
    cols = ["circle_id", "original_id", "brand", "condition", "has_defect", "season",
            "score", "original_opening", "original_rungs", "bought_price",
            "bought_discount", "bought_on", "circle_ask", "circle_final",
            "circle_rungs", "listed_on", "ended_on", "sold", "status", "multiple",
            "profit", "days_held", "days_on_circle"]
    mapping = {c: c for c in cols}
    mapping["item_type"] = "type"
    seen, mapped = set(), []
    for r in rows:
        if r["circle_id"] in seen:
            continue
        seen.add(r["circle_id"])
        mapped.append(_pick(r, mapping))
    print(f"  circle_roundtrips : {db.upsert('circle_roundtrips', mapped, 'circle_id')}")


def load_ladders() -> None:
    rows = _read("ladders_*.jsonl")
    if not rows:
        print("  item_ladders      : none")
        return
    cols = ["item_id", "brand", "condition", "demography", "season", "has_defect",
            "product_id", "status", "sold", "score", "score_version", "cutoff",
            "dwell_days", "opening_ask", "final_price", "rungs", "decay",
            "listed_on", "ended_on", "days_on_market", "ladder"]
    mapping = {c: c for c in cols}
    mapping["item_type"] = "type"
    print(f"  item_ladders      : {db.upsert('item_ladders', [_pick(r, mapping) for r in rows], 'item_id')}")


def main() -> None:
    if not db.configured():
        try:
            db._creds()
        except db.NotConfigured as exc:
            sys.exit(str(exc))
    print("loading into Postgres...")
    load_cohort()
    load_checks()
    load_roundtrips()
    load_ladders()
    print("\nverify:  select * from public.v_cohort_summary;")


if __name__ == "__main__":
    main()
