"""Read-only client for the Algolia index the Sellpy storefront actually browses.

Why this exists alongside `search.py`. The storefront queries Algolia
(`prod_marketItem_se_relevance`, ~12.5M documents). The Typesense collection that
`search.py` reads holds 586,746 — a ~5% subset, verified: only 6.8% of Algolia
items appear in Typesense, while 99.8% of Typesense items appear in Algolia.

What each source uniquely has:
  Algolia    — the whole market, `weight`, `priceDrop_SE.oldPrice`, regional
               favourite buckets, `firstOfferedAt_SE`
  Typesense  — `priceToEstimateRatio` and `sellabilityEstimate`, and nothing else
               carries those. Neither is in Parse either.

Traps worth knowing before using this:

  * **Filtering on an unconfigured attribute returns 0, it does not error.**
    `isOnShelf:true` silently matches nothing. Use `isForSale:true`.
  * **Only the unfiltered total is exhaustive.** Every filtered `nbHits` comes back
    with `exhaustiveNbHits: false` — they are estimates, sometimes badly off.
  * **`saleStartedAt` is not the listing date.** It is when the current price step
    began; median gap to `firstOfferedAt_SE` is 79 days.
  * **Sold items are deleted from the index.** Verified: 0 of 200 known-sold items
    remained, while 8 of 8 expired ones did. Disappearance is therefore a usable
    sale signal, but only for items seen beforehand.

The search key is the one every visitor's browser holds, scoped and search-only.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request

APP_ID = "M6WNFR0LVI"
SEARCH_KEY = "313e09c3b00b6e2da5dbe382cd1c8f4b"
INDEX = "prod_marketItem_se_relevance"

# Clothing and shoes, all three demographies. Deliberately excludes Accessoarer,
# Prylar and Skönhet — about 4% of the catalogue and not the trade.
WEARABLE = [
    "Man > Kläder", "Kvinna > Kläder", "Barn > Kläder",
    "Man > Skor", "Kvinna > Skor", "Barn > Skor",
]

# Algolia is third-party CDN infrastructure built for high query rates — the
# storefront itself fires several requests per page view — so a modest parallel read
# rate is unremarkable here. This is NOT the same judgement as `sellpy.py`, which
# talks to Sellpy's own Parse backend where the risk is the account, not the server,
# and which stays at one request per second and strictly serial.
MIN_INTERVAL_S = 0.05
MAX_WORKERS = 8
MAX_HITS_PER_PAGE = 1000  # hard ceiling in the API

# How many 100-item requests may be in flight at once. Deep enough that every
# worker always has work queued, shallow enough that the results in flight are a
# bounded cost rather than the whole catalogue. See get_objects_parallel.
WINDOW_CHUNKS = MAX_WORKERS * 4

_throttle_lock = threading.Lock()

TRANSIENT = (
    ConnectionError, TimeoutError,
    http.client.RemoteDisconnected, http.client.IncompleteRead,
    urllib.error.URLError,
)
RETRIES = 4
BACKOFF_S = 3

_last_call = 0.0


def _throttle() -> None:
    """Rate-limit across threads, not just within one."""
    global _last_call
    with _throttle_lock:
        wait = MIN_INTERVAL_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


HOST = f"{APP_ID}-dsn.algolia.net"

_conn_local = threading.local()


def _connection() -> tuple[http.client.HTTPSConnection, bool]:
    """This thread's HTTPS connection, created on first use.

    Thread-local rather than shared: `http.client` connections are not thread-safe and
    `get_objects_parallel` runs eight at once.

    Also returns whether the connection is FRESH, because the two failure modes deserve
    different treatment. A reused connection that the server has since dropped is
    routine — Algolia closes idle keep-alives — and should be retried immediately. A
    brand-new connection that fails is a real problem and should back off.
    """
    conn = getattr(_conn_local, "conn", None)
    if conn is not None:
        return conn, False
    conn = http.client.HTTPSConnection(HOST, timeout=30)
    _conn_local.conn = conn
    return conn, True


def _drop_connection() -> None:
    conn = getattr(_conn_local, "conn", None)
    _conn_local.conn = None
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


def _post(path: str, body: dict) -> dict:
    """POST to Algolia, reusing this thread's connection.

    Reuse is not a micro-optimisation. `urlopen` built a fresh TLS connection for every
    single request, and one pool sweep makes thousands across brands, size shapes and
    the price-split recursion. OpenSSL's per-connection buffers come from glibc malloc,
    which holds freed heap once fragmented — so a pass sat at ~264 MB RSS while the
    Python side of that same pass peaked at 16 MB and retained nothing (measured
    2026-08-11; docs/pi-runner.md). It also drops a TLS handshake per request, so this
    is faster as well as smaller.

    The body is read in full on every path, including errors. That is what makes the
    connection reusable — an unread response leaves bytes in the socket and the next
    request would read them as its own reply.
    """
    payload = json.dumps(body).encode()
    headers = {
        "X-Algolia-API-Key": SEARCH_KEY,
        "X-Algolia-Application-Id": APP_ID,
        "Content-Type": "application/json",
    }

    for attempt in range(RETRIES):
        _throttle()
        conn, fresh = _connection()
        try:
            conn.request("POST", f"/1/indexes/{path}", body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except (*TRANSIENT, http.client.HTTPException) as exc:
            # HTTPException covers the stale-connection family that urlopen never
            # produced because it never reused anything: CannotSendRequest,
            # ResponseNotReady, BadStatusLine.
            _drop_connection()
            if attempt == RETRIES - 1:
                raise
            if fresh:
                wait = BACKOFF_S * (2 ** attempt)
                print(f"  {type(exc).__name__} on {path}; retrying in {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
            continue

        if resp.status >= 400:
            # Raised as HTTPError so callers keep the type they already handle, and
            # with the body attached so `.read()` on it still works.
            _drop_connection()
            raise urllib.error.HTTPError(f"https://{HOST}/1/indexes/{path}", resp.status,
                                         resp.reason, resp.headers, io.BytesIO(data))
        return json.loads(data)


def search(filters: str = "", facet_filters: list | None = None,
           hits_per_page: int = 100, page: int = 0, **extra) -> dict:
    body = {"query": "", "hitsPerPage": min(hits_per_page, MAX_HITS_PER_PAGE), "page": page}
    if filters:
        body["filters"] = filters
    if facet_filters:
        body["facetFilters"] = facet_filters
    body.update(extra)
    return _post(f"{INDEX}/query", body)


def wearable_filter(min_price_kr: int = 100) -> tuple[str, list]:
    """The standing population filter: clothing or shoes, at or above a price floor."""
    return (f"price_SE.amount>={min_price_kr * 100}",
            [[f"categories.lvl1:{c}" for c in WEARABLE]])


def brand_facets(min_price_kr: int = 100, limit: int = 1000) -> dict[str, int]:
    """Brand -> listing count across the population. Capped at 1000 by the API,
    which covers ~59% of items; the remaining ~43,600 brands cannot be enumerated
    this way and have to be sampled by walking the population."""
    filters, facet_filters = wearable_filter(min_price_kr)
    r = search(filters=filters, facet_filters=facet_filters, hits_per_page=0,
               facets=["metadata.brand"], maxValuesPerFacet=limit)
    return (r.get("facets") or {}).get("metadata.brand", {})


def brand_items(brand: str, cap: int, min_price_kr: int = 100) -> list[dict]:
    """Up to `cap` items for one brand. A single request while cap <= 1000."""
    filters, facet_filters = wearable_filter(min_price_kr)
    ff = list(facet_filters) + [[f"metadata.brand:{brand}"]]
    out, seen, page = [], set(), 0
    while len(out) < cap:
        want = min(cap - len(out), MAX_HITS_PER_PAGE)
        hits = search(filters=filters, facet_filters=ff,
                      hits_per_page=want, page=page).get("hits", [])
        if not hits:
            break
        for h in hits:
            if h["objectID"] not in seen:
                seen.add(h["objectID"])
                out.append(h)
        page += 1
    return out


def get_objects(item_ids: list[str]) -> list[dict | None]:
    """Fetch by id, 100 per request. Returns None in place for anything missing —
    which is how a sale is detected, since sold items are removed from the index."""
    out: list[dict | None] = []
    for i in range(0, len(item_ids), 100):
        chunk = item_ids[i:i + 100]
        body = {"requests": [{"indexName": INDEX, "objectID": x} for x in chunk]}
        out.extend(_post("*/objects", body)["results"])
    return out


def _drain(done):
    """Yield each finished future's result, then let go of the future itself."""
    while done:
        fut = done.pop()
        try:
            yield fut.result()
        except Exception as exc:      # one bad chunk must not kill the sweep
            print(f"  chunk failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        del fut


def get_objects_parallel(item_ids: list[str], workers: int = MAX_WORKERS):
    """Same as get_objects, in parallel, yielding (chunk_ids, results) as they land.

    The work is entirely I/O-bound — every second is spent waiting on HTTP — so
    threads cost almost nothing and turn a serial hour into a few minutes.

    Submission is windowed, and each future is released as soon as its result is
    handed over. Both halves matter, and the second one is not optional: the
    obvious `futures = [pool.submit(...) for c in chunks]` keeps every Future alive
    for the lifetime of the pool, and a finished Future holds its result, so that
    one list silently pins every record the pass ever fetched. `as_completed` drops
    its own references but cannot drop the caller's.

    Measured 2026-08-08 over 668,961 items: 7.3 GB retained that way, 343 MB this
    way, and this way is marginally the faster of the two. Yielding per chunk was
    always the intent here — it just never actually freed anything.
    """
    chunks = (item_ids[i:i + 100] for i in range(0, len(item_ids), 100))

    def fetch(chunk):
        body = {"requests": [{"indexName": INDEX, "objectID": x} for x in chunk]}
        return chunk, _post("*/objects", body)["results"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending: set = set()
        for chunk in chunks:
            pending.add(pool.submit(fetch, chunk))
            if len(pending) >= WINDOW_CHUNKS:
                done, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                yield from _drain(done)
        while pending:
            done, pending = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED)
            yield from _drain(done)
