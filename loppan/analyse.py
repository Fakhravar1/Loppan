"""First look at ladder data: does anything visible at listing time predict outcome?

Important about what this can and cannot show. These are **Sellpy-side** outcomes —
what items cleared for on Sellpy, not what they would fetch on Circle. The whole
strategy rests on the gap between those two, and this file cannot see it.

What it *can* test is the detector: are there items Sellpy prices below what the
market will immediately pay? The fingerprint of that is an item selling **fast**
and with **little or no markdown** — nobody had a chance to decline it. That is
exactly the shape of the COS coat in the known trades (2 rungs, 13 days).
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def load() -> list[dict]:
    files = sorted(DATA.glob("ladders_*.jsonl"))
    if not files:
        sys.exit("no ladder file — run pull_ladders.py first")
    return [json.loads(line) for line in files[-1].open(encoding="utf-8")]


def band(opening: float | None) -> str:
    if opening is None:
        return "?"
    if opening >= 1500:
        return "high (>=1500)"
    if opening >= 400:
        return "mid (400-1499)"
    return "low (<400)"


def pct(part: int, whole: int) -> str:
    return f"{100*part/whole:.0f}%" if whole else "-"


def show(title: str) -> None:
    print(f"\n{title}\n{'-'*len(title)}")


def main() -> None:
    rows = load()
    done = [r for r in rows if r["ended_on"]]
    sold = [r for r in done if r["sold"]]

    print(f"{len(rows)} items | {len(done)} reached a terminal state | {len(sold)} sold")
    if not done:
        return

    show("Sellpy's own sell-through, by opening-ask band")
    print(f"{'band':16s} {'n':>5s} {'sold':>6s} {'rate':>6s} {'med rungs':>10s} {'med days':>9s} {'med decay':>10s}")
    for name in ("low (<400)", "mid (400-1499)", "high (>=1500)"):
        grp = [r for r in done if band(r["opening_ask"]) == name]
        if not grp:
            continue
        s = [r for r in grp if r["sold"]]
        rungs = statistics.median(r["rungs"] for r in grp)
        days = statistics.median(r["days_on_market"] for r in grp if r["days_on_market"] is not None)
        decay = statistics.median(r["decay"] for r in grp if r["decay"] is not None)
        print(f"{name:16s} {len(grp):5d} {len(s):6d} {pct(len(s),len(grp)):>6s} {rungs:10.0f} {days:9.0f} {decay:10.1%}")

    show("The underpricing fingerprint: sold fast, with little markdown")
    quick = [r for r in sold if (r["days_on_market"] or 999) <= 21 and r["rungs"] <= 2]
    slow = [r for r in sold if r["rungs"] >= 5]
    print(f"sold within 21 days and <=2 price steps : {len(quick)} ({pct(len(quick), len(sold))} of sales)")
    print(f"sold only after 5+ markdowns            : {len(slow)} ({pct(len(slow), len(sold))} of sales)")
    if quick:
        print(f"  fast-sale opening asks: median {statistics.median(r['opening_ask'] for r in quick):.0f} kr")
    if slow:
        print(f"  slow-sale opening asks: median {statistics.median(r['opening_ask'] for r in slow):.0f} kr")

    show("Does Sellpy's own sell-score predict whether it actually sold?")
    scored = [r for r in done if r["score"] is not None]
    if scored:
        ss = [r["score"] for r in scored if r["sold"]]
        ns = [r["score"] for r in scored if not r["sold"]]
        if ss:
            print(f"  sold     n={len(ss):4d}  mean score {statistics.mean(ss):.3f}")
        if ns:
            print(f"  not sold n={len(ns):4d}  mean score {statistics.mean(ns):.3f}")
        if ss and ns:
            gap = statistics.mean(ss) - statistics.mean(ns)
            verdict = "no signal" if abs(gap) < 0.03 else "the score carries real signal"
            print(f"  gap {gap:+.3f}  ({verdict} for Sellpy-side sell-through)")
            print("  NB: predicting whether Sellpy sells it is not the same as")
            print("  predicting whether it is a profitable buy. In the four known")
            print("  trades the two highest-scoring items were the two worst trades.")

    show("Disclosed defects")
    for label, grp in (("with defect", [r for r in done if r["has_defect"]]),
                       ("no defect", [r for r in done if not r["has_defect"]])):
        if grp:
            s = [r for r in grp if r["sold"]]
            decay = [r["decay"] for r in grp if r["decay"] is not None]
            print(f"  {label:12s} n={len(grp):4d} sold {pct(len(s),len(grp)):>4s} "
                  f"median decay {statistics.median(decay):.1%}")

    show("Warehouse dwell (assorted -> listed)")
    dw = [r for r in done if r["dwell_days"] is not None]
    if dw:
        for label, grp in (("dwell <30d", [r for r in dw if r["dwell_days"] < 30]),
                           ("dwell >=30d", [r for r in dw if r["dwell_days"] >= 30])):
            if grp:
                s = [r for r in grp if r["sold"]]
                print(f"  {label:12s} n={len(grp):4d} sold {pct(len(s),len(grp)):>4s} "
                      f"median opening {statistics.median(r['opening_ask'] for r in grp):.0f} kr")

    show("Caveat")
    print("These are Sellpy-side clearing outcomes. They test whether underpriced")
    print("items are detectable. They say nothing about the Circle resale premium,")
    print("which is where the four known trades made their money.")


if __name__ == "__main__":
    main()
