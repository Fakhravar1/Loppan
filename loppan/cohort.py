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

from loppan import db, search, sellpy

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


STATUS_OUTCOME = {
    "utlagd": "still_listed",
    "såld": "sold",      # sold, payout to the seller still pending
    "betald": "sold",    # sold and paid out
    "vilande": "expired",  # dormant — listed and never sold
    # Sellpy donates what it cannot sell, so this is the terminal state of an
    # unsold item rather than a third kind of thing. Grouped with `vilande`
    # because every sell-through figure needs "left the market without selling",
    # and the raw `status` column keeps the distinction for anyone who wants it.
    #
    # It was previously unmapped and so fell through to "unknown", which is NOT
    # terminal — 26 items were being re-fetched from Parse on every run, forever,
    # and could never resolve. That is the cost of leaving a known status unmapped.
    "skänkt": "expired",
}
TERMINAL = {"sold", "expired"}


def _day(value):
    if isinstance(value, dict):
        value = value.get("iso")
    return value[:10] if value else None


def _path(ladder: list[dict]) -> dict:
    """Flatten an item's markdown history into the fields analysis needs."""
    if not ladder:
        return {}
    opening = ladder[0]["pricing"]["amount"]
    final = ladder[-1]["pricing"]["amount"]
    listed_on, ended_on = _day(ladder[0]["createdAt"]), _day(ladder[-1].get("endedAt"))
    days = None
    if listed_on and ended_on:
        days = (dt.date.fromisoformat(ended_on) - dt.date.fromisoformat(listed_on)).days
    return {
        "opening_ask": opening,
        "final_price": final,
        "rungs": len(ladder),
        "decay": round(1 - final / opening, 3) if opening else None,
        "listed_on": listed_on,
        "ended_on": ended_on,
        "days_on_market": days,
        "ladder": [
            {"price": o["pricing"]["amount"], "from": _day(o["createdAt"]), "to": _day(o.get("endedAt"))}
            for o in ladder
        ],
    }


def _load_baseline() -> list[dict]:
    """Postgres first, local file as fallback.

    Reading the cohort from the database is what lets this run on a stateless CI
    runner, which has no `data/` directory and never will.
    """
    if db.configured():
        try:
            rows = db.query("cohort_items?select=item_id,stratum&limit=100000")
            if rows:
                return rows
        except Exception as exc:
            print(f"  could not read cohort from Postgres ({exc}); trying local", file=sys.stderr)
    if BASELINE.exists():
        return [json.loads(line) for line in BASELINE.open(encoding="utf-8")]
    sys.exit("no cohort found in Postgres or locally — run `snapshot` first")


def _already_resolved() -> dict[str, str]:
    """Items whose fate is already known.

    Sold and expired are both terminal, so re-checking them burns requests and
    can never change the answer. The job therefore gets *cheaper* every run
    instead of more expensive, and finishes when the last item resolves.
    """
    resolved: dict[str, str] = {}
    if db.configured():
        try:
            rows = db.query(
                "cohort_checks?select=item_id,outcome&outcome=in.(sold,expired)&limit=100000"
            )
            resolved.update({r["item_id"]: r["outcome"] for r in rows})
        except Exception as exc:
            print(f"  could not read outcomes from Postgres ({exc})", file=sys.stderr)
    for path in sorted(CHECKS.glob("check_*.jsonl")):
        for line in path.open(encoding="utf-8"):
            row = json.loads(line)
            if row.get("outcome") in TERMINAL:
                resolved[row["item_id"]] = row["outcome"]
    return resolved


def check() -> None:
    all_rows = _load_baseline()
    today = dt.date.today().isoformat()

    resolved = _already_resolved()
    rows = [r for r in all_rows if r["item_id"] not in resolved]
    if resolved:
        print(f"skipping {len(resolved)} already-resolved items")
    if not rows:
        print("every item has resolved — the cohort is complete.")
        return

    # Cheap pass: which are still in the search index at all?
    present: set[str] = set()
    ids = [r["item_id"] for r in rows]
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        hits = search.search(filter_by="id:[" + ",".join(chunk) + "]", per_page=100)["hits"]
        present.update(h["document"]["id"] for h in hits)

    vanished = [r for r in rows if r["item_id"] not in present]
    print(f"{len(rows)} open | {len(present)} still listed | {len(vanished)} newly vanished")

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
                # 'såld' is sold with payout pending; 'betald' is sold and paid
                # out. Both are sales. Anything unrecognised is flagged rather
                # than guessed — a new status has already appeared once.
                out["outcome"] = STATUS_OUTCOME.get(status, "unknown")
                # Capture the whole markdown path, not just where it ended.
                # "Sold at 200 kr" and "sold at 200 kr after four price cuts"
                # mean opposite things, and this is the last moment the path is
                # readable.
                out.update(_path(sellpy.ladder(row["item_id"])))
            except Exception as exc:
                out["outcome"] = f"error:{type(exc).__name__}"
        results.append(out)

    CHECKS.mkdir(parents=True, exist_ok=True)  # data/ does not exist on a fresh runner
    path = CHECKS / f"check_{today}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for res in results:
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")

    # Postgres is the durable copy; the JSONL above is the local fallback so a
    # missing key never costs a week of observations.
    if db.configured():
        rows = [
            {
                "item_id": r["item_id"],
                "checked_on": r["checked_on"],
                "outcome": "error" if str(r["outcome"]).startswith("error") else r["outcome"],
                "status": r.get("status"),
                **{k: r.get(k) for k in (
                    "opening_ask", "final_price", "rungs", "decay",
                    "listed_on", "ended_on", "days_on_market", "ladder")},
            }
            for r in results
        ]
        try:
            print(f"  synced {db.upsert('cohort_checks', rows, 'item_id,checked_on')} rows to Postgres")
        except Exception as exc:
            print(f"  Postgres sync FAILED ({exc}) — local file is intact, "
                  f"re-run `python loppan/load_to_db.py` once fixed", file=sys.stderr)
    else:
        print("  (LOPPAN_SUPABASE_KEY not set — saved locally only)", file=sys.stderr)

    # Cumulative view: this run's outcomes plus everything resolved previously.
    by_item = {r["item_id"]: r["stratum"] for r in all_rows}
    cumulative: dict[str, str] = dict(resolved)
    cumulative.update({r["item_id"]: r["outcome"] for r in results})

    print(f"\n{'stratum':24s} {'n':>5s} {'listed':>7s} {'sold':>6s} {'expired':>8s} {'sold %':>7s}")
    for stratum in STRATA:
        ids_in = [i for i, s in by_item.items() if s == stratum["name"]]
        counts = {
            k: sum(1 for i in ids_in if cumulative.get(i) == k)
            for k in ("still_listed", "sold", "expired")
        }
        done = counts["sold"] + counts["expired"]
        rate = f"{100*counts['sold']/done:.0f}%" if done else "-"
        print(f"  {stratum['name']:22s} {len(ids_in):5d} {counts['still_listed']:7d} "
              f"{counts['sold']:6d} {counts['expired']:8d} {rate:>7s}")
    print(f"\nwrote {path}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"snapshot": snapshot, "check": check}.get(cmd, lambda: sys.exit(f"unknown: {cmd}"))()
