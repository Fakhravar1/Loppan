"""Print the PROGRESS block from progress_daily rows (docs/bigquery.md §5, step 10).

    bq query --format=json 'select * from loppan.progress_daily
                            where run_date > date_sub(RUN, interval 7 day) ...' \
      | python progress_block.py RUN

Reads bq's JSON (every value a string, NULLs null or absent) on stdin: up to seven
rows, the last of them RUN. Prints plain lines between two fixed markers, so a run log
can be cut down to the block with sed. Standard library only.
"""
import json
import sys

run = sys.argv[1]
text = sys.stdin.read().strip()
print(f"===== PROGRESS {run} =====")
try:
    rows = json.loads(text) if text else []
except ValueError:   # bq printed an error, not JSON: still close the block
    print(f"unreadable query output: {text[:200]}")
    rows = []
rows = sorted((r for r in rows if r["run_date"] <= run), key=lambda r: r["run_date"])
t = next((r for r in rows if r["run_date"] == run), None)


def num(key, row=None):
    v = (row or t).get(key)
    return "-" if v in (None, "") else f"{int(float(v)):,}"


def dec(key, places=3):
    v = t.get(key)
    return "-" if v in (None, "") else f"{float(v):.{places}f}"


def compact(v):
    if v in (None, ""):
        return "-"
    v = int(float(v))
    return f"{v / 1e6:.3f}M" if v >= 1_000_000 else f"{v:,}"


if t is None:
    print(f"no progress_daily row for {run}: progress.sql did not run or failed")
else:
    lines = [
        ("live items", f"{num('live_items')}  (enrolled today {num('enrolled_today')},"
                       f" items ever {num('items_ever')})"),
        ("sold today", f"{num('sold_today')}  (total {num('sold_total')}; expired"
                       f" {num('expired_today')}, below floor {num('below_floor_today')},"
                       f" unknown {num('unknown_today')})"),
        ("price drops today", f"{num('price_drops_today')}  (like changes"
                              f" {num('fav_changes_today')})"),
        ("new found", f"{num('new_found')}  (completeness {dec('completeness', 4)})"),
        ("model", f"{num('combos_ge20')} brand x category with >= 20 sales  (>= 1:"
                  f" {num('combos_ge1')}; categories priced {num('categories_priced')})"),
        ("shortlist", f"{num('shortlist_now')} now, {num('shortlist_season')} season"),
        ("accuracy 7d", f"price / expected {dec('accuracy_median_ratio')}, n"
                        f" {num('accuracy_n')}  (< 20 sales: {dec('accuracy_thin_ratio')}"
                        f" n {num('accuracy_thin_n')}; >= 20: {dec('accuracy_thick_ratio')}"
                        f" n {num('accuracy_thick_n')})"),
        ("circle origins", f"{num('circle_with_origin')} with,"
                           f" {num('circle_without_origin')} without"),
        ("storage", f"{dec('storage_gib')} GiB"),
        ("billed today", f"{dec('billed_gib_today')} GiB"),
    ]
    for label, value in lines:
        print(f"{label + ':':<19}{value}")
    span = f"{rows[0]['run_date'][5:]}..{rows[-1]['run_date'][5:]}"
    live = " ".join(compact(r.get("live_items")) for r in rows)
    sold = " ".join(num("sold_today", r) for r in rows)
    print(f"{'trend 7d:':<19}{span}  live {live}  |  sold/day {sold}")
print("===== END PROGRESS =====")
