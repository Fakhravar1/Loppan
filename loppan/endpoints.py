"""Marketplace endpoints, supplied by the environment.

No hostname belongs in this repository. The crawler reads one specific
second-hand marketplace, and hardcoding its hosts would name the target in the
clear to anyone who opens the source — including through the file listing, long
before they read a line of it.

None of these values is secret: they are public endpoints served to every
browser that loads the site. That makes repository *variables* the right home
for them rather than secrets, and it means a missing one is a configuration
mistake, not a leak.

    LOPPAN_MARKET_API          Parse REST base, ending in /parse
    LOPPAN_MARKET_GRAPHQL      GraphQL endpoint on the same host
    LOPPAN_MARKET_SITE         public site origin, used to build item permalinks
    LOPPAN_MARKET_IMAGE_HOSTS  comma-separated host prefixes that item images
                               are served from. Parse hands out a private URL
                               that 403s while the search index hands out the
                               public CDN; the path after the host is identical,
                               so every prefix here is stripped to a bare path.

Resolution is lazy and cached. Reading these at import time would make an
unconfigured shell unable to so much as `import loppan.search`, which is a
miserable way to discover you forgot an environment variable — and it would
break the docs tooling, which imports for signatures and never makes a request.
Failing at the point of use instead puts the error next to the call that needed
it.
"""

from __future__ import annotations

import os

_cache: dict[str, object] = {}


class NotConfigured(RuntimeError):
    """An endpoint was needed and the environment did not supply it."""


def _clean(value: str | None) -> str | None:
    """Strip whitespace and a leading BOM.

    Same reasoning as db._clean: piping a value through PowerShell prepends a
    UTF-8 BOM, and dashboard copy-paste tends to bring a trailing newline.
    Neither should cost anyone an hour.
    """
    return value.strip().lstrip("﻿").strip() if value else value


def _require(name: str) -> str:
    if name not in _cache:
        value = _clean(os.environ.get(name))
        if not value:
            raise NotConfigured(
                f"{name} is not set. The marketplace endpoints are read from the "
                f"environment so that no hostname is committed to this repository; "
                f"see loppan/endpoints.py for the full list."
            )
        _cache[name] = value.rstrip("/")
    return _cache[name]  # type: ignore[return-value]


def api() -> str:
    """Parse REST base. Callers append the object path."""
    return _require("LOPPAN_MARKET_API")


def graphql() -> str:
    """GraphQL endpoint. Introspection is disabled server-side."""
    return _require("LOPPAN_MARKET_GRAPHQL")


def site() -> str:
    """Public site origin, without a trailing slash."""
    return _require("LOPPAN_MARKET_SITE")


def image_hosts() -> tuple[str, ...]:
    """Host prefixes to strip from image URLs, each ending in a slash.

    Order is not significant — image_paths() takes the first prefix that
    matches, and the hosts do not overlap.
    """
    if "_hosts" not in _cache:
        raw = _require("LOPPAN_MARKET_IMAGE_HOSTS")
        hosts = tuple(
            h.strip().rstrip("/") + "/" for h in raw.split(",") if h.strip()
        )
        if not hosts:
            raise NotConfigured(
                "LOPPAN_MARKET_IMAGE_HOSTS is set but lists no hosts."
            )
        _cache["_hosts"] = hosts
    return _cache["_hosts"]  # type: ignore[return-value]
