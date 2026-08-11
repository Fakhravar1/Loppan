"""Rebuild the analytics layer after a track pass. Three functions, in this order.

    python loppan/analytics.py
    python loppan/analytics.py --as-of 2026-08-08

The order is not cosmetic and the whole thing has to run *after* track.py:

  1. refresh_peer_prices()   Freezes the peer position of everything that resolved
                             this pass onto items.peer_*_frozen, THEN truncates and
                             rebuilds the live shelf. Truncating first would throw
                             away the only record of where a sold item stood, which
                             is what it used to do — 0 of 15,616 resolved items had
                             a peer row on 2026-08-08.
  2. snapshot_predictors()   Reads that frozen position as one of its features, so
                             it has to come after (1), not alongside it.
  3. snapshot_brands()       Independent of (1) and (2); last because it is cheapest
                             and there is no reason for it to hold up the others.

All three replace their own day's rows rather than appending, so re-running after a
failure is safe and is the intended response to a timeout.

Measured 2026-08-08 over 669k items: 56 s + 39 s + 10 s, about 1 min 45 s in total.
That is small against the ~27 min pass it follows, but it is not free — if it starts
mattering, snapshot_brands is the one to move to a slower cadence, because brand
aggregates drift far more slowly than a daily hazard does.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db

# The date every snapshot is stamped with. Passed explicitly rather than left to
# each function's `current_date` default so that all three agree even if the run
# crosses midnight UTC — otherwise a pass starting at 23:59 writes peer freezes
# under one date and predictors under the next, and the join between them silently
# misses a day.
AS_OF = object()   # placeholder, swapped for the run's date in main()

# name, params, unit, chain
#
# The peer rebuild is five calls rather than one because `refresh_peer_prices()` grew to
# 99 s and Supabase's API gateway cuts a request at 60 s -- so the single call failed
# every time while committing anyway. Measured after the split, worst step 14.1 s.
#
# `chain` is not decoration. The peer steps are strictly ordered and destructive:
# stage_peer_live() truncates peer_prices, and the levels each place only what the
# previous level could not. A later step running after an earlier one failed would
# either destroy the freeze or score against stale staging, so a failure inside a chain
# skips the rest of it. The snapshots have no chain: they are genuinely independent,
# which is the whole reason this script carries on past failures at all.
#
# The database enforces both orderings too -- stage refuses while unfrozen resolved rows
# exist, the levels refuse against staging older than 30 minutes. Belt and braces on
# purpose: this is the one place in the project where getting the order wrong destroys
# data that cannot be recovered.
STEPS = [
    ("freeze_peer_prices",  {},                 "resolved items frozen",   "peer"),
    ("stage_peer_live",     {},                 "live rows staged",        "peer"),
    ("score_peer_level",    {"p_level": 1},     "items scored at level 1", "peer"),
    ("score_peer_level",    {"p_level": 2},     "items scored at level 2", "peer"),
    ("score_peer_level",    {"p_level": 3},     "items scored at level 3", "peer"),
    ("snapshot_predictors", {"p_as_of": AS_OF}, "feature rows written",    None),
    ("snapshot_brands",     {"p_as_of": AS_OF}, "brands snapshotted",      None),
]


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")

    as_of = (sys.argv[sys.argv.index("--as-of") + 1] if "--as-of" in sys.argv
             else dt.date.today().isoformat())
    print(f"analytics layer for {as_of}")

    failed = []
    broken_chains = set()
    for name, params, unit, chain in STEPS:
        label = f"{name}({params['p_level']})" if "p_level" in params else name

        if chain in broken_chains:
            print(f"  {label}: SKIPPED — an earlier '{chain}' step failed and the rest "
                  f"of that chain would work on half-built state", file=sys.stderr)
            failed.append(label)
            continue

        call = {k: (as_of if v is AS_OF else v) for k, v in params.items()}
        started = dt.datetime.now()
        try:
            result = db.rpc(name, call or None)
        except RuntimeError as exc:
            # Carry on rather than aborting: the snapshots are independent enough that
            # losing the brand snapshot is no reason to also lose the predictors,
            # and a half-built layer is easier to reason about than one that stops
            # at a different step every time something is slow.
            #
            # Steps in a chain are the exception — see STEPS.
            print(f"  {label}: FAILED — {exc}", file=sys.stderr)
            failed.append(label)
            if chain:
                broken_chains.add(chain)
            continue
        secs = (dt.datetime.now() - started).total_seconds()
        print(f"  {label}: {result:,} {unit} in {secs:.0f}s", flush=True)

    if failed:
        sys.exit(f"{len(failed)} of {len(STEPS)} steps failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
