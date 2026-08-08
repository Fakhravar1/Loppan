"""Fill in `brands.price_point` and the rest of Sellpy's brand classification.

    python loppan/backfill_brand_classification.py
    python loppan/backfill_brand_classification.py --all      # re-read every brand
    python loppan/backfill_brand_classification.py --dry-run

All six classification columns were null for all 16,067 brands after the Algolia rehaul —
`price_point`, `styles`, `age_groups`, `origin_vibe`, `ethos`, `aesthetic_tone`. The v2
enrolment writes items and brand names, and never carried the classification across.

That is not cosmetic. `refresh_peer_prices()` builds its level-3 peer group from
(brand tier, garment). With every tier null the tier collapses to a single bucket and
level 3 silently becomes "same garment, anywhere in the market" — 66,367 items compared
against groups averaging 7,020 peers, while claiming to be a brand-tier comparison. It is
also why `dash_slice(p_dim => 'brand_tier')` returns nothing at all.

Where it comes from. Every Algolia document carries `brandClassification` inline:

    {"styles": ["Avant-garde","Chic"], "ageGroups": ["Gen Y","Gen X"], "pricePoint": 5,
     "originVibe": "French", "ethos": "Luxury Heritage", "aestheticTone": "Creative"}

So no new source is needed — the field is on documents the sweep already fetches. This
reads a handful per brand rather than re-sweeping, which is ~160 requests instead of 6,700.

Why more than one item per brand: sold items are removed from the Algolia index
immediately, so a single sampled id has a real chance of returning nothing. Sampling three
also lets the script check the assumption it depends on — that the classification is
constant per brand — instead of trusting it. Disagreements are reported and the brand is
left alone rather than being given an arbitrary one of the two answers.

Idempotent: by default it only touches brands whose `price_point` is still null, so it is
safe to re-run after `enrol` introduces new brands.
"""

from __future__ import annotations

import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loppan import algolia, db

PER_BRAND = 3
WRITE_BATCH = 500

# brandClassification key -> brands column. Kept explicit rather than derived from the
# camelCase, so that a renamed field upstream fails loudly here instead of quietly
# writing nulls over a column that already had a value.
FIELDS = {
    "pricePoint":    "price_point",
    "styles":        "styles",
    "ageGroups":     "age_groups",
    "originVibe":    "origin_vibe",
    "ethos":         "ethos",
    "aestheticTone": "aesthetic_tone",
}


def classification(doc: dict | None) -> dict | None:
    """The six classification values off one Algolia document, or None."""
    if not doc:
        return None
    raw = doc.get("brandClassification")
    if not raw or raw.get("pricePoint") is None:
        return None
    return {col: raw.get(key) for key, col in FIELDS.items()}


def main() -> None:
    if not db.configured():
        sys.exit("LOPPAN_SUPABASE_KEY is not set")
    only_missing = "--all" not in sys.argv
    dry_run = "--dry-run" in sys.argv

    sample = db.rpc("brand_sample_items",
                    {"p_per_brand": PER_BRAND, "p_only_missing": only_missing})
    if not sample:
        print("every brand already classified — nothing to do")
        return

    by_brand: dict[int, dict] = {}
    ids_for: dict[int, list[str]] = defaultdict(list)
    for row in sample:
        by_brand[row["brand_id"]] = {"name": row["brand_name"]}
        ids_for[row["brand_id"]].append(row["item_id"])

    item_ids = [i for ids in ids_for.values() for i in ids]
    print(f"{len(by_brand):,} brands to classify, sampling {len(item_ids):,} live items "
          f"({(len(item_ids)+99)//100:,} requests across {algolia.MAX_WORKERS} workers)")

    got: dict[str, dict] = {}
    fetched = 0
    for chunk_ids, results in algolia.get_objects_parallel(item_ids):
        for item_id, doc in zip(chunk_ids, results):
            fetched += 1
            found = classification(doc)
            if found:
                got[item_id] = found

    rows, disagreed, unresolved = [], [], []
    for brand_id, ids in ids_for.items():
        answers = [got[i] for i in ids if i in got]
        if not answers:
            unresolved.append(by_brand[brand_id]["name"])
            continue
        # The classification is documented as constant per brand. Verify rather than
        # assume: if two live items of the same brand disagree, neither is trustworthy
        # and picking one would bake a coin flip into every downstream peer group.
        if any(a != answers[0] for a in answers[1:]):
            disagreed.append(by_brand[brand_id]["name"])
            continue
        rows.append({"id": brand_id, "name": by_brand[brand_id]["name"], **answers[0]})

    print(f"  {fetched:,} documents read, {len(got):,} carried a classification")
    print(f"  {len(rows):,} brands resolved, {len(disagreed):,} disagreed, "
          f"{len(unresolved):,} had no live item left in the index")
    if disagreed:
        print(f"  disagreed (left unchanged): {', '.join(disagreed[:10])}"
              f"{' …' if len(disagreed) > 10 else ''}", file=sys.stderr)

    if dry_run:
        print("\n--dry-run: nothing written")
        for r in rows[:5]:
            print(f"    {r['name']}: tier {r['price_point']}, {r['ethos']}, {r['origin_vibe']}")
        return
    if not rows:
        print("nothing to write")
        return

    for i in range(0, len(rows), WRITE_BATCH):
        db.upsert("brands", rows[i:i + WRITE_BATCH], "id")
    print(f"\n{len(rows):,} brands updated")
    print("  NOTE: rerun loppan/analytics.py — refresh_peer_prices builds its level-3")
    print("  peer groups from the tier, and has been using a single collapsed bucket.")


if __name__ == "__main__":
    main()
