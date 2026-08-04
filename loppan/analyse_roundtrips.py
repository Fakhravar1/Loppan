"""What do real Circle resellers actually achieve?

The four known trades suggested buying items that had NOT been marked down much
produced ~5x returns, while items bought at the bottom of a long markdown ladder
produced 1.4-2.0x. That was four hand-picked trades by one person. This tests the
same relationship on strangers' round trips, where nothing was selected after the
fact and the failures are visible.
"""

from __future__ import annotations

import glob
import json
import pathlib
import statistics
import sys

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
KEEP_SHARE = 0.84


def load() -> list[dict]:
    files = sorted(glob.glob(str(DATA / "roundtrips_*.jsonl")))
    if not files:
        sys.exit("no roundtrip file — run circle_roundtrips.py first")
    return [json.loads(line) for line in open(files[-1], encoding="utf-8")]


def med(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def head(title):
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> None:
    rows = load()
    sold = [r for r in rows if r["sold"]]

    print(f"{len(rows)} completed Circle round trips | {len(sold)} sold "
          f"({100*len(sold)/len(rows):.0f}%)")

    head("Did the resale actually pay?")
    if sold:
        mult = [r["multiple"] for r in sold if r["multiple"]]
        profit = [r["profit"] for r in sold if r["profit"] is not None]
        wins = [p for p in profit if p > 0]
        print(f"  median multiple (sale ÷ purchase) : {med(mult):.2f}x")
        print(f"  median profit at k={KEEP_SHARE}          : {med(profit):+.0f} kr")
        print(f"  profitable after Sellpy's cut     : {len(wins)}/{len(profit)} "
              f"({100*len(wins)/len(profit):.0f}%)")
        print(f"  median purchase price             : {med([r['bought_price'] for r in sold]):.0f} kr")
        print(f"  median days held before relisting : {med([r['days_held'] for r in sold]):.0f}")
        print(f"  median days on Circle             : {med([r['days_on_circle'] for r in sold]):.0f}")
        print(f"\n  A multiple below {1/KEEP_SHARE:.2f}x loses money after the cut.")
        losers = [m for m in mult if m < 1 / KEEP_SHARE]
        print(f"  Round trips below that line: {len(losers)}/{len(mult)} "
              f"({100*len(losers)/len(mult):.0f}%)")

    head("The core question: does buying a heavily-discounted item hurt the multiple?")
    print("  (grouped by how far the item had been marked down when the reseller bought)")
    print(f"  {'discount at purchase':22s} {'n':>4s} {'med multiple':>13s} {'sold':>6s} {'med paid':>9s}")
    for label, lo, hi in (("barely marked down <20%", 0.0, 0.2),
                          ("moderate 20-50%", 0.2, 0.5),
                          ("deep 50-75%", 0.5, 0.75),
                          ("bottom of ladder >75%", 0.75, 1.01)):
        grp = [r for r in rows if r["bought_discount"] is not None
               and lo <= r["bought_discount"] < hi]
        if not grp:
            continue
        grp_sold = [r for r in grp if r["sold"]]
        m = med([r["multiple"] for r in grp_sold])
        print(f"  {label:22s} {len(grp):4d} {(f'{m:.2f}x' if m else '-'):>13s} "
              f"{100*len(grp_sold)/len(grp):5.0f}% {med([r['bought_price'] for r in grp]):8.0f} kr")

    head("By purchase price — is the cheap-and-fast pattern real?")
    print(f"  {'paid':16s} {'n':>4s} {'med multiple':>13s} {'sold':>6s} {'med profit':>11s}")
    for label, lo, hi in (("under 100 kr", 0, 100), ("100-299 kr", 100, 300),
                          ("300-799 kr", 300, 800), ("800 kr+", 800, 10**9)):
        grp = [r for r in rows if lo <= r["bought_price"] < hi]
        if not grp:
            continue
        grp_sold = [r for r in grp if r["sold"]]
        m = med([r["multiple"] for r in grp_sold])
        p = med([r["profit"] for r in grp_sold])
        print(f"  {label:16s} {len(grp):4d} {(f'{m:.2f}x' if m else '-'):>13s} "
              f"{100*len(grp_sold)/len(grp):5.0f}% {(f'{p:+.0f} kr' if p is not None else '-'):>11s}")

    head("How aggressively do resellers price, and does it work?")
    markup = [(r["circle_ask"] / r["bought_price"], r["sold"]) for r in rows if r["bought_price"]]
    for label, lo, hi in (("asked <2x paid", 0, 2), ("2-4x", 2, 4), ("4-8x", 4, 8), ("8x+", 8, 10**9)):
        grp = [(m, s) for m, s in markup if lo <= m < hi]
        if grp:
            print(f"  {label:16s} n={len(grp):4d}  sold {100*sum(s for _, s in grp)/len(grp):3.0f}%")

    head("Did they have to discount on Circle?")
    for label, grp in (("never repriced", [r for r in rows if r["circle_rungs"] == 1]),
                       ("repriced 2+ times", [r for r in rows if r["circle_rungs"] > 1])):
        if grp:
            s = [r for r in grp if r["sold"]]
            print(f"  {label:20s} n={len(grp):4d}  sold {100*len(s)/len(grp):3.0f}%  "
                  f"med multiple {med([r['multiple'] for r in s]) or 0:.2f}x")

    head("Caveats")
    print("  These are other people's trades, so their pricing skill is baked in.")
    print("  Unsold Circle listings that are STILL on the shelf are excluded by")
    print("  construction, so the sold rate here is optimistic.")


if __name__ == "__main__":
    main()
