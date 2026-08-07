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
| `stratum` | A brand-balanced · B pooled tail · L migrated legacy |
| `sample_weight` | How many real listings this row stands for |
| `price_band` | 0 <200kr · 1 200–299 · 2 300–499 · 3 500–999 · 4 1000+ |

---

## Supporting tables

**`brands`** (15,455) — `name`, `price_point` (Sellpy's 1–6 tier), `styles`, `age_groups`,
`origin_vibe`, `ethos`, `aesthetic_tone`, `population_listings`, `stratum`.

Every classification field is **constant per brand** — verified across 857 brands, zero
with a second classification. That is why they live here rather than being repeated on
666,769 item rows.

**`lookup`** — every categorical value, keyed by `kind`
(category, item_type, size, condition, demography, fabric, pattern).

**`mask_meaning`** — which bit means which fibre, colour or season.

**`brand_band_population`** — population and sampled count per (brand, price band). What
`recompute_sample_weights()` derives the weights from, and what makes inclusion
probability known rather than assumed.

---

## Deliberately not collected

| | Why |
|---|---|
| `images` | Reconstructible from `item_id`; 4.7 URLs per item was the single largest per-row cost |
| `keywords`, `concept`, `style` | Sellpy's generated text tags. Available if text features become interesting |
| `relevanceRanking*`, `proximityBucket*` | Sellpy's own ranking and personalisation. These **cause** sales by controlling visibility — endogenous, and they leak the outcome into the features |
| `itemAbTestFraction` | Sellpy runs experiments on these items. Worth capturing to *detect* a confounder, never to train on |
| `storeIds`, `storageSite`, `bag`, `itemIO`, `user` | Operational identifiers, not properties of the item |
| `estimateBid_rounded` | Present on ~1% of items |

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
