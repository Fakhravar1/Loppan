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
