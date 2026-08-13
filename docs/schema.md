# What we collect, column by column

Reference for the v2 schema. `overview.md` is why, `api-notes.md` is the mechanics of
getting it, this is what actually lands in the database.

Everything here is swept from **Algolia** (`prod_marketItem_se_relevance`), the search
index the sellpy.se storefront runs on. Two fields come from elsewhere and are marked.

⚠️ **All prices are in öre.** Divide by 100 for kronor. A row reading `19000` is 190 kr.

---

## `items` — one row per item

666,769 rows as of 2026-08-07.

### Identity

| Column | What it is |
|---|---|
| `item_id` | Sellpy's own id, and the URL: `sellpy.se/item/{item_id}` |
| `brand_id` | → `brands` |
| `category_id` | → `lookup`. Full path, e.g. *Kvinna > Kläder > Byxor & Jeans* |
| `item_type_id` | → `lookup`. The garment itself — *Kavaj*, *Träningsskor* |
| `demography_id` | → `lookup`. Kvinna / Man / Flicka / Pojke / Unisex |

### Physical

| Column | What it is |
|---|---|
| `size_id` | → `lookup`. Coded: `WMN-EU-38`, `SHOES-EU-40`, `PANTS-INCH-30`, `NO SIZE` |
| `condition_id` | → `lookup`. Nytt / Mycket bra / Bra / Acceptabelt / Dåligt |
| `has_defect` | Sellpy recorded a flaw |
| `fabric_id` | → `lookup`. Denim, Mesh, Ribbstickad… |
| `pattern_id` | → `lookup`. Enfärgat, randigt… |
| `material_mask` | Bitmask of fibres — decode via `mask_meaning` |
| `colour_mask` | Bitmask of colours |
| `season_mask` | Bitmask: Vår 1, Sommar 2, Höst 4, Vinter 8 |
| `weight_g` | Grams. The direct input to shipping cost, which §1 argues is the binding constraint |

### Price

| Column | What it is |
|---|---|
| `first_price_ore` | Price when **we** first saw it |
| `price_ore` | Price at the most recent check |
| `old_price_ore` | Sellpy's own previous price — one markdown step back. Present on ~80% |
| `final_price_ore` | What it sold for. From **Parse**, not Algolia |
| `history` | Packed `[day, price, day, price…]`, day counted from `first_seen`. Appended only when the price moves |
| `price_to_estimate` | Sellpy's asking price ÷ their own value estimate. Only ~2,900 items, from the abandoned **Typesense** index |
| `peer_pct_frozen` | Where this item sat among comparable live listings the last time it was seen on the shelf, 0 = cheapest. **Written once, when the item resolves** |
| `peer_median_ore_frozen` | What that peer group's median ask was |
| `peer_n_frozen` | How many peers the comparison was against. Under ~20 it means little |
| `peer_level_frozen` | 1 brand+garment+condition · 2 brand+category · 3 garment across the market. See `analytics.md` §2 — level 3 is far weaker than its name |
| `peer_frozen_on` | Which pass's shelf the percentile was measured against |

The `peer_*_frozen` columns are the joinable copy. The live equivalent for currently-listed
items is the `peer_prices` table, which is truncated and rebuilt every pass and therefore
holds **nothing** for resolved items — that is exactly why these exist. `analytics.md` §2.

⚠️ **`first_price_ore` is not the original listing price**, except for items caught within
days of listing. Sellpy marks prices down roughly 11% every 10 days, and the median item
was already **52 days old** when enrolled. Measured against true histories: an item we
recorded at 110 kr had opened at **3,460 kr**, 27 markdowns earlier. Reconstructing the
opening from age is only reliable under ~30 days (80% within ±20%); past 120 days the
median estimate is 1.9× the truth and the worst decile 4.4×. The original price is only
obtainable per item from Parse.

### Demand

| Column | What it is |
|---|---|
| `favourites` | Likes at last check. **The only demand signal that exists** — Parse does not store it |
| `first_favourites` | Likes when first seen. Subtract for momentum |
| `fav_nordic` / `fav_eu` / `fav_dach` | Regional interest *buckets*, not raw counts |

### Dates

| Column | What it is |
|---|---|
| `first_offered` | **True original listing date.** Use this for age |
| `sale_started` | When the *current price step* began. Median **79 days** after `first_offered` — not a listing date |
| `first_seen` | When we enrolled it |
| `last_seen` | Last time we confirmed it alive |
| `resolved_on` | When we saw it had gone |

⚠️ `sale_started` is the trap. It looks like a listing date and is not. A "days on market"
feature built on it measures time-at-current-price.

⚠️ **`last_seen` did not mean this before 2026-08-13, and values written before then are
not what the row above says.** `track.py` only wrote it through `changed()`, which builds
an update solely for rows whose price, favourites, reserved or last-chance flag had moved
— so the column recorded the last time an item *changed*, not the last time it was seen.
The 08-13 pass confirmed 632,871 items alive and stamped 220,396 of them; **200,593 live
items were carrying a `last_seen` more than two days stale while being checked every
pass**. It is now written by `track.stamp_alive()`, the single writer, for every item the
sweep finds. Rows are corrected the first time a pass touches them, so treat any
`last_seen` earlier than the first completed pass after 2026-08-13 as unreliable, and use
`outcome is null` — not `last_seen` — to mean "on the shelf".

### Status

| Column | What it is |
|---|---|
| `outcome` | `null` still listed · 1 sold · 2 expired · 3 unknown · 4 below floor |
| `is_reserved` | Someone has it held — a leading indicator of a sale |
| `last_chance` | Sellpy's own end-of-life flag |
| `p2p` | true = Circle listing (private seller), false = consignment. **Different economics — do not pool them** |

### Sampling bookkeeping

| Column | What it is |
|---|---|
| `stratum` | A brand-balanced · B pooled tail · N newly listed · **C Circle census** · L migrated legacy |
| `sample_weight` | How many real listings this row stands for |
| `price_band` | 0 <200kr · 1 200–299 · 2 300–499 · 3 500–999 · 4 1000+ |

⚠️ **Stratum C is a census, not a sample**, so its `sample_weight` is 1.0. That is only
true while it holds every Circle listing at or above the floor — 14,781 tracked against
a live population of 14,728 as of 2026-08-08. `enrol.py --stratum C` warns if it falls
short, and a shortfall makes the weight wrong rather than merely imprecise.

---

## Circle round trips

**`circle_origins`** — the purchase side of a Circle resale: what the reseller paid Sellpy
for the item they are now reselling. Keyed by the **Circle** `item_id`, reached via the
`preceding` pointer on the Parse `Item`.

| Column | What it is |
|---|---|
| `original_id` | The listing the seller originally bought |
| `bought_price_ore` | Last price the original ever carried = what they paid |
| `original_opening_ore` | First price it carried, before any markdown |
| `original_rungs` | How many price steps the original went through |
| `bought_discount` | `1 - bought/opening` — how marked down it was at purchase |

⚠️ **Öre here, kronor in `cohort_items`.** Parse quotes kronor;
`backfill_item_origins.py` converts on the way in so this table matches `items.*_ore`.
The older `backfill_circle_origin.py` writes kronor into `cohort_items`. The two are
deliberately not shared code — unifying them without unifying the units would corrupt one.

⚠️ **Collect this while the item is still live.** The sale price arrives on its own from
the tracker, but the purchase price lives on the *original* listing and nothing guarantees
that stays reachable. Fifteen Circle sales were recorded before any origin existed, and
each one was priceable only because the original happened to survive.

**`v_tracked_roundtrips`** — `items` ⋈ `circle_origins`, giving `asking_multiple`,
`realised_multiple`, `profit_ore` and `days_paid_to_sold`.

Sellpy keeps 16% of a Circle sale, so **break-even is a gross multiple of 1.19×**.
`profit_ore` is null unless `outcome = 'sold'`: a final price on an expired listing is an
asking price nobody paid, and counting it as revenue would invent profit.

Three Circle populations exist and are **not** interchangeable:

| Source | n | What it can say |
|---|---|---|
| `circle_roundtrips` | 240 | Backward sample. **Structurally zero sales** — sold items are deleted from the index before a backward sample can see them |
| `v_circle_outcomes` | 500 | The frozen cohort stratum, enrolled 2026-08-04 |
| `v_tracked_roundtrips` | filling → 14,781 | The tracked census. The only one that will accumulate sales at scale |

The view inner-joins `circle_origins`, so it holds only what the backfill has reached so
far. First 13 priced sales, 2026-08-08: **median realised multiple 1.00×** against a
1.19× break-even, −1,005 kr across the thirteen. That sample is the *fast* tail — items
that sold within days of enrolment, against a ~60-day median time to sell — so it is
biased toward whatever sells quickest, which is generally whatever is underpriced. Treat
it as a first reading, not the answer.

---

## Supporting tables

**`brands`** (16,067) — `name`, `price_point` (Sellpy's 1–6 tier), `styles`, `age_groups`,
`origin_vibe`, `ethos`, `aesthetic_tone`, `population_listings`, `stratum`.

Every classification field is **constant per brand** — re-verified 2026-08-08 across
14,896 brands with exactly one disagreement (Conhpol, left unwritten). That is why they
live here rather than being repeated on 666,769 item rows.

⚠️ All six classification fields were **null for every brand** between the v2 rehaul and
2026-08-08, when `loppan/backfill_brand_classification.py` read them back off the
`brandClassification` object on the Algolia documents. 14,895 of 15,492 brands are now
populated; the rest have no live item left in the index to read. `population_listings` and
`stratum` remain sparse (1,047 brands) — they come from the sampling frame, not Algolia.

**`lookup`** — every categorical value, keyed by `kind`
(category, item_type, size, condition, demography, fabric, pattern).

**`mask_meaning`** — which bit means which fibre, colour or season.

**`brand_band_population`** — population and sampled count per (brand, price band). What
`recompute_sample_weights()` derives the weights from, and what makes inclusion
probability known rather than assumed.

**`shortlist_daily`** (~500/day, 30-day window) — the undervalued shortlist the dashboard
grid reads, one row per item per pass. Deliberately **denormalised**: brand names, lookup
values, decoded masks and the brand-level aggregates are all copied in, because the grid
sorts on every one of them and a computed column in a view cannot be indexed. Written by
`refresh_shortlist()`, then given its pictures by `loppan/shortlist.py`. See
`analytics.md` §8.

⚠️ **Every column named `_pct` here is a percentage, 0–100** — including `discount_pct`,
`peer_pct` and `markdown_pct`, which are scaled on write from the 0–1 fractions their
sources use. `brand_attention_index` is the exception and stays a ratio, because 1.00 =
"exactly the attention its mix deserves" is the entire meaning of that number.

**`size_group` · `size_system` · `size_value`** are **generated columns**, split from
`size` so the grid can filter on it. Sellpy codes every size as `GROUP-SYSTEM-VALUE`:

| | Examples | Notes |
|---|---|---|
| `size_group` | `WMN` `MEN` `CHILD` `SHOES` `SOCKS` `PANTS` `RINGS` `BELTS` `GLOVES` `HATS` | plus `ONE SIZE` and `NO SIZE`, which carry no hyphen and land here whole with a null system and value |
| `size_system` | `EU` `INT` `CM` `INCH` `MM` | and the tailoring cuts `EU_BRA` `EU_COLLAR` `EU_LONG` `EU_REGULAR` `EU_SHORT` `EU_WIDE` |
| `size_value` | `38` `M` `56/58` `80I` `16.5` | |

`split_part` is exact rather than best-effort here: **all 733 codes in `lookup` carry
exactly two hyphens**, systems use underscores and values use slashes and dots, so no
separator is ambiguous. Verified 2026-08-10.

Generated rather than written by `refresh_shortlist()` so the parse lives in one place
and cannot drift from the function the first time someone edits one and not the other.

⚠️ **`size_value` is meaningless on its own.** `SHOES-EU-38` and `WMN-EU-38` share a
digit and are unrelated things. Any filter or group-by on the value alone is a bug;
it only means something alongside its group and system.

### `size_area` — the axis Sellpy's coding lacks

**`size_group` mixes two unrelated things.** `WMN` / `MEN` / `CHILD` say *who* a garment
is cut for; `SHOES` / `PANTS` / `RINGS` / `BELTS` / `GLOVES` / `HATS` say *what part of
the body it fits*. So `WMN-EU-38` might be a dress, a skirt or trousers — three
different measurements of the same person — and no query over group alone can answer
"would this fit me".

`size_area` supplies the missing axis, generated from the size code plus the category
path:

| Area | Comes from |
|---|---|
| `feet` | `SHOES`, `SOCKS` |
| `lower body` | `PANTS`, or Byxor / Jeans / Shorts / Kjolar |
| `upper body` | Tröjor / T-shirts / Skjortor / Blusar / Toppar / Jackor / Kavajer |
| `full body` | Klänningar / Byxdressar / Badkläder / Sovplagg / Mammakläder |
| `underwear` · `sportswear` | their own categories |
| `bust` · `neck` | the `EU_BRA` and `EU_COLLAR` systems — the scale settles it whatever aisle the item sits in |
| `waist` · `head` · `hands` · `fingers` | `BELTS`, `HATS`, `GLOVES`, `RINGS` |
| `child` | `CHILD` (height in cm) |
| `unsized` · `other` | `ONE SIZE` / `NO SIZE`, and anything unmatched |

Distribution on the 2,000-row shortlist, 2026-08-10: upper body 842, full body 531,
feet 353, lower body 171, child 77, everything else 25.

This is the level a **personal size profile** is expressed in — feet one size, legs
another, torso a third — and the dashboard's "my sizes" panel is built on it. The
profile lives in the browser's localStorage, never in the database: the app is
read-only and that has not changed.

Not generated from `size_group`/`size_system`, because Postgres forbids a generated
column referencing another generated column. The code is re-split inside the
expression; that duplication is deliberate and confined to one place.

---

## Deliberately not collected

| | Why |
|---|---|
| `images` | Re-fetchable by `item_id` from Algolia; 4.7 URLs per item was the single largest per-row cost. ⚠️ **Not *computable* from `item_id`** — see below |
| `keywords`, `concept`, `style` | Sellpy's generated text tags. Available if text features become interesting |
| `relevanceRanking*`, `proximityBucket*` | Sellpy's own ranking and personalisation. These **cause** sales by controlling visibility — endogenous, and they leak the outcome into the features |
| `itemAbTestFraction` | Sellpy runs experiments on these items. Worth capturing to *detect* a confounder, never to train on |
| `storeIds`, `storageSite`, `bag`, `itemIO`, `user` | Operational identifiers, not properties of the item |
| `estimateBid_rounded` | Present on ~1% of items |

### The image claim was wrong, and cost a dashboard rebuild to find (2026-08-10)

This table used to say images were "reconstructible from `item_id`". They are not, in the
sense anyone would read that. A real URL looks like:

```
https://prod.images.sellpy.net/photoRobot-case-14-k-8/IWCFH2lSCH-0fca-0.jpg
                               └── one of ~200 stations ─┘ └id┘ └hex┘
```

The folder is whichever photo station shot the item — `photoRobot-case-14-k-8`,
`photoRobot-hanging-10-k-3`, `phone-images` and so on, sampled across 3,000 items — and
the segment after the id is a random four-hex suffix. Neither is derivable from anything
we store, and phone-uploaded images carry no station folder at all. **There is no formula.
An image costs a lookup.**

What is true, and is what the wording was reaching for: the lookup is *cheap*. Algolia
returns the whole `images` array on a by-id fetch, 100 ids per request, so pictures for a
500-item shortlist cost about five requests. `loppan/shortlist.py` does exactly that. Do
**not** generalise it to the whole shelf — 671k live items would be ~7,000 requests and
~190 MB, which is why the column was dropped in the first place.

Store the **path**, not the URL, per `search.image_paths`: Parse serves a private S3 URL
that 403s while the search index serves the public CDN, and the path after the host is
identical.

---

## Reading it

```sql
select i.item_id, b.name as brand, lt.value as item_type,
       ls.value as size, lc.value as condition,
       i.price_ore / 100.0 as price_kr,
       i.favourites, i.weight_g,
       (i.first_seen - i.first_offered) as age_at_enrolment_days,
       i.stratum, i.sample_weight
from public.items i
left join public.brands b  on b.id = i.brand_id
left join public.lookup lt on lt.id = i.item_type_id
left join public.lookup ls on ls.id = i.size_id
left join public.lookup lc on lc.id = i.condition_id
where i.outcome is null
limit 20;
```

Winter items, decoding the bitmask:

```sql
select count(*) from public.items where season_mask & 8 > 0;
```

Population estimates need the weight — an unweighted count answers a question about the
sample, not about Sellpy:

```sql
select round(sum(sample_weight)) as estimated_real_items
from public.items where has_defect;
```
