"""Minimal read-only client for Sellpy's public Parse backend.

The application id and javascript key below are the browser SDK's own keys, served
in plain text inside https://www.sellpy.se/market/index.*.bundle.js. They are
public-by-design client credentials, not secrets. See docs/api-notes.md for the
map of which query shapes the server will actually accept.

Read-only by construction: this module never issues a write, and nothing here
authenticates as a user.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "https://sellpy-parse-prod.herokuapp.com/parse"
APP_ID = "3ebgwo1hPV0sk74fnWBTSW3RIxgw3b2ZAxM6qmCj"
JS_KEY = "hRVEXFeMQX8fB18ODYI9UvtlLkliB43qeaqUht3f"

# The server kills any query that runs longer than ~10 s with a bare HTTP 500.
# A 500 here almost always means "unindexed query", not "broken request".
SERVER_TIMEOUT_S = 10

# Be a good guest. One request per second is plenty for research volumes and
# keeps us far away from anything that looks like scraping pressure.
MIN_INTERVAL_S = 1.0

_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL_S - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _post(path: str, body: dict) -> dict:
    payload = dict(body)
    payload.update(
        {
            "_method": "GET",
            "_ApplicationId": APP_ID,
            "_JavaScriptKey": JS_KEY,
            "_ClientVersion": "js4.3.1",
        }
    )
    req = urllib.request.Request(
        f"{BASE}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    _throttle()
    with urllib.request.urlopen(req, timeout=SERVER_TIMEOUT_S + 20) as resp:
        return json.load(resp)


class QueryTooSlow(RuntimeError):
    """The server returned 500, which in practice means the query was unindexed."""


def get(cls: str, object_id: str) -> dict:
    """Fetch one object by id. Works on Item, ItemCategory, ItemType, MarketOffer."""
    return _post(f"classes/{cls}/{object_id}", {})


def find(cls: str, where: dict, limit: int = 100, skip: int = 0, **kwargs) -> list[dict]:
    """Run a constrained query.

    Raises QueryTooSlow on the server's 10 s timeout so callers can back off to a
    smaller page or a cheaper query shape rather than silently returning nothing.
    """
    body = {"where": where, "limit": limit}
    if skip:
        body["skip"] = skip
    body.update(kwargs)
    try:
        return _post(f"classes/{cls}", body).get("results", [])
    except urllib.error.HTTPError as exc:
        if exc.code == 500:
            raise QueryTooSlow(
                f"{cls} query timed out (limit={limit}, skip={skip}, where={where}). "
                "The constrained field is probably unindexed — see docs/api-notes.md."
            ) from exc
        raise


def item(object_id: str) -> dict:
    return get("Item", object_id)


def ladder(item_id: str, region: str = "SE") -> list[dict]:
    """Every price step Sellpy ever set for one item, oldest first.

    This is the whole point of the project: the markdown history is retained and
    readable long after the item sold, so decay curves can be reconstructed
    retrospectively instead of polled for.
    """
    offers = find(
        "MarketOffer",
        {
            "item": {"__type": "Pointer", "className": "Item", "objectId": item_id},
            "region": region,
        },
        limit=200,
        order="createdAt",
    )
    return sorted(offers, key=lambda o: o["createdAt"])


def sample_latest_offers(n: int, region: str = "SE", page: int = 1000) -> list[dict]:
    """Sample the most recent offer of many items.

    Deliberately unordered. Ordering this query makes the server time out, and the
    unordered result is effectively arbitrary with respect to price and brand —
    which is exactly what a cohort sample wants. Selecting on taste would make the
    cohort measure taste.

    Note the population: `latest=True` means the item's final/current offer, so
    this mixes still-listed items with ones that already ended. Split on `endedAt`.
    """
    out: list[dict] = []
    seen: set[str] = set()
    skip = 0
    while len(out) < n and skip < 9000:
        batch = find(
            "MarketOffer",
            {"region": region, "latest": True},
            limit=min(page, n - len(out)),
            skip=skip,
        )
        if not batch:
            break
        for offer in batch:
            if offer["objectId"] not in seen:
                seen.add(offer["objectId"])
                out.append(offer)
        skip += page
    return out
