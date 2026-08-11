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
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db

# The date every snapshot is stamped with. Passed explicitly rather than left to
# each function's `current_date` default so that all three agree even if the run
# crosses midnight UTC — otherwise a pass starting at 23:59 writes peer freezes
# under one date and predictors under the next, and the join between them silently
# misses a day.
AS_OF = object()   # placeholder, swapped for the run's date in main()

# How a step can be checked to have landed even though its call did not come back.
#
# Supabase's API gateway cuts a request at 60 s and the statement keeps running and
# commits regardless, so a long RPC reports failure for work that succeeded. That is not
# hypothetical: on 2026-08-11 `snapshot_predictors` returned RemoteDisconnected while
# writing all 40 of its rows, for both targets, correctly.
#
# So for steps that can outlast the gateway, ask the database what happened instead of
# believing the socket. Only steps with an unambiguous side effect for the day belong
# here — all of these replace their own day's rows, so "a row exists for p_as_of" is a
# true completion signal rather than a guess.
SETTLED = {
    "snapshot_predictors": "predictor_daily?select=as_of&as_of=eq.{as_of}",
    "snapshot_brands":     "brand_daily?select=as_of&as_of=eq.{as_of}",
}

SETTLE_WAIT_S = 150      # a touch over the longest of these, measured ~60 s
SETTLE_POLL_S = 10

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


def _settled(name: str, as_of: str) -> bool:
    """Did this step's work land, despite the call not coming back?

    Polls rather than asking once: the gateway hangs up at 60 s while the statement is
    still running, so the rows appear some seconds AFTER the failure is reported. Asking
    immediately would say no and be wrong.

    A verification failure answers False — if the database cannot be reached to check,
    the honest reading is "unknown", and unknown belongs in the failed list where a human
    will look at it.
    """
    path = SETTLED.get(name)
    if not path:
        return False

    deadline = time.monotonic() + SETTLE_WAIT_S
    while True:
        try:
            if db.count(path.format(as_of=as_of)) > 0:
                return True
        except RuntimeError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(SETTLE_POLL_S)


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
            # Before believing it: a step that outlasts the gateway reports failure for
            # work that is still running and will commit. Ask the database.
            if _settled(name, as_of):
                secs = (dt.datetime.now() - started).total_seconds()
                print(f"  {label}: call dropped after {secs:.0f}s, but the rows for "
                      f"{as_of} are there — treating as done", flush=True)
                continue

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
