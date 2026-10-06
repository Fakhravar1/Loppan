"""Supabase writes via PostgREST, for the shortlist export (bq_export.py). Stdlib only.

Credentials come from the environment and are never stored in this repo:

    LOPPAN_SUPABASE_URL   https://zgqywowejxtokqsybqnu.supabase.co
    LOPPAN_SUPABASE_KEY   the service-role key, from the Supabase dashboard
                          (Project Settings -> API Keys -> service_role)

The service-role key bypasses row-level security, which is exactly what the export
needs and exactly why it must never reach a browser, a commit, or a log line.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.error
import urllib.parse
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




def _keepalive(sock) -> None:
    """Keep a long, silent request's TCP flow warm.

    Cheap insurance against a middlebox dropping an idle flow: an analytics RPC runs
    for a minute or more with no packets in either direction, and probing every 15 s
    makes it non-idle. The options after SO_KEEPALIVE are Linux-specific, hence the
    hasattr guard.

    ⚠️ **This is not what fixes `refresh_peer_prices`, despite being added for it.**
    The reasoning at the time was that the tunnel's NAT was expiring the flow, since
    the RPC succeeded on 08-08 and 08-09 and failed on every run from 08-10, the day
    the VPN went up. That correlation was a coincidence. Timed directly against the
    database on 2026-08-11 the function takes **99 s** and scores 647,051 rows, up
    from the ~56 s recorded on 08-08 — and Supabase's API gateway cuts a request at
    **60 s**. It crossed that line at about the moment the tunnel appeared.

    No client-side setting extends a gateway's request limit, so keepalives cannot
    help and nor could a longer `timeout` here. The call has to stop needing more than
    60 s of held-open HTTP: make the statement faster, split it, or run it detached.
    Kept because it is harmless and genuinely does protect the other long RPCs from
    idle-flow drops — but do not read its presence as evidence the problem was network.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, value in (("TCP_KEEPIDLE", 15), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 8)):
        if hasattr(socket, opt):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)


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

    Uses http.client directly rather than urlopen, only so the socket can be reached
    to set keepalives on it before the request goes out — see `_keepalive`.
    """
    url, key = _creds()
    host = urllib.parse.urlsplit(url).netloc
    payload = json.dumps(params or {}).encode()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.connect()
        _keepalive(conn.sock)
        conn.request("POST", f"/rest/v1/rpc/{name}", body=payload, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode()
        if resp.status >= 400:
            raise RuntimeError(f"rpc {name}: HTTP {resp.status} — {body[:300]}")
        return json.loads(body) if body else None
    except TimeoutError as exc:
        # Must precede the OSError clause below, which would otherwise swallow it --
        # TimeoutError is an OSError subclass, and this message is the more useful one.
        raise RuntimeError(
            f"rpc {name}: no response within {timeout}s. The statement may still be "
            f"running server-side; re-running is safe."
        ) from exc
    except (OSError, http.client.HTTPException) as exc:
        # Everything that is not an HTTP *response*: a dropped socket, a refused or
        # reset connection, DNS failure. These used to escape as their own types, which
        # meant callers that deliberately catch RuntimeError to carry on --
        # analytics.py runs three independent snapshots -- aborted on the first one
        # instead. A RemoteDisconnected on refresh_peer_prices cost the brand and
        # predictor snapshots for 2026-08-10 and 08-11 that way.
        #
        # Note the wording: this says the CALL failed, not that the work did not
        # happen. Per the docstring above, the statement usually keeps running and
        # commits server-side, so the response to one of these is to re-run, not to
        # assume the day is missing.
        raise RuntimeError(f"rpc {name}: {type(exc).__name__} — {exc} "
                           f"(the call failed; the statement may still have committed)") from exc
    finally:
        conn.close()


