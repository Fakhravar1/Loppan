"""Enrol a brand-stratified sample of the Sellpy catalogue for outcome tracking.

The sampling problem. The population is ~6.6M clothing and shoe listings at or
above 100 kr. The top 1,000 brands hold 59% of it and Zara alone holds 162,000, so
an unstratified sample would mostly measure Zara. But the other ~43,600 brands
average 62 listings each — too few to estimate a brand effect, and they cannot even
be enumerated, because the facet API returns at most 1,000 brand values.

So two strata, with weights recorded so population estimates stay possible:

  A  brands with >= FLOOR listings, capped at CAP items each. Balanced enough that
     no brand dominates, deep enough to fit per-brand effects.
  B  a pooled walk of the population, keeping only items whose brand fell below the
     floor. Not balanced by brand — it exists so the long tail is represented at
     all. That matters because Idea 2 says the profit comes from Sellpy's pricing
     ERRORS, and errors should be commonest on obscure brands their model has least
     data for. Dropping the tail would discard the likeliest source of edge.

    python loppan/enrol.py --stratum A
    python loppan/enrol.py --stratum B --target 100000
    python loppan/enrol.py --stratum A --dry-run
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, db

FLOOR = 500     # a brand needs this many listings to get its own stratum-A quota
CAP = 700       # and contributes at most this many items
MIN_PRICE_KR = 100

_lookup: dict[tuple[str, str], int] = {}
_brands: dict[str, int] = {}
_masks: dict[str, dict[str, int]] = {}


# ---------------------------------------------------------------- lookups


def _load_caches() -> None:
    _lookup.clear(); _brands.clear(); _masks.clear()
    for r in db.query("lookup?select=id,kind,value&limit=200000"):
        _lookup[(r["kind"], r["value"])] = r["id"]
    for r in db.query("brands?select=id,name&limit=200000"):
        _brands[r["name"]] = r["id"]
    for r in db.query("mask_meaning?select=kind,bit,value&limit=10000"):
        _masks.setdefault(r["kind"], {})[r["value"]] = r["bit"]


def _ensure_lookups(pairs: set[tuple[str, str]]) -> None:
    new = [{"kind": k, "value": v} for k, v in pairs if (k, v) not in _lookup]
    if not new:
        return
    db.upsert("lookup", new, "kind,value")
    for r in db.query("lookup?select=id,kind,value&limit=200000"):
        _lookup[(r["kind"], r["value"])] = r["id"]


def _ensure_bits(kind: str, values: set[str], width: int) -> None:
    """Assign a bit per distinct value, in discovery order. Anything beyond `width`
    shares the top bit — losing a rare value is better than silently truncating the
    mask and mislabelling common ones."""
    known = _masks.setdefault(kind, {})
    fresh = [v for v in sorted(values) if v not in known]
    if not fresh:
        return
    rows = []
    for v in fresh:
        bit = len(known) if len(known) < width - 1 else width - 1
        known[v] = bit
        rows.append({"kind": kind, "bit": bit, "value": v})
    db.upsert("mask_meaning", rows, "kind,bit")


def _mask(kind: str, values) -> int:
    m = 0
    for v in values or []:
        bit = _masks.get(kind, {}).get(v)
        if bit is not None:
            m |= (1 << bit)
    return m


def _lid(kind: str, value) -> int | None:
    return _lookup.get((kind, value)) if value else None


# ---------------------------------------------------------------- mapping


def _date(ms) -> str | None:
    if not ms:
        return None
    if ms > 1e11:      # milliseconds
        ms = ms / 1000
    return dt.date.fromtimestamp(ms).isoformat()


def row_of(hit: dict, stratum: str, weight: float, today: str) -> dict:
    m = hit.get("metadata") or {}
    price = (hit.get("price_SE") or {}).get("amount")
    old = ((hit.get("priceDrop_SE") or {}).get("oldPrice") or {}).get("amount")
    cats = (hit.get("categories") or {}).get("lvl2") or (hit.get("categories") or {}).get("lvl1") or []
    fav = hit.get("favouriteCount")
    weight_kg = hit.get("weight")

    return {
        "item_id": hit["objectID"],
        "brand_id": _brands.get(m.get("brand")),
        "category_id": _lid("category", cats[0] if cats else None),
        "item_type_id": _lid("item_type", m.get("type")),
        "size_id": _lid("size", m.get("size")),
        "condition_id": _lid("condition", m.get("condition")),
        "demography_id": _lid("demography", m.get("demography")),
        "fabric_id": _lid("fabric", m.get("fabric")),
        "pattern_id": _lid("pattern", m.get("pattern")),
        "season_mask": _mask("season", m.get("season")),
        "material_mask": _mask("material", m.get("material")),
        "colour_mask": _mask("colour", m.get("color")),
        "weight_g": int(weight_kg * 1000) if weight_kg else None,
        "has_defect": bool(m.get("defects")),
        "first_price_ore": price,
        "first_favourites": fav,
        "first_seen": today,
        "price_ore": price,
        "old_price_ore": old,
        "favourites": fav,
        "fav_nordic": hit.get("favoriteCountBucket_NORDIC"),
        "fav_eu": hit.get("favoriteCountBucket_EU"),
        "fav_dach": hit.get("favoriteCountBucket_DACH"),
        "last_seen": today,
        "last_chance": hit.get("lastChance"),
        "is_reserved": hit.get("isReserved"),
        "p2p": hit.get("p2p"),
        # firstOfferedAt is the true listing date; saleStartedAt is the current price step
        "first_offered": _date(hit.get("firstOfferedAt_SE")),
        "sale_started": _date(hit.get("saleStartedAt")),
        "history": [0, price] if price else None,
        "stratum": stratum,
        "sample_weight": weight,
    }


def _prepare(hits: list[dict]) -> None:
    """Create every brand, lookup and bit these hits need, before mapping them."""
    brands = {(h.get("metadata") or {}).get("brand") for h in hits}
    brands.discard(None)
    fresh = [{"name": b} for b in brands if b not in _brands]
    if fresh:
        db.upsert("brands", fresh, "name")
        for r in db.query("brands?select=id,name&limit=200000"):
            _brands[r["name"]] = r["id"]

    pairs, mats, cols, seasons = set(), set(), set(), set()
    for h in hits:
        m = h.get("metadata") or {}
        cats = (h.get("categories") or {}).get("lvl2") or (h.get("categories") or {}).get("lvl1") or []
        for kind, val in (("category", cats[0] if cats else None),
                          ("item_type", m.get("type")), ("size", m.get("size")),
                          ("condition", m.get("condition")), ("demography", m.get("demography")),
                          ("fabric", m.get("fabric")), ("pattern", m.get("pattern"))):
            if val:
                pairs.add((kind, val))
        mats.update(m.get("material") or [])
        cols.update(m.get("color") or [])
        seasons.update(m.get("season") or [])
    _ensure_lookups(pairs)
    _ensure_bits("material", mats, 62)
    _ensure_bits("colour", cols, 31)
    _ensure_bits("season", seasons, 15)


# ---------------------------------------------------------------- strata


def stratum_a(dry: bool) -> int:
    facets = algolia.brand_facets(MIN_PRICE_KR)
    qualifying = {b: n for b, n in facets.items() if n >= FLOOR}
    print(f"{len(facets)} brands in the facet, {len(qualifying)} with >= {FLOOR} listings")
    print(f"expected sample: {sum(min(n, CAP) for n in qualifying.values()):,} items "
          f"in ~{len(qualifying)} requests")
    if dry:
        return 0

    today = dt.date.today().isoformat()
    written = 0
    for i, (brand, pop) in enumerate(sorted(qualifying.items(), key=lambda x: -x[1]), 1):
        hits = algolia.brand_items(brand, CAP, MIN_PRICE_KR)
        if not hits:
            continue
        _prepare(hits)
        # inverse inclusion probability: how many real listings each sampled item stands for
        w = pop / len(hits)
        rows = [row_of(h, "A", w, today) for h in hits]
        db.upsert("items", rows, "item_id")
        db.upsert("brands", [{"name": brand, "population_listings": pop, "stratum": "A"}], "name")
        written += len(rows)
        if i % 25 == 0 or i == len(qualifying):
            print(f"  {i}/{len(qualifying)} brands, {written:,} items", file=sys.stderr)
    return written


def stratum_b(target: int, dry: bool) -> int:
    """Walk the population and keep only tail-brand items.

    The tail cannot be queried by brand — the facet API caps at 1,000 values — so it
    has to be reached by walking. Price bands give many distinct query shapes, which
    matters because deep pagination on any single shape is limited.
    """
    facets = algolia.brand_facets(MIN_PRICE_KR)
    big = {b for b, n in facets.items() if n >= FLOOR}

    # Algolia stops paginating a query shape after ~2,000 results, so the number of
    # DISTINCT shapes is what determines reach, not how deep each one goes. Crossing
    # narrow price bands with individual categories multiplies the shapes; a single
    # band walked deep just runs into the ceiling, which is what capped the first
    # attempt at 4,727 items.
    bands = [(100,124),(125,149),(150,174),(175,199),(200,249),(250,299),
             (300,349),(350,399),(400,499),(500,599),(600,799),(800,999),
             (1000,1499),(1500,2499),(2500,None)]
    shapes = [(lo, hi, cat) for lo, hi in bands for cat in algolia.WEARABLE]
    print(f"walking {len(shapes)} query shapes ({len(bands)} price bands x "
          f"{len(algolia.WEARABLE)} categories) for tail items, target {target:,}")
    if dry:
        return 0

    today = dt.date.today().isoformat()
    written = 0
    for page in range(3):
        for lo, hi, cat in shapes:
            if written >= target:
                return written
            f = f"price_SE.amount>={lo*100}" + (f" AND price_SE.amount<={hi*100}" if hi else "")
            hits = algolia.search(filters=f, facet_filters=[[f"categories.lvl1:{cat}"]],
                                  hits_per_page=1000, page=page).get("hits", [])
            tail = [h for h in hits
                    if (h.get("metadata") or {}).get("brand")
                    and (h["metadata"]["brand"] not in big)]
            if not tail:
                continue
            _prepare(tail)
            rows = [row_of(h, "B", 0.0, today) for h in tail]
            db.upsert("items", rows, "item_id")
            written += len(rows)
            print(f"  p{page} {lo}-{hi or '+'} {cat[:14]:14s} +{len(tail):4d} "
                  f"({written:,})", file=sys.stderr)
    return written


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    dry = "--dry-run" in sys.argv
    stratum = sys.argv[sys.argv.index("--stratum") + 1] if "--stratum" in sys.argv else "A"
    target = int(sys.argv[sys.argv.index("--target") + 1]) if "--target" in sys.argv else 100_000

    _load_caches()
    print(f"caches: {len(_brands):,} brands, {len(_lookup):,} lookups")

    n = stratum_a(dry) if stratum.upper() == "A" else stratum_b(target, dry)
    print(f"\nenrolled {n:,} items into stratum {stratum.upper()}")


if __name__ == "__main__":
    main()
