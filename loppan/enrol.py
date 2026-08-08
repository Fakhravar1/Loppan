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

Stratum C is the exception to all of the above: it is a census of Circle listings
rather than a sample, because that population is small and is the only one that can
say whether reselling pays. See `stratum_c`.

    python loppan/enrol.py --stratum A
    python loppan/enrol.py --stratum B --target 100000
    python loppan/enrol.py --stratum C --target 20000
    python loppan/enrol.py --stratum A --dry-run
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import time

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


# Bands must match the price_band CASE in the migration, or weights join to the
# wrong population. Index is the band number stored on the item.
BANDS = [(100, 199), (200, 299), (300, 499), (500, 999), (1000, None)]
PER_BAND = CAP // len(BANDS)


def stratum_a(dry: bool) -> int:
    """Quota per price band within each brand.

    Pulling the first CAP hits per brand does NOT give a representative slice: Algolia
    orders by its own relevance ranking, which favours expensive items. Measured on
    COS, that put 15% of the sample under 200 kr where the population is 35%, and 12%
    above 800 kr where the population is 3.9%. Cheap items are where the deep
    markdowns are, so the skew ran away from the effect being measured.

    Each band query returns both its items and its `nbHits`, so the band population is
    captured for free and the inclusion probability becomes known rather than assumed.
    """
    facets = algolia.brand_facets(MIN_PRICE_KR)
    qualifying = {b: n for b, n in facets.items() if n >= FLOOR}
    print(f"{len(facets)} brands in the facet, {len(qualifying)} with >= {FLOOR} listings")
    print(f"{len(BANDS)} bands x {PER_BAND}/band = up to {CAP} per brand, "
          f"~{len(qualifying)*len(BANDS):,} requests")
    if dry:
        return 0

    today = dt.date.today().isoformat()
    written = 0
    for i, (brand, pop) in enumerate(sorted(qualifying.items(), key=lambda x: -x[1]), 1):
        brand_id = _brands.get(brand)
        if brand_id is None:
            db.upsert("brands", [{"name": brand}], "name")
            for r in db.query(f"brands?select=id,name&name=eq.{brand.replace(' ', '%20')}"):
                _brands[r["name"]] = r["id"]
            brand_id = _brands.get(brand)

        bands_seen = []
        for band_no, (lo, hi) in enumerate(BANDS):
            f = f"price_SE.amount>={lo*100}" + (f" AND price_SE.amount<={hi*100}" if hi else "")
            _, ff = algolia.wearable_filter(MIN_PRICE_KR)
            r = algolia.search(filters=f, facet_filters=list(ff) + [[f"metadata.brand:{brand}"]],
                               hits_per_page=PER_BAND)
            hits = r.get("hits", [])
            if not hits:
                continue
            _prepare(hits)
            rows = [row_of(h, "A", 1.0, today) for h in hits]
            for row in rows:
                row["price_band"] = band_no
            db.upsert("items", rows, "item_id")
            bands_seen.append({"brand_id": brand_id, "band": band_no,
                               "population": r.get("nbHits", 0)})
            written += len(rows)

        if bands_seen:
            db.upsert("brand_band_population", bands_seen, "brand_id,band")
        db.upsert("brands", [{"name": brand, "population_listings": pop, "stratum": "A"}], "name")
        if i % 25 == 0 or i == len(qualifying):
            print(f"  {i}/{len(qualifying)} brands, {written:,} items", file=sys.stderr)

    print("recomputing sample weights from observed band coverage...")
    db.rpc("recompute_sample_weights")
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
            r = algolia.search(filters=f, facet_filters=[[f"categories.lvl1:{cat}"]],
                               hits_per_page=1000, page=page)
            hits = r.get("hits", [])
            if not hits:
                continue
            tail = [h for h in hits
                    if (h.get("metadata") or {}).get("brand")
                    and (h["metadata"]["brand"] not in big)]
            if not tail:
                continue

            # Per-shape weight, not one pooled constant. Within this shape the query
            # reports nbHits items in total and we examined len(hits) of them, so each
            # kept item stands for nbHits / examined of the population — the tail
            # fraction cancels, which is why no estimate of "tail items in this band"
            # is needed.
            #
            # ⚠️ This assumes the examined hits are a random slice of the shape. They
            # are not: Algolia orders by relevance. If tail brands rank systematically
            # lower they are under-drawn and this UNDERSTATES their weight. Narrow
            # bands limit the damage but do not remove it. Stratum B is exploratory
            # for that reason — do not use it for population estimates without saying so.
            weight = (r.get("nbHits") or len(hits)) / max(len(hits), 1)

            _prepare(tail)
            rows = [row_of(h, "B", weight, today) for h in tail]
            for row in rows:
                p = row.get("first_price_ore") or 0
                row["price_band"] = (0 if p < 20000 else 1 if p < 30000
                                     else 2 if p < 50000 else 3 if p < 100000 else 4)
            db.upsert("items", rows, "item_id")
            written += len(rows)
            print(f"  p{page} {lo}-{hi or '+'} {cat[:14]:14s} +{len(tail):4d} "
                  f"w={weight:>6.1f} ({written:,})", file=sys.stderr)
    return written


NEW_PER_BRAND = 25      # so Zara cannot flood a single run
LOOKBACK_DAYS = 3       # overlaps the every-other-day cadence, so nothing slips through


def stratum_n(target: int, dry: bool) -> int:
    """Enrol items that have just been listed.

    This is the highest-value stratum, and the reason is the ladder. Everything in A
    and B was enrolled mid-life — the median item was already 52 days and several
    markdowns into its run when we found it, so its true opening price is gone and
    unrecoverable without a per-item Parse lookup. An item caught within days of
    listing has its opening price recorded by definition, so its ENTIRE price path
    from first ask to final sale is observable.

    That is what makes Idea 2 testable: "Sellpy priced it wrong on day one" requires
    the day-one price, which only this stratum reliably has.

    `firstOfferedAt_SE` is the true listing date and is numerically filterable.
    `saleStartedAt` is NOT — it marks when the current price step began, a median 79
    days later, and filtering on it would return items that merely changed price.

    The walk uses price band x category shapes for the same reason stratum B does:
    Algolia stops paginating a single shape after ~2,000 results, so reach comes from
    the number of distinct shapes, not from paging deeper.
    """
    cutoff = int((time.time() - LOOKBACK_DAYS * 86400) * 1000)
    bands = [(100,199),(200,299),(300,499),(500,999),(1000,None)]
    shapes = [(lo, hi, cat) for lo, hi in bands for cat in algolia.WEARABLE]

    f0, _ = algolia.wearable_filter(MIN_PRICE_KR)
    total_new = algolia.search(filters=f0 + f" AND firstOfferedAt_SE>={cutoff}",
                               facet_filters=[[f"categories.lvl1:{c}" for c in algolia.WEARABLE]],
                               hits_per_page=0).get("nbHits", 0)
    print(f"~{total_new:,} listed in the last {LOOKBACK_DAYS} days; "
          f"taking up to {target:,}, max {NEW_PER_BRAND}/brand, over {len(shapes)} shapes")
    if dry:
        return 0

    today = dt.date.today().isoformat()
    per_brand: dict[str, int] = {}
    written = 0

    for page in range(3):
        for lo, hi, cat in shapes:
            if written >= target:
                break
            f = (f"price_SE.amount>={lo*100}"
                 + (f" AND price_SE.amount<={hi*100}" if hi else "")
                 + f" AND firstOfferedAt_SE>={cutoff}")
            hits = algolia.search(filters=f, facet_filters=[[f"categories.lvl1:{cat}"]],
                                  hits_per_page=1000, page=page).get("hits", [])
            if not hits:
                continue

            keep = []
            for h in hits:
                b = (h.get("metadata") or {}).get("brand")
                if not b or per_brand.get(b, 0) >= NEW_PER_BRAND:
                    continue
                per_brand[b] = per_brand.get(b, 0) + 1
                keep.append(h)
            if not keep:
                continue

            # Never reclassify an item we already hold: an item enrolled into A or B
            # keeps its stratum and its weight, or the sampling design silently rots.
            ids = [h["objectID"] for h in keep]
            known = set()
            for i in range(0, len(ids), 200):
                chunk = ",".join(ids[i:i + 200])
                known.update(r["item_id"] for r in
                             db.query(f"items?select=item_id&item_id=in.({chunk})"))
            fresh = [h for h in keep if h["objectID"] not in known]
            if not fresh:
                continue

            _prepare(fresh)
            weight = (total_new / target) if target else 1.0
            rows = [row_of(h, "N", weight, today) for h in fresh]
            for row in rows:
                p = row.get("first_price_ore") or 0
                row["price_band"] = (0 if p < 20000 else 1 if p < 30000
                                     else 2 if p < 50000 else 3 if p < 100000 else 4)
            db.upsert("items", rows, "item_id")
            written += len(rows)
        if written >= target:
            break

    print(f"  {len(per_brand):,} distinct brands touched")
    return written


def stratum_c(target: int, dry: bool) -> int:
    """Enrol Circle listings — the resale side of the market — as close to a census
    as the API allows.

    Every other stratum samples. This one tries to take the lot, because the Circle
    population is small (14,729 wearables at or above the floor, against 6.6M
    listings overall) and it is the only population that can answer whether reselling
    is profitable at all. At that size, sampling buys nothing and costs statistical
    power we do not have: the tracker had 3,671 Circle items and only 15 resolved
    sales to show for months of sweeping.

    Inclusion probability is therefore ~1 and the weight is 1.0 — but only while the
    stratum really does take everything. If `target` is set below the population, the
    weight recorded here becomes a lie. That is why the shortfall is reported loudly
    at the end rather than left for someone to infer.

    ⚠️ `isOnShelf:true` is not a configured filter attribute in Algolia — it matches
    nothing and does NOT error (see loppan/algolia.py). `isForSale:true` is the one
    that works. Getting this wrong yields a silent zero-item run that looks fine.
    """
    circle = "p2p:true AND isForSale:true"
    f0, ff = algolia.wearable_filter(MIN_PRICE_KR)
    population = algolia.search(filters=f"{circle} AND {f0}", facet_filters=ff,
                                hits_per_page=0).get("nbHits", 0)

    # A single query shape stops paginating after ~3 pages, so reach comes from the
    # number of distinct shapes rather than from paging deeper — the same constraint
    # stratum B and N work around.
    bands = [(100,124),(125,149),(150,174),(175,199),(200,249),(250,299),
             (300,349),(350,399),(400,499),(500,599),(600,799),(800,999),
             (1000,1499),(1500,2499),(2500,None)]
    shapes = [(lo, hi, cat) for lo, hi in bands for cat in algolia.WEARABLE]
    print(f"{population:,} Circle listings at or above {MIN_PRICE_KR} kr; "
          f"taking up to {target:,} over {len(shapes)} shapes")
    if dry:
        return 0

    today = dt.date.today().isoformat()
    written = 0

    for page in range(3):
        for lo, hi, cat in shapes:
            if written >= target:
                break
            f = (f"{circle} AND price_SE.amount>={lo*100}"
                 + (f" AND price_SE.amount<={hi*100}" if hi else ""))
            hits = algolia.search(filters=f, facet_filters=[[f"categories.lvl1:{cat}"]],
                                  hits_per_page=1000, page=page).get("hits", [])
            if not hits:
                continue

            # Never reclassify an item we already hold: a Circle item that arrived via
            # A, B or N keeps its stratum and its weight, or the sampling design rots.
            ids = [h["objectID"] for h in hits]
            known = set()
            for i in range(0, len(ids), 200):
                chunk = ",".join(ids[i:i + 200])
                known.update(r["item_id"] for r in
                             db.query(f"items?select=item_id&item_id=in.({chunk})"))
            fresh = [h for h in hits if h["objectID"] not in known]
            if not fresh:
                continue

            _prepare(fresh)
            rows = [row_of(h, "C", 1.0, today) for h in fresh]
            for row in rows:
                p = row.get("first_price_ore") or 0
                row["price_band"] = (0 if p < 20000 else 1 if p < 30000
                                     else 2 if p < 50000 else 3 if p < 100000 else 4)
            db.upsert("items", rows, "item_id")
            written += len(rows)
            print(f"  p{page} {lo}-{hi or '+'} {cat[:14]:14s} +{len(fresh):4d} "
                  f"({written:,})", file=sys.stderr)
        if written >= target:
            break

    held = db.count("items?select=item_id&p2p=is.true&limit=1")
    print(f"  {held:,} of {population:,} Circle listings now tracked")
    if held < population:
        print(f"  ⚠️ {population - held:,} not reached — sample_weight 1.0 assumes a "
              f"census, so treat population estimates from stratum C as provisional "
              f"until this closes.")
    return written


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    dry = "--dry-run" in sys.argv
    stratum = sys.argv[sys.argv.index("--stratum") + 1] if "--stratum" in sys.argv else "A"
    target = int(sys.argv[sys.argv.index("--target") + 1]) if "--target" in sys.argv else 100_000

    _load_caches()
    print(f"caches: {len(_brands):,} brands, {len(_lookup):,} lookups")

    u = stratum.upper()
    if u == "A":
        n = stratum_a(dry)
    elif u == "B":
        n = stratum_b(target, dry)
    elif u == "N":
        n = stratum_n(target, dry)
    elif u == "C":
        n = stratum_c(target, dry)
    else:
        sys.exit(f"unknown stratum {stratum!r} — use A, B, N or C")
    print(f"\nenrolled {n:,} items into stratum {stratum.upper()}")


if __name__ == "__main__":
    main()
