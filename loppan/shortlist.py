"""Build the undervalued shortlist, then attach its pictures.

    python loppan/shortlist.py
    python loppan/shortlist.py --as-of 2026-08-10 --n 800

Two steps, and the second is the only reason this is a script rather than one RPC:

  1. `refresh_shortlist()` ranks live items on how cheap they are against their own
     peer group and writes the day's rows. Everything the dashboard sorts on is
     stored by that function, because ranking 693k items live costs a seq scan and
     a sort — 3.0 s measured, against the anon role's ~3 s statement timeout.

  2. Images, which are **not in the database at all**. `images` was dropped in the
     v2 rehaul as the largest per-row cost, and `schema.md`'s claim that it is
     "reconstructible from `item_id`" is wrong: the path carries a photo-station
     folder (`photoRobot-case-14-k-8`) and a random hex suffix, neither of which is
     derivable. Algolia holds the real URLs, so they are fetched here — for the
     shortlist only. That is ~5 requests for 500 items against ~7,000 to re-image
     the whole shelf, and ~0.3 MB stored against ~190 MB.

Absence from Algolia is itself a measurement rather than a failure: sold items are
deleted from the index within the day, so anything that comes back missing is
recorded as `still_listed = false` instead of being dropped. Re-running this over an
earlier `--as-of` is therefore a cheap way to ask what became of that day's picks.

Ordering: run it **after** `analytics.py`. The shortlist reads `peer_prices`, which
`refresh_peer_prices()` truncates and rebuilds every pass, and `brand_daily`, which
`snapshot_brands()` writes. Running first would rank against yesterday's shelf.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, db, search


def _arg(flag: str, default: str | None = None) -> str | None:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def attach_images(as_of: str) -> tuple[int, int]:
    """Fill image_paths for the day's rows. Returns (with_images, gone).

    Parallel because the pool is now the whole eligible set rather than a top-N
    sample: ~37,000 items is ~370 requests, which is a couple of minutes serially and
    seconds across the pool `algolia.py` already runs. Results are written in chunks
    as they land rather than accumulated, so peak memory stays proportional to a
    chunk — the same discipline `track.py` had to learn when it grew to 7.3 GB.
    """
    rows = db.query(f"shortlist_daily?select=item_id,rank&as_of=eq.{as_of}")
    if not rows:
        return 0, 0

    ranks = {r["item_id"]: r["rank"] for r in rows}
    ids = list(ranks)
    today = dt.date.today().isoformat()
    with_images = gone = 0

    for chunk_ids, docs in algolia.get_objects_parallel(ids):
        payload = []
        for item_id, doc in zip(chunk_ids, docs):
            paths = search.image_paths(doc.get("images")) if doc else []
            if doc is None:
                gone += 1
            if paths:
                with_images += 1
            # Every row carries an identical key set. PostgREST unions the keys across
            # a batch and fills the gaps with null, so a row that omitted `rank` here
            # would null out a NOT NULL column for the whole upsert.
            payload.append({
                "as_of": as_of,
                "item_id": item_id,
                "rank": ranks[item_id],
                "image_paths": paths or None,
                "image_checked_on": today,
                "still_listed": doc is not None,
            })
        db.upsert("shortlist_daily", payload, on_conflict="as_of,item_id")

    return with_images, gone


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")

    as_of = _arg("--as-of") or dt.date.today().isoformat()
    print(f"shortlist for {as_of}")

    # No row cap any more: the function writes every eligible item and records the top
    # slice separately in `shortlist_flagged`. A cap here would put the dashboard back
    # to filtering a sample of a sample.
    written = db.rpc("refresh_shortlist", {"p_as_of": as_of})
    print(f"  refresh_shortlist: {written:,} items in the pool")
    if not written:
        sys.exit("  nothing shortlisted — has analytics.py run? peer_prices may be empty")

    with_images, gone = attach_images(as_of)
    print(f"  images: {with_images:,} of {written:,} have a picture")
    if gone:
        # Not an error. It is the sale signal, and on a same-day run it should be
        # close to zero — a large number means the shelf moved under us, or that
        # this is a re-run over an older day.
        print(f"  {gone:,} no longer in the search index — recorded as not listed")


if __name__ == "__main__":
    main()
