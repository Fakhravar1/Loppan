"""Refresh price and liveness across the whole pool, every day.

    python loppan/pool_refresh.py
    python loppan/pool_refresh.py --dry-run

The rotation sweeps a quarter of the brands a day, so a row can be up to four days old
by the time its bucket comes round again. Two of the things on it go stale far faster
than that:

  price        Sellpy marks down roughly 11% every 10 days, so a four-day-old price is
               visibly wrong and the discount computed from it is wrong with it.
  still_listed sold items vanish from the search index within the day. A pool that does
               not notice is a pool of things you cannot buy.

Both are cheap to fix for the entire pool at once — ~39,000 items is ~390 Algolia
requests — so they are refreshed daily regardless of which bucket is being swept.

**The peer median is deliberately NOT refreshed here.** Recomputing it would need the
whole peer group again, which is exactly the expensive thing the rotation avoids. It is
as old as its bucket, and that is the trade the rotation makes: a current price compared
against a median up to four days old. Medians over dozens of listings move slowly, so
this is the right way round — but it does mean `discount_pct` is a fresh numerator over
a slightly stale denominator, and nothing here pretends otherwise.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, db, search

BATCH = 500


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    dry = "--dry-run" in sys.argv

    # rank comes along because it is NOT NULL: PostgREST builds a complete insert tuple
    # for an upsert and validates it before resolving the conflict, so omitting it fails
    # the whole batch even though the row already exists and no insert will happen.
    #
    # `as_of` is the other NOT NULL column and was missed here until 2026-08-11, so this
    # script had never once completed: every run died on
    # `23502 ... Failing row contains (null, <item_id>, 1, ...)`. Nobody saw it because
    # the sweep step ahead of it was failing first and this one only ever ran as the
    # `if: always()` tail of an already-broken job. It is supplied in the payload below
    # rather than selected here, because it must be TODAY rather than whatever the row
    # currently holds.
    rows = db.query("shortlist_daily?select=item_id,rank,peer_median_kr,price_kr")
    if not rows:
        sys.exit("pool is empty — has a bucket been swept yet?")
    by_id = {r["item_id"]: r for r in rows}
    print(f"refreshing {len(by_id):,} pool items")
    if dry:
        return

    today = dt.date.today().isoformat()
    gone = repriced = 0

    for chunk_ids, docs in algolia.get_objects_parallel(list(by_id)):
        payload = []
        for item_id, doc in zip(chunk_ids, docs):
            row = by_id[item_id]
            live = doc is not None
            if not live:
                gone += 1

            price_kr = row["price_kr"]
            if live:
                amount = (doc.get("price_SE") or {}).get("amount")
                if amount:
                    new_kr = amount // 100
                    if new_kr != row["price_kr"]:
                        repriced += 1
                    price_kr = new_kr

            med = row["peer_median_kr"]
            payload.append({
                "item_id": item_id,
                # Today, not the sweep date. `as_of` is how current this row's data is,
                # and price and still_listed below were just re-read. `swept_on` is the
                # separate column carrying the peer group's vintage, and it is
                # deliberately NOT touched here -- next_sweep_bucket() rotates on
                # max(swept_on), so bumping it would make every bucket look freshly
                # swept and the rotation would stop advancing.
                "as_of": today,
                "rank": row["rank"],
                "price_kr": price_kr,
                # Recomputed so the headline number matches the price beside it. The
                # median is whatever the last sweep of this bucket found.
                "discount_pct": (round((1 - price_kr / med) * 100, 2)
                                 if med and price_kr is not None and med > 0 else None),
                "still_listed": live,
                "image_checked_on": today,
                "image_paths": (search.image_paths(doc.get("images")) or None) if live else None,
            })
        db.upsert("shortlist_daily", payload, on_conflict="item_id")

    print(f"  {repriced:,} changed price")
    print(f"  {gone:,} no longer in the search index — most of those sold")


if __name__ == "__main__":
    main()
