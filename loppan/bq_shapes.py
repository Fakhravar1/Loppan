"""Split a search into query shapes small enough to read whole, and prove it.

A query shape stops paginating at 2,400 results (measured 2026-10-02: nbPages x
hitsPerPage, and page 3 of 1,000 comes back empty), so reach comes from splitting.
Shapes here are numeric ranges, bisected until each holds at most LEAF hits, and a
leaf is read in ONE page of 1,000. One page means pagination never enters into it,
so the "pages shift under you" trap in docs/api-notes.md cannot occur.

Why numeric ranges and not category x price band x brand. Price clusters on single
values (99 kr) and brand facets stop at 1,000 values, so neither always splits.
`createdAt` (epoch seconds) is filterable, set on every live item (30,563 of 30,563
in one exhaustive check) and never changes, so an item cannot move between leaves
mid-crawl. `firstOfferedAt_SE` is missing on a few items (7 of 30,563), which is
fine for "listed since" and not for a census. Price is the fallback dimension.

Every leaf is checked: `exhaustiveNbHits` must be true and the hits read must equal
`nbHits`. Above the leaves, any shape whose count came back exhaustive is checked
against the sum of its leaves, which is what would expose items the ranges miss.
Counting asks for a facet, because that makes Algolia count exhaustively up to
~150k hits where a bare count is an estimate (84,656 exact vs 130,432 estimated).
"""

from __future__ import annotations

import concurrent.futures as cf
import time
from dataclasses import dataclass

from loppan import algolia

LEAF = 1000                 # = MAX_HITS_PER_PAGE: a leaf is one request
PAGE_CAP = 2400             # measured; docs say ~2,000
FAR = 4_102_444_800         # 2100-01-01 in seconds: an upper bound past any item
QUIET = {"attributesToHighlight": [], "attributesToSnippet": [], "analytics": False,
         "clickAnalytics": False, "enableABTest": False, "enableRules": False,
         "enablePersonalization": False,
         "responseFields": ["hits", "nbHits", "nbPages", "exhaustiveNbHits", "exhaustive",
                            "exhaustiveFacetsCount", "facets"]}


@dataclass(frozen=True)
class Dim:
    attr: str
    lo: int
    hi: int                 # half-open [lo, hi)
    always: bool = False    # filter even at the root range


def exhaustive(r: dict) -> bool:
    return bool(r.get("exhaustiveNbHits")) and (r.get("exhaustive") or {}).get("nbHits", True)


class Crawl:
    def __init__(self, base: str, facet_filters: list, dims: list[Dim], attrs: list[str],
                 max_leaves: int | None = None):
        self.base, self.ff, self.dims, self.attrs = base, facet_filters, dims, attrs
        self.max_leaves = max_leaves
        self.requests = 0
        self.nodes: dict[int, list] = {}     # id -> [parent, count, exhaustive, leaf_sum]
        self.problems: list[dict] = []

    def _filter(self, bounds) -> str:
        parts = [self.base] if self.base else []
        for d, (lo, hi) in zip(self.dims, bounds):
            if d.always or (lo, hi) != (d.lo, d.hi):
                parts.append(f"{d.attr}>={lo} AND {d.attr}<{hi}")
        return " AND ".join(parts)

    def _search(self, bounds, **kw) -> dict:
        self.requests += 1    # GIL-atomic enough for a tally
        return algolia.search(filters=self._filter(bounds), facet_filters=self.ff,
                              **QUIET, **kw)

    def _fetch(self, bounds) -> dict:
        return self._search(bounds, hits_per_page=LEAF, attributesToRetrieve=self.attrs)

    def probe(self, bounds, est):
        """Runs in a worker. ('leaf', hits, n, exhaustive) or ('split', n, exhaustive)."""
        if est is not None and est <= 0.9 * LEAF:
            r = self._fetch(bounds)
        else:
            r = self._search(bounds, hits_per_page=0, facets=["metadata.condition"],
                             maxValuesPerFacet=1)
            if r["nbHits"] == 0:
                return "leaf", [], 0, exhaustive(r)
            if r["nbHits"] > LEAF and self._splittable(bounds):
                return "split", r["nbHits"], exhaustive(r)
            r = self._fetch(bounds)
        n, ex = r["nbHits"], exhaustive(r)
        if (n > LEAF or not ex) and self._splittable(bounds):
            return "split", n, ex
        return "leaf", r.get("hits", []), n, ex

    def leaf_size(self, hits, n) -> int:
        """Items a leaf accounts for. Here, the hits actually read; a subclass whose
        leaves are counts rather than hits overrides this."""
        return len(hits)

    def _splittable(self, bounds) -> bool:
        return any(hi - lo > 1 for lo, hi in bounds)

    def _children(self, bounds, n):
        i = next(i for i, (lo, hi) in enumerate(bounds) if hi - lo > 1)
        lo, hi = bounds[i]
        k = 2 if n <= 8 * LEAF else 8
        cuts = sorted({lo + (hi - lo) * j // k for j in range(k)} | {hi})
        for a, b in zip(cuts, cuts[1:]):
            yield bounds[:i] + ((a, b),) + bounds[i + 1:], n / (len(cuts) - 1)

    def run(self, on_hits) -> dict:
        """Crawl every shape; `on_hits(hits)` gets each leaf's hits in this thread."""
        t0 = time.time()
        root = tuple((d.lo, d.hi) for d in self.dims)
        stack = [(root, None, None)]          # (bounds, estimate, parent id)
        leaves = hits_read = failed = next_id = 0
        truncated = False
        with cf.ThreadPoolExecutor(max_workers=algolia.MAX_WORKERS) as pool:
            inflight: dict = {}
            while stack or inflight:
                while stack and len(inflight) < algolia.WINDOW_CHUNKS:
                    if self.max_leaves is not None and leaves >= self.max_leaves:
                        truncated, stack = True, []
                        break
                    bounds, est, parent = stack.pop()
                    next_id += 1
                    self.nodes[next_id] = [parent, None, False, 0]
                    inflight[pool.submit(self.probe, bounds, est)] = (next_id, bounds)
                if not inflight:
                    break
                done, _ = cf.wait(inflight, return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    nid, bounds = inflight.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as exc:          # after algolia's own retries
                        failed += 1
                        self.problems.append({"kind": "failed", "filter": self._filter(bounds),
                                              "error": f"{type(exc).__name__}: {exc}"[:200]})
                        continue
                    node = self.nodes[nid]
                    if res[0] == "split":
                        node[1], node[2] = res[1], res[2]
                        stack.extend((b, e, nid) for b, e in self._children(bounds, res[1]))
                        continue
                    _, hits, n, ex = res
                    leaves += 1
                    got = self.leaf_size(hits, n)
                    hits_read += got
                    if not ex or got != n:
                        self.problems.append({"kind": "leaf not exhaustive" if not ex else
                                              "hits != nbHits", "filter": self._filter(bounds),
                                              "nbHits": n, "read": got})
                    p = nid
                    while p is not None:              # credit every ancestor
                        self.nodes[p][3] += got
                        p = self.nodes[p][0]
                    if hits:
                        on_hits(hits)
        secs = time.time() - t0
        return {"shapes": len(self.nodes), "leaves": leaves, "hits_read": hits_read,
                "requests": self.requests, "seconds": round(secs, 2),
                "requests_per_s": round(self.requests / secs, 1) if secs else None,
                "failed_shapes": failed, "truncated": truncated,
                "complete": not truncated and not failed and not self.problems,
                "subtree_checks": self._checks(truncated or failed),
                "problems": self.problems[:50]}

    def _checks(self, incomplete) -> dict:
        """Exhaustive internal counts against the leaves under them. Live churn moves
        a few items either way while a crawl runs, so only >1% is flagged."""
        if incomplete:
            return {"skipped": "crawl incomplete"}
        checked, worst, flagged = 0, 0.0, []
        for nid, (parent, count, ex, got) in self.nodes.items():
            if count is None or not ex:
                continue
            checked += 1
            rel = abs(got - count) / count
            worst = max(worst, rel)
            if rel > 0.01:
                flagged.append({"node": nid, "count": count, "leaf_sum": got})
        return {"checked": checked, "worst_rel_diff": round(worst, 5), "flagged": flagged[:20]}


def scope(categories: list[str]) -> list[list[str]]:
    """One OR group over the given category paths, each at its own level. An item
    can sit in several lvl1 categories (unisex shoes are under both Man and Kvinna),
    so one OR group, not one shape per category, keeps the leaves disjoint."""
    return [[f"categories.lvl{c.count(' > ')}:{c}" for c in categories]]
