"""Inputs for the brand rule (docs/bigquery.md §12), read from the search index.

Two passes, both count-only (no hits are downloaded):

1. Live listings per brand. One facet query over the whole scope is an estimate
   (`facetsCount: false` above ~150k hits; wrong by up to 2.3x), so the scope is
   split on `createdAt` until every part counts exhaustively AND lists fewer than
   1,000 brands, and the parts are summed. A part whose facet list is full has cut
   brands off its end, so it is split again: every brand's total is then exact.
2. Median live ask per brand, from the `price_SE.amount` facet filtered to the brand:
   the facet values are the prices themselves, so one request gives the whole
   distribution. `brand_tier` is the commonest `brandClassification.pricePoint`
   among a few hits; the field is not facetable.

The unbranded placeholder. There is none: an unbranded item has no `metadata.brand`
key at all, in the index and in Parse (checked 2026-10-02), so it never appears in
the brand facet and cannot rank on frequency. In BigQuery it is `brand IS NULL`.
"""

from __future__ import annotations

import concurrent.futures as cf
import statistics
import time
from collections import Counter

from loppan import algolia, bq_shapes

FACET_MAX = 1000


class BrandPartition(bq_shapes.Crawl):
    """Leaves are shapes small enough that every brand's count in them is exact: the
    hit count and the facet counts came back exhaustive, AND the brand facet list is
    shorter than FACET_MAX, so no brand was cut off the end of it. A leaf that meets
    only the first two still undercounts every brand ranked past 1,000 in it, which
    is most of the ~40k brands, so it is split again like any other."""

    def probe(self, bounds, est):
        r = self._search(bounds, hits_per_page=0, facets=["metadata.brand"],
                         maxValuesPerFacet=FACET_MAX)
        facets = (r.get("facets") or {}).get("metadata.brand", {})
        counted = bq_shapes.exhaustive(r)
        exact = counted and facets_exhaustive(r) and len(facets) < FACET_MAX
        if not exact and self._splittable(bounds):
            return "split", r["nbHits"], counted
        return "leaf", [facets], r["nbHits"], exact

    def leaf_size(self, hits, n) -> int:
        return n


def facets_exhaustive(r: dict) -> bool:
    return bool((r.get("exhaustive") or {}).get(
        "facetsCount", r.get("exhaustiveFacetsCount", False)))


def facet_value(v: str) -> str:
    """A leading '-' in a facet filter means NOT; escape it."""
    return "\\" + v if v.startswith("-") else v


def brand_counts(scope: list, base: str = "isForSale:true") -> dict:
    counts: Counter = Counter()
    state = {"truncated": 0, "branded": 0}

    def on_leaf(payload):
        facets = payload[0]
        counts.update(facets)
        state["branded"] += sum(facets.values())
        state["truncated"] += len(facets) >= FACET_MAX

    crawl = BrandPartition(base, scope, [bq_shapes.Dim("createdAt", 0, bq_shapes.FAR)], [])
    report = crawl.run(on_leaf)
    report.update(live_total=report.pop("hits_read"), facet_branded_total=state["branded"],
                  truncated_leaves=state["truncated"], brands_seen=len(counts))
    return {"counts": counts, "report": report}


def price_profile(brand: str, scope: list, base: str = "isForSale:true") -> dict:
    r = algolia.search(filters=base, facet_filters=scope + [[f"metadata.brand:{facet_value(brand)}"]],
                       hits_per_page=10, attributesToRetrieve=["brandClassification.pricePoint"],
                       facets=["price_SE.amount"], maxValuesPerFacet=FACET_MAX,
                       **bq_shapes.QUIET)
    dist = {int(float(k)): v for k, v in
            ((r.get("facets") or {}).get("price_SE.amount") or {}).items()}
    seen = sum(dist.values())
    median = None
    if seen:
        half, run = seen / 2, 0
        for price in sorted(dist):
            run += dist[price]
            if run >= half:
                median = price
                break
    tiers = [t for h in r.get("hits", [])
             if isinstance(t := (h.get("brandClassification") or {}).get("pricePoint"), int)]
    return {"listings_in_query": r["nbHits"], "median_ask_ore": median,
            "price_coverage": round(seen / r["nbHits"], 4) if r["nbHits"] else None,
            "prices_exhaustive": bool((r.get("exhaustive") or {}).get("facetsCount")),
            "brand_tier": statistics.mode(tiers) if tiers else None}


def profiles(brands: list[str], scope: list) -> dict[str, dict]:
    """Price profiles in parallel, inside algolia.py's throttle and worker count."""
    out: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=algolia.MAX_WORKERS) as pool:
        futs = {pool.submit(price_profile, b, scope): b for b in brands}
        for f in cf.as_completed(futs):
            try:
                out[futs[f]] = f.result()
            except Exception as exc:
                out[futs[f]] = {"error": f"{type(exc).__name__}"}
    return out


def run(scope: list, median_top: int = 0, base: str = "isForSale:true") -> dict:
    """Exact live listings per brand over the scope and `base` filter (the price
    floor goes in `base`), plus the median ask for the top N if asked. `counts` is
    brand -> listings, for brand_counts_staging. Deliberately no gate arithmetic:
    the kosher threshold is kosher.sql's business, not more requests."""
    t0 = time.time()
    counted = brand_counts(scope, base)
    counts, rep = counted["counts"], counted["report"]
    total = rep["live_total"]
    rows = [{"rank": i, "brand": b, "live_listings": c}
            for i, (b, c) in enumerate(counts.most_common(), 1)]
    t1 = time.time()
    prof = profiles([r["brand"] for r in rows[:median_top]], scope) if median_top else {}
    for r in rows[:median_top]:
        r.update(prof.get(r["brand"], {}))
    return {
        "base": base,
        "counts": counts,
        "partition": rep,
        "unbranded": {"label": None,
                      "how": "metadata.brand absent (no key) in the index and in Parse",
                      "share_upper_bound": round(1 - rep["facet_branded_total"] / total, 4)
                      if total else None},
        "price_profiles": {"brands": len(prof), "requests": len(prof),
                           "seconds": round(time.time() - t1, 2)},
        "seconds": round(time.time() - t0, 2),
        "brands": rows,
    }
