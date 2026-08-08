"""Postgres writes via PostgREST. Stdlib only — no new dependencies.

Credentials come from the environment and are never stored in this repo:

    LOPPAN_SUPABASE_URL   https://zgqywowejxtokqsybqnu.supabase.co
    LOPPAN_SUPABASE_KEY   the service-role key, from the Supabase dashboard
                          (Project Settings -> API Keys -> service_role)

The service-role key bypasses row-level security, which is exactly what these
backend scripts need and exactly why it must never reach a browser, a commit, or
a log line. Every table has RLS enabled with no policies, so the publishable key
can read and write nothing.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

PROJECT_REF = "zgqywowejxtokqsybqnu"
DEFAULT_URL = f"https://{PROJECT_REF}.supabase.co"
BATCH = 500
RPC_TIMEOUT = 300  # seconds; the slowest analytics function measures ~56 s


class NotConfigured(RuntimeError):
    pass


def _clean(value: str | None) -> str | None:
    """Strip whitespace and a leading BOM.

    Piping a secret through PowerShell prepends a UTF-8 BOM, which then cannot be
    encoded into an HTTP header and fails with a latin-1 codec error that says
    nothing about the real cause. Copy-paste via a dashboard likewise tends to
    bring a trailing newline. Neither should cost anyone an hour.
    """
    return value.strip().lstrip("﻿").strip() if value else value


def _creds() -> tuple[str, str]:
    url = _clean(os.environ.get("LOPPAN_SUPABASE_URL")) or DEFAULT_URL
    url = url.rstrip("/")
    key = _clean(os.environ.get("LOPPAN_SUPABASE_KEY"))
    if not key:
        raise NotConfigured(
            "LOPPAN_SUPABASE_KEY is not set.\n"
            "  Get the service_role key from the Supabase dashboard:\n"
            f"    https://supabase.com/dashboard/project/{PROJECT_REF}/settings/api-keys\n"
            "  Then, in PowerShell:\n"
            '    $env:LOPPAN_SUPABASE_KEY = "<the key>"\n'
            "  Add it to your user environment variables to make it stick."
        )
    return url, key


def configured() -> bool:
    return bool(os.environ.get("LOPPAN_SUPABASE_KEY"))


def upsert(table: str, rows: list[dict], on_conflict: str | None = None) -> int:
    """Insert rows, updating any that already exist. Chunked to keep requests sane."""
    if not rows:
        return 0
    url, key = _creds()
    written = 0

    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        endpoint = f"{url}/rest/v1/{table}"
        if on_conflict:
            endpoint += f"?on_conflict={on_conflict}"
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(chunk, ensure_ascii=False).encode(),
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"{table}: HTTP {exc.code} — {exc.read().decode()[:300]}"
            ) from exc
        written += len(chunk)

    return written


PAGE = 1000


def update(table: str, rows: list[dict], key: str) -> int:
    """PATCH existing rows, one request each.

    Use this, not `upsert`, when filling in a few columns on rows that already
    exist. PostgREST's upsert constructs a complete insert tuple and validates it
    before resolving the conflict, so any NOT NULL column missing from the payload
    fails the whole batch — even though the row is already there and the insert
    will never happen. PATCH updates only the columns supplied, and cannot
    accidentally create a row.
    """
    if not rows:
        return 0
    url, apikey = _creds()
    done = 0

    for row in rows:
        payload = {k: v for k, v in row.items() if k != key}
        if not payload:
            continue
        req = urllib.request.Request(
            f"{url}/rest/v1/{table}?{key}=eq.{row[key]}",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "apikey": apikey,
                "Authorization": f"Bearer {apikey}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"{table} update {row[key]}: HTTP {exc.code} — {exc.read().decode()[:300]}"
            ) from exc
        done += 1

    return done


def query(path: str, paginate: bool = True) -> list[dict]:
    """Read back via PostgREST, e.g. query('v_cohort_summary?select=*').

    PostgREST caps a response at 1000 rows regardless of any `limit` in the query
    string, and returns the truncated page without complaining. That silently cost
    us 300 of 1300 cohort items once, so reads page through Range headers by
    default rather than trusting a single response to be complete.
    """
    url, key = _creds()
    rows: list[dict] = []
    offset = 0

    while True:
        req = urllib.request.Request(
            f"{url}/rest/v1/{path}",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Range-Unit": "items",
                "Range": f"{offset}-{offset + PAGE - 1}",
            },
        )
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        rows += page
        if not paginate or len(page) < PAGE:
            return rows
        offset += PAGE

def query_pages(path: str, key: str = "item_id", size: int = PAGE,
                after: str | None = None):
    """Yield a read one page at a time, instead of accumulating the whole result.

    Use this over `query` for anything catalogue-sized. `query` materialises every
    row before the caller sees the first one — for the 669k live items that is
    288 MB of Python dicts (452 B/row) that then has to stay resident for the whole
    pass. Paging keeps peak memory proportional to a page.

    Paginates by seeking past the last key read, NOT by Range offsets, for two
    reasons. Offsets make the database re-walk and discard everything it has
    already returned, which over 669 pages is quadratic. More importantly they are
    not stable: PostgREST adds no ORDER BY of its own, Postgres promises no row
    order without one, and `enrol` can be writing to `items` while a long `track`
    pass is still reading it — so a row can shift between pages and be silently
    skipped or returned twice. Ordering by the key and seeking makes each page
    independent of what happened to the pages before it.

    `key` must appear in the select list, and be unique. `after` starts the walk
    partway through, which is what makes an interrupted read resumable.
    """
    url, apikey = _creds()
    sep = "&" if "?" in path else "?"
    last = after

    while True:
        q = f"{path}{sep}order={key}.asc&limit={size}"
        if last is not None:
            q += f"&{key}=gt.{last}"
        req = urllib.request.Request(
            f"{url}/rest/v1/{q}",
            headers={"apikey": apikey, "Authorization": f"Bearer {apikey}"},
        )
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        if not page:
            return
        last = page[-1][key]
        yield page
        if len(page) < size:
            return


def delete(path: str) -> None:
    """DELETE rows matching a PostgREST filter, e.g. delete('track_progress?id=eq.1').

    The filter is required by PostgREST itself — an unfiltered DELETE is rejected
    rather than silently emptying the table — but pass one deliberately anyway.
    """
    url, key = _creds()
    req = urllib.request.Request(
        f"{url}/rest/v1/{path}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Prefer": "return=minimal",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"delete {path}: HTTP {exc.code} — {exc.read().decode()[:300]}"
        ) from exc


def count(path: str) -> int:
    """How many rows a read would return, without transferring any of them."""
    url, key = _creds()
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(
        f"{url}/rest/v1/{path}{sep}limit=1",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Range-Unit": "items",
            "Range": "0-0",
            "Prefer": "count=exact",
        },
        method="HEAD",
    )
    with urllib.request.urlopen(req) as resp:
        content_range = resp.headers.get("Content-Range") or ""
    return int(content_range.split("/")[-1]) if "/" in content_range else 0


def rpc(name: str, params: dict | None = None, timeout: int = RPC_TIMEOUT):
    """Call a Postgres function. Aggregates belong in the database, not in a
    round trip that pulls 84,000 rows out just to average them.

    The analytics functions are the long ones — measured 2026-08-08 against 669k
    items: refresh_peer_prices ~56 s, snapshot_predictors ~39 s, snapshot_brands
    ~10 s. All three set their own statement_timeout server-side; `timeout` here
    guards the socket, so a gateway that accepts the POST and then goes quiet
    fails in minutes instead of holding the job open for its full 300.

    A socket timeout does NOT mean the work did not happen — the statement keeps
    running server-side and usually finishes. All three functions replace their
    own day's rows rather than appending, so the safe response is to re-run.
    """
    url, key = _creds()
    req = urllib.request.Request(
        f"{url}/rest/v1/rpc/{name}",
        data=json.dumps(params or {}).encode(),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"rpc {name}: HTTP {exc.code} — {exc.read().decode()[:300]}") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"rpc {name}: no response within {timeout}s. The statement may still be "
            f"running server-side; re-running is safe."
        ) from exc
