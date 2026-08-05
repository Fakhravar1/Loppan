"""Typesense search client — the surface Parse could not give us.

Parse can only be enumerated about 9,000 rows deep and cannot filter on price,
brand or date. This index can do all three across the whole live catalogue
(~584k documents), and carries several fields that exist nowhere in the Parse
objects — see docs/api-notes.md.

The search key is fetched at runtime from the same GraphQL call the website makes.
It is a scoped, search-only, client-side key: every visitor's browser holds it.
It is deliberately NOT hardcoded here, so if Sellpy rotates it this keeps working.

Note on conduct: robots.txt disallows Sellpy's /search paths. This is the API the
site itself calls rather than those pages, but the spirit is close enough that the
same restraint applies — modest volumes, one request per second, no redistribution.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPHQL = "https://sellpy-parse-prod.herokuapp.com/graphql"
APP_ID = "3ebgwo1hPV0sk74fnWBTSW3RIxgw3b2ZAxM6qmCj"
JS_KEY = "hRVEXFeMQX8fB18ODYI9UvtlLkliB43qeaqUht3f"
COLLECTION = "market_items"
MIN_INTERVAL_S = 1.0

_config: dict | None = None
_last_call = 0.0

_CONFIG_QUERY = """query getTypesenseClientConfig {
  getTypesenseClientConfig {
    nodes { host port protocol }
    nearestNode { host port protocol }
    searchApiKey
  }
}"""


def config() -> dict:
    """Fetch (once) the search host and scoped search-only key."""
    global _config
    if _config:
        return _config
    req = urllib.request.Request(
        GRAPHQL,
        data=json.dumps({"query": _CONFIG_QUERY, "variables": {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Parse-Application-Id": APP_ID,
            "X-Parse-Javascript-Key": JS_KEY,
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    cfg = (data.get("data") or {}).get("getTypesenseClientConfig")
    if not cfg or not cfg.get("searchApiKey"):
        raise RuntimeError(f"no search config returned: {json.dumps(data)[:200]}")
    _config = cfg
    return cfg


def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL_S - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def search(filter_by: str = "", per_page: int = 100, page: int = 1, **params) -> dict:
    cfg = config()
    node = cfg.get("nearestNode") or cfg["nodes"][0]
    query = {"q": "*", "per_page": per_page, "page": page, **params}
    if filter_by:
        query["filter_by"] = filter_by
    url = (
        f"{node['protocol']}://{node['host']}/collections/{COLLECTION}"
        f"/documents/search?{urllib.parse.urlencode(query)}"
    )
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": cfg["searchApiKey"]})
    _throttle()
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"search failed: {exc.read().decode()[:200]}") from exc


def count(filter_by: str) -> int:
    return search(filter_by=filter_by, per_page=0)["found"]


def iterate(filter_by: str, limit: int = 1000, per_page: int = 100):
    """Page through matches. Deep pagination works here, unlike Parse."""
    got = 0
    page = 1
    while got < limit:
        hits = search(filter_by=filter_by, per_page=min(per_page, limit - got), page=page).get("hits", [])
        if not hits:
            return
        for hit in hits:
            yield hit["document"]
            got += 1
        page += 1


def price_kr(doc: dict, region: str = "SE") -> float | None:
    """price_SE.amount is in ÖRE. Forgetting that gives answers 100x too big."""
    block = doc.get(f"price_{region}")
    return block["amount"] / 100 if block else None


IMAGE_HOSTS = ("https://prod.images.sellpy.net/",
               "https://sellpy-parse-prod-files.s3.amazonaws.com/",
               "https://sellpy-parse-prod-files.s3.eu-west-1.amazonaws.com/")


def image_paths(urls) -> list[str]:
    """Strip the host, keep the path.

    Parse serves a private S3 URL that 403s; the search index serves the public
    CDN. The path after the host is identical, so storing only the path keeps
    both sources consistent and survives a host change.
    """
    out = []
    for url in urls or []:
        for host in IMAGE_HOSTS:
            if url.startswith(host):
                url = url[len(host):]
                break
        out.append(url)
    return out


def summarise(doc: dict) -> dict:
    shared = doc.get("sharedMetadata") or {}
    translated = doc.get("translatedMetadata_sv") or {}
    brand_class = doc.get("brandClassification") or {}
    cats = doc.get("categories_sv") or {}
    deepest = next(
        (cats[f"lvl{i}"][0] for i in (3, 2, 1, 0) if cats.get(f"lvl{i}")), None
    )
    return {
        "item_id": doc["id"],
        "url": f"https://www.sellpy.se/item/{doc['id']}",
        "brand": shared.get("brand"),
        "type": translated.get("type"),
        # Sellpy tags season natively, so the seasonality idea needs no
        # inference. Present on roughly a fifth of items; null means untagged,
        # not all-season.
        "season": translated.get("season"),
        # Fibre content, raw. Wool/cashmere/silk hold value in ways polyester
        # does not, so this is a candidate signal — stored unprocessed so the
        # definition of "premium" can change without re-collecting.
        "materials": translated.get("material") or doc.get("materials_sv"),
        "category": deepest,
        # Coded and multi-valued: WMN-INT-M, WMN-EU-38, SHOES-EU-40, PANTS-INCH-30,
        # and the literal "NO SIZE". Present on every document sampled, and absent
        # from translatedMetadata_sv — the Parse Item has metadata.size, but that
        # would cost one request per item. Stored raw; decode at read time.
        "sizes": doc.get("sizes"),
        "image_paths": image_paths(doc.get("images")),
        "demography": translated.get("demography"),
        "condition": translated.get("condition"),
        "has_defect": bool(translated.get("defects")),
        "price_kr": price_kr(doc),
        # Sellpy's own price-vs-value ratio. Below 1 means the current asking
        # price sits under their own estimate for the item.
        "price_to_estimate": doc.get("priceToEstimateRatio"),
        "favourites": doc.get("favouriteCount"),
        "brand_tier": brand_class.get("pricePoint"),
        "last_chance": doc.get("lastChance"),
        "is_circle": doc.get("p2p"),
        "on_shelf": doc.get("isOnShelf"),
        "reserved": doc.get("isReserved"),
        "sale_started_at": doc.get("saleStartedAt"),
    }
