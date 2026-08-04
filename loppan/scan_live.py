"""Scan currently-listed offers and keep the full Item record for each.

`include=item` returns the whole 54-field Item inline, which is enormously cheaper
than one lookup per item — but the server's ~10 s budget means it only survives
shallow `skip`. So this walks outward until it starts timing out, then stops,
rather than pretending it can enumerate the catalogue.

Output: data/scan_<timestamp>.jsonl, one offer-with-item per line.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import sellpy

PAGE = 100
DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


def scan(max_items: int = 3000, region: str = "SE") -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    skip = 0
    consecutive_failures = 0

    while len(out) < max_items and consecutive_failures < 2:
        started = time.time()
        try:
            batch = sellpy.find(
                "MarketOffer",
                {"region": region, "latest": True},
                limit=PAGE,
                skip=skip,
                include="item",
            )
        except sellpy.QueryTooSlow:
            consecutive_failures += 1
            print(f"  skip={skip}: timed out, backing off", file=sys.stderr)
            skip += PAGE
            continue

        consecutive_failures = 0
        if not batch:
            break

        fresh = 0
        for offer in batch:
            oid = offer["objectId"]
            if oid in seen:
                continue
            seen.add(oid)
            out.append(offer)
            fresh += 1

        print(
            f"  skip={skip:5d} {time.time()-started:4.1f}s  +{fresh:3d}  total={len(out)}",
            file=sys.stderr,
        )
        skip += PAGE

    return out


def summarise(offers: list[dict]) -> None:
    """Report the shape of what we got, so a thin slice is obvious immediately."""
    items = [o.get("item", {}) for o in offers]
    live = [o for o in offers if not o.get("endedAt")]

    print(f"\n{len(offers)} offers | {len(live)} still listed | {len(offers)-len(live)} ended")

    prices = sorted(o["pricing"]["amount"] for o in offers)
    if prices:
        n = len(prices)
        print(
            f"price: median {prices[n//2]:.0f} | p90 {prices[int(n*0.9)]:.0f} "
            f"| max {prices[-1]:.0f}"
        )

    brands = collections.Counter(
        (i.get("metadata") or {}).get("brand") for i in items if i.get("metadata")
    )
    print(f"\ndistinct brands: {len(brands)}")
    print("most common:", ", ".join(f"{b} {c}" for b, c in brands.most_common(10)))

    targets = ["Carhartt WIP", "Carhartt", "COS", "Dr. Martens", "Ambika"]
    print("\ntarget brands from the four known trades:")
    for t in targets:
        print(f"  {t:15s} {brands.get(t, 0)}")

    have = sum(1 for i in items if (i.get("metadata") or {}).get("productId"))
    season = sum(1 for i in items if (i.get("metadata") or {}).get("season"))
    defects = sum(1 for i in items if (i.get("metadata") or {}).get("defects"))
    scored = sum(1 for i in items if i.get("sellabilityEstimate"))
    print(
        f"\nfield coverage: productId {have}/{len(items)} | season {season} "
        f"| defects {defects} | sellabilityEstimate {scored}"
    )


def main() -> None:
    max_items = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    DATA.mkdir(exist_ok=True)
    offers = scan(max_items)
    path = DATA / f"scan_{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for offer in offers:
            fh.write(json.dumps(offer, ensure_ascii=False) + "\n")
    summarise(offers)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
