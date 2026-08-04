"""Forward-looking cohort: pick items now, follow them to their outcome.

Why this exists. Sell-through — what fraction of items actually sell — is the term
that decides whether the whole strategy is profitable, and it cannot be recovered
from history: items that sell are removed from the search index, so a backward
sample sees only survivors and failures, never successes. The only honest way to
measure it is to name a set of items in advance and watch what happens to them.

Design rules, from docs/handover.md §11:
  - Selection is by filter, never by taste. A cohort picked on instinct measures
    instinct.
  - Control strata are included on purpose. Without items we expect to FAIL,
    every rule looks brilliant.
  - Predictions are frozen at entry. A prediction made afterwards is not one.
  - Everything is followed to the end, including the boring ones. The items that
    quietly expire ARE the measurement.

Outcome detection relies on a verified quirk: a sold item disappears from the
search index, while a failed one lingers and turns `vilande` in Parse. So
"vanished from search" is the signal to ask Parse what happened.

    python loppan/cohort.py snapshot     # once, to enrol
    python loppan/cohort.py check        # weekly
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import search, sellpy

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
BASELINE = DATA / "cohort_baseline.jsonl"
CHECKS = DATA / "cohort_checks"

ON_SHELF = "isOnShelf:true"

# Each stratum states, in advance, what it is for and what we expect. The
# `expect` field is the frozen prediction — it is what later gets scored.
STRATA = [
    {
        "name": "circle",
        "filter": f"p2p:true && {ON_SHELF}",
        "target": 500,
        "expect": "unknown — this is THE measurement",
        "why": "Circle sell-through. The number the entire business case rests on "
               "and the one thing no historical sample can provide.",
    },
    {
        "name": "screen",
        "filter": f"{ON_SHELF} && priceToEstimateRatio:<0.6 && favouriteCount:>=5",
        "target": 150,
        "expect": "sells faster than baseline",
        "why": "The candidate buy signal: heavily discounted AND widely favourited. "
               "Meant to separate 'cheap and wanted' from 'cheap because unwanted'.",
    },
    {
        "name": "control_unwanted",
        "filter": f"{ON_SHELF} && priceToEstimateRatio:<0.6 && favouriteCount:=0",
        "target": 150,
        "expect": "sells slower than the screen stratum",
        "why": "Control. Equally discounted, but nobody is watching it. If this "
               "performs as well as 'screen', favouriteCount carries no signal.",
    },
    {
        "name": "control_wanted_pricey",
        "filter": f"{ON_SHELF} && priceToEstimateRatio:>1.0 && favouriteCount:>=5",
        "target": 150,
        "expect": "sells, but slowly",
        "why": "Control. Wanted but not discounted. Isolates which half of the "
               "screen is doing the work.",
    },
    {
        "name": "premium",
        "filter": f"{ON_SHELF} && price_SE.amount:>=150000",
        "target": 100,
        "expect": "slower than baseline, thinner buyer pool",
        "why": "The 1500 kr+ band. Untouched by any of the four known trades, and "
               "the band the 'fewer, larger tickets' strategy depends on.",
    },
    {
        "name": "baseline",
        "filter": ON_SHELF,
        "target": 250,
        "expect": "the reference rate",
        "why": "Unfiltered. Every other stratum is only meaningful relative to this.",
    },
]


def _enrol(doc: dict, stratum: str) -> dict:
    row = search.summarise(doc)
    row["stratum"] = stratum
    row["enrolled_on"] = dt.date.today().isoformat()
    return row


def snapshot() -> None:
    if BASELINE.exists():
        sys.exit(f"{BASELINE} already exists — refusing to overwrite an existing cohort. "
                 "Move it aside if you really mean to start over.")
    DATA.mkdir(exist_ok=True)
    rows: list[dict] = []
    claimed: set[str] = set()

    for stratum in STRATA:
        available = search.count(stratum["filter"])
        kept = 0
        for doc in search.iterate(stratum["filter"], limit=stratum["target"] * 2):
            if kept >= stratum["target"]:
                break
            if doc["id"] in claimed:  # keep strata disjoint so counts stay clean
                continue
            claimed.add(doc["id"])
            rows.append(_enrol(doc, stratum["name"]))
            kept += 1
        print(f"  {stratum['name']:22s} enrolled {kept:4d} of {available:7d} available")

    with BASELINE.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest = DATA / "cohort_manifest.json"
    manifest.write_text(
        json.dumps({"created": dt.date.today().isoformat(), "strata": STRATA}, indent=2),
        encoding="utf-8",
    )
    print(f"\nenrolled {len(rows)} items -> {BASELINE}")
    print(f"frozen predictions      -> {manifest}")
    print("\nRe-run `python loppan/cohort.py check` weekly. Do not edit the baseline.")


def check() -> None:
    if not BASELINE.exists():
        sys.exit("no cohort yet — run `snapshot` first")
    rows = [json.loads(line) for line in BASELINE.open(encoding="utf-8")]
    today = dt.date.today().isoformat()

    # Cheap pass: which are still in the search index at all?
    present: set[str] = set()
    ids = [r["item_id"] for r in rows]
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        hits = search.search(filter_by="id:[" + ",".join(chunk) + "]", per_page=100)["hits"]
        present.update(h["document"]["id"] for h in hits)

    vanished = [r for r in rows if r["item_id"] not in present]
    print(f"{len(rows)} tracked | {len(present)} still in index | {len(vanished)} vanished")

    # Expensive pass, only for the ones that left: ask Parse what happened.
    results = []
    for row in rows:
        out = {"item_id": row["item_id"], "stratum": row["stratum"], "checked_on": today}
        if row["item_id"] in present:
            out["outcome"] = "still_listed"
        else:
            try:
                item = sellpy.item(row["item_id"])
                status = item.get("itemStatus")
                out["status"] = status
                out["outcome"] = {"betald": "sold", "vilande": "expired"}.get(status, status)
                ladder = sellpy.ladder(row["item_id"])
                if ladder:
                    out["final_price"] = ladder[-1]["pricing"]["amount"]
                    out["rungs"] = len(ladder)
            except Exception as exc:
                out["outcome"] = f"error:{type(exc).__name__}"
        results.append(out)

    CHECKS.mkdir(exist_ok=True)
    path = CHECKS / f"check_{today}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for res in results:
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")

    print(f"\n{'stratum':24s} {'n':>5s} {'listed':>7s} {'sold':>6s} {'expired':>8s}")
    for stratum in STRATA:
        grp = [r for r in results if r["stratum"] == stratum["name"]]
        if not grp:
            continue
        counts = {k: sum(1 for r in grp if r["outcome"] == k)
                  for k in ("still_listed", "sold", "expired")}
        print(f"  {stratum['name']:22s} {len(grp):5d} {counts['still_listed']:7d} "
              f"{counts['sold']:6d} {counts['expired']:8d}")
    print(f"\nwrote {path}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"snapshot": snapshot, "check": check}.get(cmd, lambda: sys.exit(f"unknown: {cmd}"))()
