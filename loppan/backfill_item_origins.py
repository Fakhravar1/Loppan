"""Record what each tracked Circle seller PAID, for the whole `items` population.

The sibling `backfill_circle_origin.py` does this for the 500-item cohort stratum.
This does it for the ~3,700 Circle items in `items`, which is where the outcomes
actually accumulate: the tracker had already recorded 15 Circle sales before any of
them had an origin, so each one was a complete round trip that could only be priced
by going back to Parse and hoping the original was still there.

That hope is the reason this job is time-sensitive. A Circle listing points back, via
`preceding`, to the item its seller originally bought from the marketplace. `track.py` will
tell us what the item finally fetched — but the purchase price lives on the *original*
listing, and nothing guarantees that stays reachable. Capturing the link while the
item is still live turns "did it sell" into "paid P, sold for S, so the multiple was
S/P". Sell-through alone cannot say whether the trade is profitable.

Break-even is a gross multiple of 1/0.84 = 1.19x, since the marketplace keeps 16%.

⚠️ Units. Parse returns kronor; `items` and `circle_origins` are in ÖRE. The
conversion happens here, on the way in. This is why the function below is not shared
with `backfill_circle_origin.py`, which writes kronor into `cohort_items` — the two
targets genuinely differ, and quietly unifying them would corrupt one of them.

Two Parse requests per item, one request per second: ~2 hours for the full population.
Resumable — every origin is written to the cache the moment it is known.

    python loppan/backfill_item_origins.py
    python loppan/backfill_item_origins.py --limit 200
    python loppan/backfill_item_origins.py --interval 0.25   # 4 req/s, still serial
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import db, market
# The origin lookup itself (two Parse requests per item) lives in outcomes.py, which
# imports no database, so the BigQuery path shares it. FIELDS is the row shape:
# PostgREST rejects a batch whose objects do not all carry the same keys ("All object
# keys must match"), so every row is built with explicit nulls rather than omissions.
from loppan.outcomes import FIELDS, origin_of  # noqa: F401  (FIELDS kept for callers)

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
CACHE = DATA / "item_origins.jsonl"
FLUSH_EVERY = 100   # rows buffered before a write, i.e. ~3 minutes of collection


def _cached() -> dict[str, dict]:
    """Origins already fetched, keyed by Circle item id.

    Fetching costs two the marketplace requests per item and roughly two hours for the whole
    population. A database error should not make us pay that again.
    """
    if not CACHE.exists():
        return {}
    return {
        row["item_id"]: row
        for row in (json.loads(line) for line in CACHE.open(encoding="utf-8"))
    }


def targets() -> list[str]:
    """Circle items with no origin yet, RESOLVED ONES FIRST.

    Ordering is the whole point. An item that has already sold is a round trip that
    completes the moment its origin lands, and its original listing is the oldest and
    so the likeliest to have become unreachable. Live items are future round trips and
    can wait for the next run; a resolved one that decays is gone for good.

    Both reads go through `query_pages`, not `query`. Both cross the 1000-row page
    boundary, and `query` pages by Range offset over a result PostgREST never ordered
    — so rows shift between pages and come back twice or not at all. The first run of
    this job returned 25 ids for 23 distinct items, which is the harmless half of that
    bug; the silent half is an item that is never handed back and so never collected.
    """
    have = {r["item_id"] for page in db.query_pages("circle_origins?select=item_id")
            for r in page}
    resolved, live = [], []
    for page in db.query_pages("items?select=item_id,outcome&p2p=is.true"):
        for r in page:
            if r["item_id"] in have:
                continue
            (resolved if r["outcome"] is not None else live).append(r["item_id"])
    print(f"{len(resolved):,} resolved and {len(live):,} live Circle items need an origin")
    return resolved + live


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

    # Scoped to this job on purpose. `market.MIN_INTERVAL_S` is a module global, so
    # editing the default would also re-tune track.py's adjudication and the cohort
    # checks — jobs that were sized against 1 req/s and are nowhere near this hot.
    #
    # Measured 2026-08-09: a Parse request costs ~0.24 s of actual latency against a
    # 1.0 s minimum interval, so ~76% of a pass at the default is deliberate waiting.
    # Lowering the interval speeds it up roughly proportionally until latency binds
    # at ~0.25 s. Note this stays STRICTLY SERIAL either way — one request in flight,
    # never a worker pool, which is the "no distributed crawling" line in
    # docs/api-notes.md and is not what this flag relaxes.
    if "--interval" in sys.argv:
        market.MIN_INTERVAL_S = float(sys.argv[sys.argv.index("--interval") + 1])
    print(f"Marketplace interval: {market.MIN_INTERVAL_S}s "
          f"(~{1/market.MIN_INTERVAL_S:.1f} req/s, serial)" if market.MIN_INTERVAL_S
          else "Marketplace interval: unthrottled")

    todo = targets()
    if not todo:
        print("nothing to backfill — every tracked Circle item already has its origin")
        return

    cache = _cached()
    fetch = [i for i in todo if i not in cache]
    if limit:
        fetch = fetch[:limit]
    print(f"{len(cache):,} already fetched | {len(fetch):,} to pull from the marketplace "
          f"(~{max(1, len(fetch) * 2 // 60)} min at 1 req/s)")

    DATA.mkdir(parents=True, exist_ok=True)
    missing = failed = written = 0
    pending: list[dict] = []
    landed: set[str] = set()   # ids this run has actually got into Postgres

    with CACHE.open("a", encoding="utf-8") as fh:
        for n, item_id in enumerate(fetch, 1):
            try:
                origin = origin_of(item_id)
            except Exception as exc:
                failed += 1
                print(f"  {item_id}: {type(exc).__name__}", file=sys.stderr)
                continue
            if origin is None:
                missing += 1  # a Circle listing with no preceding pointer
                continue
            fh.write(json.dumps(origin, ensure_ascii=False) + "\n")
            fh.flush()
            cache[item_id] = origin
            pending.append(origin)
            # Land rows in Postgres as we go. A full pass is ~8 hours at one request
            # per second, and holding every row until the end would mean eight hours
            # of collected data sitting in a file, invisible to the dashboard and to
            # anyone asking whether the job is working. The jsonl cache still governs
            # resumability; this only decides how fresh the database is.
            if len(pending) >= FLUSH_EVERY:
                written += db.upsert("circle_origins", pending, "item_id")
                landed.update(r["item_id"] for r in pending)
                pending = []
            if n % 50 == 0:
                print(f"  fetched {n:,}/{len(fetch):,}, {written:,} written",
                      file=sys.stderr)

    if pending:
        written += db.upsert("circle_origins", pending, "item_id")
        landed.update(r["item_id"] for r in pending)

    # Rows collected by an EARLIER interrupted run sit in the cache but may never have
    # reached Postgres. Reconcile those, and only those — everything this run wrote is
    # already there, and re-upserting it would mean a redundant ~14,700-row write at
    # the end of every full pass.
    wanted = set(todo)
    stragglers = [v for k, v in cache.items() if k in wanted and k not in landed]
    if stragglers:
        print(f"\nreconciling {len(stragglers):,} rows from an earlier run...")
        db.upsert("circle_origins", stragglers, "item_id")

    print(f"\ndone. {written:,} written | no preceding pointer: {missing} | "
          f"fetch errors: {failed}")
    print("check:  select outcome, count(*) from public.v_tracked_roundtrips group by 1;")


if __name__ == "__main__":
    main()
