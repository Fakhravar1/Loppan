"""Parse reads that settle what happened to an item, with no database attached.

Two questions only the marketplace's own backend can answer:

  adjudicate_detailed  an item left the search index, or stopped being for sale.
                       Was it sold, did it expire, or is it still listed?
                       `itemStatus` is authoritative; disappearance is only a hint.
  origin_of            a Circle listing: what did its seller pay the marketplace for
                       the item, and how far had it been marked down by then?

These used to live in track.py, cohort.py and backfill_item_origins.py. All three
import db.py (Supabase) at module level, and the BigQuery path (bq_fetch.py) must
not touch Supabase at all. So the logic moved here and those modules import it
back: `track.adjudicate`, `cohort.STATUS_OUTCOME` and
`backfill_item_origins.origin_of` still exist and behave as before, apart from the
rounding fix noted at `_ore`.

Conduct. Every call goes through the Parse client's own throttle, so there is one
request in flight at its MIN_INTERVAL_S. Nothing here may ever run in a worker
pool: docs/api-notes.md, "Conduct".
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# The Parse client. Its endpoints come from the environment (see endpoints.py).
from loppan import market as parse

ADJUDICATE = 60      # MarketOffer $in ceiling, verified

# itemStatus -> outcome. The single mapping: compare strings anywhere else and a
# status gets read two ways.
STATUS_OUTCOME = {
    "utlagd": "still_listed",
    "såld": "sold",        # sold, payout to the seller still pending
    "betald": "sold",      # sold and paid out
    "vilande": "expired",  # dormant — listed and never sold
    # The marketplace donates what it cannot sell, so this is the terminal state of
    # an unsold item rather than a third kind of thing. Grouped with `vilande`
    # because every sell-through figure needs "left the market without selling".
    #
    # It was previously unmapped and so fell through to "unknown", which is NOT
    # terminal — 26 items were being re-fetched from Parse on every run, forever,
    # and could never resolve. That is the cost of leaving a known status unmapped.
    "skänkt": "expired",
}

# A Circle origin, as backfill_item_origins.py writes it. PostgREST rejects a batch
# whose objects do not all carry the same keys, so every row starts from this shape
# with explicit nulls.
FIELDS = ("item_id", "original_id", "bought_price_ore", "bought_on",
          "original_opening_ore", "original_rungs", "bought_discount")


def _day(value):
    if isinstance(value, dict):
        value = value.get("iso")
    return value[:10] if value else None


def _ore(kr):
    """Parse quotes kronor. Everything downstream of here is öre.

    Rounded, not truncated. track.adjudicate used to write `int(price * 100)`, which
    turns a kronor price such as 4.35 into 434 öre, because 4.35 * 100 is
    434.99999999999994 in floating point.
    """
    return int(round(kr * 100)) if kr is not None else None


def adjudicate_detailed(item_ids: list[str]):
    """Ask Parse what happened to each id, 60 ids per request, strictly serial.

    Returns `(verdicts, failed)`:

      verdicts  item_id -> (verdict, final_price_ore, raw itemStatus). The verdict is
                still_listed | sold | expired | unknown. `unknown` is a status this
                module does not recognise, deliberately not guessed.
      failed    ids whose request raised. They have NO answer, which is not the
                same as an answer of `unknown`: a caller must leave them alone and
                ask again next run.

    An id in neither has no latest SE offer at all. Parse could not account for it.
    """
    verdicts: dict[str, tuple[str, int | None, str | None]] = {}
    failed: list[str] = []
    for i in range(0, len(item_ids), ADJUDICATE):
        chunk = item_ids[i:i + ADJUDICATE]
        pointers = [{"__type": "Pointer", "className": "Item", "objectId": x} for x in chunk]
        try:
            offers = parse.find(
                "MarketOffer",
                {"item": {"$in": pointers}, "region": "SE", "latest": True},
                limit=200, include="item")
        except Exception as exc:
            print(f"  adjudication batch {i}: {type(exc).__name__}", file=sys.stderr)
            failed.extend(chunk)
            continue
        for offer in offers:
            item = offer.get("item") or {}
            item_id = item.get("objectId")
            if not item_id:
                continue
            status = item.get("itemStatus")
            price = (offer.get("pricing") or {}).get("amount")
            verdicts[item_id] = (STATUS_OUTCOME.get(status, "unknown"),
                                 _ore(price) if price else None, status)
    return verdicts, failed


def origin_of(circle_id: str) -> dict | None:
    """What the seller paid, and how marked-down the item was when they bought.

    A Circle listing points back, via `preceding`, to the item its seller bought
    from the marketplace. The purchase price lives on that original listing, and
    nothing guarantees it stays reachable, so collect it while the original exists.

    Returns None when the listing carries no `preceding` pointer at all — that is a
    Circle item whose purchase side is simply not recorded, not a failure. Two Parse
    requests per linked item, one for an unlinked one.
    """
    circle = parse.item(circle_id)
    preceding = circle.get("preceding")
    if not preceding:
        return None

    row = dict.fromkeys(FIELDS)
    row["item_id"] = circle_id
    row["original_id"] = preceding["objectId"]

    ladder = parse.ladder(row["original_id"])
    if not ladder:
        return row  # linked, but the original's price history is gone

    opening = ladder[0]["pricing"]["amount"]
    paid = ladder[-1]["pricing"]["amount"]
    row.update({
        "bought_price_ore": _ore(paid),
        "bought_on": _day(ladder[-1].get("endedAt")),
        "original_opening_ore": _ore(opening),
        "original_rungs": len(ladder),
        "bought_discount": round(1 - paid / opening, 3) if opening else None,
    })
    return row
