# Sellpy backend — what the API will and won't do

Established empirically 2026-08-04. Read this before writing a query; several
obvious-looking ones fail, and the failure mode is uninformative.

## Endpoints

| Layer | URL |
|---|---|
| Parse REST | `https://sellpy-parse-prod.herokuapp.com/parse` |
| GraphQL | `https://sellpy-parse-prod.herokuapp.com/graphql` (introspection disabled) |

Credentials are the browser SDK's `applicationId` + `javascriptKey`, served in
plain text in `https://www.sellpy.se/market/index.*.bundle.js`. Public-by-design
client keys. If they rotate, re-read them from the bundle.

No Cloudflare, no bot protection, no user-agent check. The site itself is a
client-side SPA — the HTML is a 6 kB empty shell, so there is nothing to scrape
there and everything to read here.

## The one failure mode you need to recognise

**Any query the server cannot serve in ~10 seconds returns a bare `HTTP 500`.**
It is not a malformed-request error. It nearly always means the field you
constrained or sorted on is unindexed. Timing confirms it: every failure below
took 10.2–10.4 s, every success was under 4 s.

`sellpy.find()` raises `QueryTooSlow` on 500 for exactly this reason.

## Class access

| Class | Read by id | Query by other fields |
|---|---|---|
| `Item` | ✅ | ❌ — returns `{"results":[]}` for any constraint except `objectId` |
| `MarketOffer` | ✅ | ✅ — but only certain shapes, see below |
| `ItemCategory`, `ItemType` | ✅ | — |
| `ItemReservation`, `MarketOffer` | ✅ | — |
| `ItemBrand`, `ItemBlob`, `CircleItem`, `Sale` | ❌ permission denied | — |

Consequence: **`Item` cannot be enumerated.** Discovery has to run through
`MarketOffer`, and `Item` is enrichment-by-id on top of it.

## MarketOffer query shapes

Base filter `{"region": "SE", "latest": true}`.

| Query | Result |
|---|---|
| base, `limit=1000`, no order | ✅ 0.5 s |
| base, `limit=1000`, `skip` up to 8000 | ✅ 0.5–1.0 s, **no overlap between pages** |
| base, `skip=10000` | ❌ timeout |
| base, `order=objectId`, `limit≤100` | ✅ 3.7 s |
| base, `order=objectId`, `limit≥200` | ❌ timeout |
| base, `order=createdAt` | ❌ timeout |
| base, `order=-pricing.amount` | ❌ timeout |
| base + `include=item`, `limit=100` | ✅ 3.7 s — returns the **full 54-field Item inline** |
| base + `include=item`, `limit≥200` | ❌ timeout |
| base + `pricing.amount` range, `limit=2` | ✅ |
| base + `pricing.amount` range, `limit=100` | ❌ timeout — **price is not indexed** |
| base + `objectId: {$gt: …}` keyset | ✅ alone, ❌ combined with `include` |
| `createdAt` range filter | ❌ timeout — **date is not indexed** |
| `first: true` | ❌ timeout |
| by `item` pointer (one item's full ladder) | ✅ fast, returns every historical step |

### What that means in practice

- **Price-band filtering must happen client-side.** Pull unfiltered pages, bucket
  locally. Server-side band filters look like they work at `limit=2` and then die
  at any useful page size.
- **Paginate with `skip` in 1000-row pages, unordered.** Verified 5,000 rows
  across five pages with zero duplicates. Deep skip (≥10000) times out, so a
  single unordered scan tops out around 9,000 rows per filter — take a fresh
  sample rather than trying to walk the whole catalogue.
- **`include=item` is worth it when you want ≤100 rows** with full detail in one
  call. Above that, enumerate cheaply and enrich by id.
- **Per-item history is cheap and unrestricted.** `ladder(item_id)` returns the
  complete markdown sequence with `createdAt` / `endedAt` per step, for items that
  sold months or years ago.

## Field notes

- **Filter `region`.** One price step exists once per market; a single step
  returned 11 rows across currencies. `SE` gives SEK.
- **`first` / `latest`** flag the opening ask and the final/current one.
- **`latest: true` includes items that already ended.** Split on `endedAt`:
  absent = still listed, present = sold or expired.
- **Sale date is the last offer's `endedAt`, not `Item.paidAt`.** `paidAt` is the
  seller payout — same day for consignment, but **21–24 days later for Circle**
  sales (measured, n=3).
- **`Item.itemStatus`** — four values observed, and **two of them mean sold**:

  | Status | Meaning | Outcome |
  |---|---|---|
  | `utlagd` | listed | still listed |
  | **`såld`** | **sold, payout to the seller still pending** | **sold** |
  | `betald` | sold and paid out | sold |
  | `vilande` | dormant (observed after a cancelled Circle sale) | expired |
  | `skänkt` | donated — Sellpy gives away what it cannot sell | expired |

  ⚠️ **Never test `itemStatus == "betald"` to mean sold.** `betald` is sold *and
  paid out*, and the payout lands 21–24 days after a Circle sale (above), so that
  test silently misses nearly every recent sale. Measured on the cohort: **49 of
  52 sales were `såld`, only 3 `betald`** — the naive test would have found 6% of
  them. It had already recorded two real sales in `item_ladders` as unsold.

  Use `cohort.STATUS_OUTCOME`, which is the single mapping, rather than comparing
  strings in each caller. An unrecognised status maps to `unknown`, which is
  deliberately **not** terminal so it gets flagged rather than guessed — but note
  that a *known* status left unmapped is then re-fetched on every run forever, as
  `skänkt` was 26 times before it was added.
- **Circle listings are ordinary `Item` records** carrying
  `p2pValueShare: {version: 1, customerShare: 0.8}`.
- **`sellabilityEstimate`** `{score, cutoff, isReliable, version}` — Sellpy's own
  sell-probability. **Key any analysis by `version`**: `"3"` and `"3-mla"` both
  seen in the wild, so the model has already rolled at least once and scores are
  not comparable across versions.
- **Rich metadata is sparse but excellent where present**: `productId` /
  `variantId` (manufacturer codes — exact comparables, no fuzzy matching),
  `season` (`["Höst","Vinter"]`), `defects` (structured, with location).

## Observed population shape

From an unbiased 5,000-offer sample (2026-08-04), latest offers, region SE:

| | median | p90 | p99 | max | ≥2000 kr |
|---|---|---|---|---|---|
| all | 60 kr | 260 | 900 | 3,920 | **0.06%** |
| still listed (n=478) | 65 kr | 360 | — | 3,920 | 1 item |
| ended (n=4,522) | 55 kr | 250 | — | 2,360 | 2 items |

**Sellpy's live catalogue is overwhelmingly cheap.** 96% of current asking prices
are under 500 kr. Any strategy that depends on a supply of high-value items has a
sourcing problem, and defining a "premium" band on *current* price will find
almost nothing — define it on the **opening ask** instead, since expensive items
are marked down into the cheap bands rather than staying expensive.

## Conduct

`robots.txt` allows `/item/*` and disallows the search paths. These are
undocumented endpoints being used outside a browser, so the constraint is
self-imposed: one request per second (`sellpy.MIN_INTERVAL_S`), no distributed
crawling, no redistribution of the data, one account. The exposure that matters
isn't the scraper breaking — it's the account.

### What "one request per second" is actually protecting (2026-08-09)

Two things were measured while moving 14,800 Circle origins, and both refine the
rule rather than relax it.

**Nothing here authenticates as a user.** `sellpy.py` sends `_ApplicationId`,
`_JavaScriptKey` and `_ClientVersion` — the public browser-SDK keys served in every
visitor's bundle — and no session token, cookie or `Authorization` header. So the
account is not attached to this traffic at the auth layer, and the realistic worst
case is an IP being rate-limited or blocked, which breaks a crawl and is
recoverable. The residual account exposure is **correlation**: the crawl leaving the
same household IP that also carries a logged-in Sellpy session. That is an argument
about where the traffic originates, not about how fast it goes.

**A Parse request costs ~0.24 s of latency against a 1.0 s interval**, so at the
default roughly 76% of a pass is deliberate waiting rather than work. Lowering the
interval therefore speeds a job up almost proportionally, until latency binds at
around 0.25 s. `backfill_item_origins.py --interval` does this **scoped to one job**:
2.04 → 0.60 s/item measured, a 3.4× speedup.

⚠️ Two lines this does not cross, and should not. It stays **strictly serial** — one
request in flight, never a worker pool — so "no distributed crawling" is untouched;
and it does not change `sellpy.MIN_INTERVAL_S` itself, because that global also
governs `track.py`'s adjudication and the cohort checks, which were sized against
1 req/s and are nowhere near this hot. Re-tune per job, with a measurement, not
globally.

---

# The Algolia index — the storefront's own search surface (added 2026-08-08)

Everything above describes Parse, which cannot filter by price, brand or date and
tops out around 9,000 rows. Two search indexes lift those limits, and it matters
which one you reach for. **Algolia is the index the sellpy.se storefront actually
browses, and it is the primary discovery surface here.** The Typesense collection
documented in the next section is a ~5% subset of it, kept only for the two fields
it uniquely carries.

Client is `loppan/algolia.py`; `loppan/enrol.py` reads Algolia hits, so the current
enrolment path is Algolia-based. Index **`prod_marketItem_se_relevance`**,
**12,475,724 documents** (measured 2026-08-08, `exhaustiveNbHits: true`). The app id
and search key are the ones every visitor's browser holds — scoped and search-only.

## How the two indexes relate

| | Algolia | Typesense (`market_items`) |
|---|---|---|
| Documents | ~12.5M | 586,746 |
| Scope | the whole market | a ~5% subset |
| Overlap | **99.8%** of Typesense items are also here | only **6.8%** of Algolia items are here |

So a Typesense-scoped count is very nearly a subset of an Algolia one, but never the
reverse — and Typesense is close to useless as a measure of market size.

(The Typesense figure is the 586,746 recorded in `algolia.py` when the overlap was
measured. The section below says 584,041, measured 2026-08-04. Both are right for
their date; the collection drifts. The ~5% relationship is what matters, and it is
not sensitive to which one you take.)

## What each source uniquely carries

| Only in Algolia | Only in Typesense |
|---|---|
| the whole market | **`priceToEstimateRatio`** |
| `weight` | **`sellabilityEstimate`** |
| `priceDrop_SE.oldPrice` | |
| regional favourite buckets | |
| `firstOfferedAt_SE` | |

Neither `priceToEstimateRatio` nor `sellabilityEstimate` is in Parse either, which is
the whole reason the Typesense section below survives. Confirmed absent from Algolia
hits on inspection (0 of 20 sampled documents carried either).

**The favourite fields are spelled two different ways.** The plain count is British —
`favouriteCount` — while the buckets are American: `favoriteCountBucket`,
`favoriteCountBucket_NORDIC`, `favoriteCountBucket_EU`, `favoriteCountBucket_DACH`.
The regional buckets are frequently null; the unsuffixed one was populated on every
sampled document.

## Traps

- ⚠️ **Filtering on an unconfigured attribute returns 0 — it does not error.**
  `isOnShelf:true` silently matches nothing. **Use `isForSale:true`** (10.9M).
  This is nastier than it sounds: `isOnShelf` *is* present on the documents
  themselves (20 of 20 sampled), so the field looks real, and the zero comes back
  with `exhaustiveNbHits: true` — an answer that reads as a confident, exact "none".
  Every other attribute probed (`p2p`, `isForSale`, `isReserved`, `lastChance`,
  `weight`, `favouriteCount`, `favoriteCountBucket`, `price_SE.amount`,
  `firstOfferedAt_SE`, `priceDrop_SE.oldPrice.amount`, `saleStartedAt`) filters
  correctly.
- ⚠️ **`nbHits` is often an estimate — read `exhaustiveNbHits` on every query.**
  The unfiltered total is exact. Filtered results are exact while the result set is
  small and become estimates once it is large: measured 2026-08-08, 27,238 and 1,069
  and 672 all came back exact, while 238,577 and 925,882 and everything above them
  did not. The flip sits somewhere between those, so do not infer it from whether a
  filter is present — read the flag.
- **`saleStartedAt` is not the listing date.** It is when the current price step
  began; median gap to `firstOfferedAt_SE` is 79 days.
- **Sold items are deleted from the index.** 0 of 200 known-sold items remained,
  while 8 of 8 expired ones did. Disappearance is a usable sale signal, but only for
  items seen beforehand.
- **A single query shape stops paginating after ~2,000 results.** Reach comes from
  splitting the population into many shapes (price band × category) rather than
  walking one deep — see the band/category fan-out in `enrol.py`.

## Conduct

Algolia is third-party CDN infrastructure built for high query rates — the storefront
fires several requests per page view — so `algolia.py` runs at
`MIN_INTERVAL_S = 0.05` across 8 workers. **This is deliberately not the same
judgement as `sellpy.py`**, which talks to Sellpy's own Parse backend at one request
per second, strictly serial, because there the exposure is the account.

---

# The Typesense search index — added 2026-08-04

⚠️ **A ~5% subset, and no longer the primary discovery surface.** This section
originally called Typesense primary; that was wrong, and it predates the Algolia work
above. Keep it for one reason only: **`priceToEstimateRatio` and
`sellabilityEstimate` exist nowhere else** — not in Algolia, not in Parse. For
anything about the market as a whole, and for all discovery, use Algolia.
`docs/schema.md` accordingly calls this "the abandoned Typesense index".

Config comes from the GraphQL query `getTypesenseClientConfig` (see
`loppan/search.py`, which fetches it at runtime rather than hardcoding it). The
key is scoped and search-only — every visitor's browser holds the same one.

Collection: **`market_items`**, **584,041 documents**, of which **529,742 are on
shelf**. Deep pagination works (verified to page 150). Reading the collection
schema is forbidden to a search-only key, so fields were discovered by inspecting
returned documents and probing filter names.

## Fields that matter, and are absent from Parse

Absent from Parse, but **most of these are also in Algolia** — only
`priceToEstimateRatio` and `sellabilityEstimate` are exclusive to this index. The
rest are listed here because this is where they were first found, not because
Typesense is the place to read them.

| Field | Note |
|---|---|
| **`priceToEstimateRatio`** | **Sellpy's own current-price ÷ their value estimate.** Exclusive to this index, and the single most useful field found. |
| **`favouriteCount`** / `regularFavouriteCount` | Demand signal. Filterable but **not sortable**. |
| **`lastChance`** | Boolean — the item is near end of life. Powers `/store/selection/last-chance-items`. |
| **`price_SE`** | `{amount, currency}` — **amount is in ÖRE**. Filter path is `price_SE.amount`. 200000 = 2,000 kr. |
| `priceDrop_SE` | Present, usually null; not yet characterised. |
| `brandClassification.pricePoint` | Sellpy's brand price tier, 1–6. |
| `brandClassification` | Also `aestheticTone`, `ethos`, `originVibe`, `styles`, `ageGroups` — evidently LLM-generated. |
| `saleStartedAt`, `firstOfferedAt_SE` | Listing timestamps. |
| `isOnShelf`, `isReserved`, `saleType`, `p2p` | `p2p:true` = **a Circle listing**. |
| `embeddingV2` | A vector embedding per item. Similar-item search without any image work. |
| `keywords_sv`, `concept`, `style_sv` | Generated descriptive tags. |
| **`sizes`** | Array of coded sizes: `WMN-INT-M`, `WMN-EU-38`, `MEN-EU-48`, `SHOES-EU-40`, `PANTS-INCH-30`, `NO SIZE`. Present on **100%** of sampled documents. |

**`sizes` is not in `translatedMetadata_sv`** — it sits at the top level, and it is
the only place size is available cheaply. The Parse `Item` has `metadata.size`,
but reading it costs one request per item: 23 hours for 84,000 items versus zero
extra requests here, since the sweep already fetches these documents. Stored raw
and decoded at read time, so the display format can change without re-collecting.

**No price field is exposed at the top level** — `price` / `currentPrice` /
`salePrice` all 404. It is `price_SE.amount`, and it is in öre.

## Population **within Typesense**, measured 2026-08-04

⚠️ **Every count below is scoped to this ~5% subset. None of it is market-wide.**
Read them as "of the 584,041 documents in `market_items`", never as "of Sellpy".
The Circle row in particular understates the real pool by more than an order of
magnitude — see the Algolia counts underneath.

| Segment | Count |
|---|---|
| On shelf | 529,742 |
| **≥ 2,000 kr** | **700** |
| 1,500–2,000 kr | 1,562 |
| < 400 kr | 487,705 (92%) |
| **Circle listings (`p2p:true`)** | **15,008** (2,310 on shelf) — **subset-scoped, see below** |
| Circle + premium brand | 1,134 |
| Premium brand (tier ≥4) | 137,243 |
| `lastChance` on shelf | 12,389 — **none priced ≥1,000 kr** |
| `priceToEstimateRatio` < 0.5 | 61,975 |
| ≥5 favourites | 96,043 |
| ≥20 favourites | 13,317 |

Brand counts for the four known trades: COS 2,601 · Carhartt WIP 322 ·
Dr. Martens 251 · Ambika 56.

## The same Circle question, asked of Algolia (measured 2026-08-08)

| Query | `nbHits` | `exhaustiveNbHits` |
|---|---|---|
| index total, unfiltered | **12,475,724** | ✅ true — solid |
| `p2p:true` | 925,882 | ❌ false — an estimate, treat as an order of magnitude |
| **`p2p:true AND isForSale:true`** | **27,238** | ✅ **true — solid** |

**27,238 Circle items are currently for sale, against the 2,310 "on shelf" the
Typesense table implies — roughly 12×.** That exact figure is the one to quote:
it is the live-listing count and it comes back exhaustive. The ~926k `p2p:true`
total is an estimate and includes items no longer for sale, so it is not a
substitute.

Do not filter this with `isOnShelf:true` — that attribute is not configured for
filtering in Algolia and returns a confident-looking exact zero. `isForSale:true`
is the working equivalent.

## Recommended workflow

1. **Select** with **Algolia** — the whole market, filterable by brand, price band,
   Circle and favourites. This is the discovery surface.
2. **Add `priceToEstimateRatio` / `sellabilityEstimate` from Typesense** where the
   item is one of the ~6.8% that exist there. Nothing else needs this index.
3. **Enrich** with Parse by item id — the full markdown ladder, warehouse dwell,
   terminal status. One request per item, so select first.

## Warning about `priceToEstimateRatio`

A low ratio means the current price sits below Sellpy's own estimate. Items start
at or above the estimate and are marked **down** through the ladder — so a low
ratio is largely a measure of **how far the item has already been discounted**,
which is the pattern the four known trades associate with the *worst* returns
(see handover.md, Idea 4). Do not read it as "underpriced" on its own.

Combined with `favouriteCount` it is more interesting: heavily discounted **and**
widely favourited separates "cheap because nobody wants it" from "cheap and
wanted". That distinction was not measurable at all before this index.

---

# The curation engine (added 2026-08-05)

`sweep.py` walks items at **100 kr and above** (~165k of 529k on shelf, ~20 min at
Sellpy's maximum 250 per page) into `catalogue`, then rebuilds `brand_stats`.
Runs daily via `.github/workflows/sweep.yml`. Price and favourite history are both
written by database triggers, so the sweep upserts blindly.

**The floor moved from 200 kr to 100 kr on 2026-08-06** because both 5× trades on
record were bought at 55 and 170 kr — below the old floor, so the tool could not
have found the only trades that produced the target return. Storage is what stops
it going lower: at 1,412 bytes/row measured, a 50 kr floor puts `catalogue` alone
at ~470 MB of the 500 MB tier.

| Floor | On shelf | `catalogue` size |
|---|---|---|
| 400 kr | 33,549 | 47 MB |
| 200 kr (old) | 83,680 | 118 MB |
| **100 kr (current)** | **164,959** | **233 MB** |
| 50 kr | 333,643 | 471 MB |
| none | 520,903 | 736 MB |

## `refresh_brand_stats()` — do not restore the LATERAL (2026-08-06)

The first sweep at the 100 kr floor ingested all 165,536 rows and then **failed on
the last step**: `refresh_brand_stats()` hit `57014 statement timeout`, leaving
`brand_stats` describing the old 84k catalogue while `v_candidates` served
`brand_demand` from it. The sweep "succeeded" and the derived signal was silently
a day and half a catalogue out of date.

The cause scales with two things at once. The function joined a per-brand
`LATERAL` over the `season_clearings ∪ item_ladders` history, so it re-scanned the
whole union once per brand: ~12,200 brands × ~1,750 rows ≈ 21M row visits. Both
factors grew when the floor dropped.

Fixed by aggregating the history by brand **once** and joining, with the
`having count(*) >= 5` moved into that aggregate. Identical output, one pass:
**9.8 s for 20,230 brands**, down from a timeout. `service_role` also now carries
`statement_timeout = '240s'`, since this is a maintenance job called over PostgREST
where the default is tuned for API calls.

⚠️ If you ever rewrite this function, do not reintroduce the per-brand `LATERAL`.
It is not merely slow — it fails in a way that leaves stale data behind a
successful-looking run.

## Why `score` is stored, not computed (2026-08-06)

The dashboard's default view — `v_candidates?order=score.desc` — **failed outright**
once the catalogue reached 164k rows: 3.2 s against the anon role's ~3 s statement
timeout. A computed column in a view cannot be indexed, so every request evaluated
`expected_profit()` and `has_premium_fibre()` and joined `brand_stats` across all
rows, then sorted them.

Two steps were needed, and the first alone did not work:

1. **Store the computed scalars** in `item_scores` (score, expected_profit,
   worth_x_price, premium_fibre, out_of_season_now, cap_binds), indexed on the three
   sortable ones. Materialising the *whole* view would have duplicated images and
   arrays for ~230 MB; the scalars cost ~18 MB.
2. **Move eligibility into that table too.** With the price floor, `is_circle` and the
   category exclusions still living on `catalogue`, the planner had to scan and filter
   165k rows before it could sort, and the score index went unused — still 3.3 s.
   `item_scores` now holds *only* eligible rows and `v_candidates` is driven from it,
   so ordering walks the index and `catalogue` is touched only for the rows that
   survive the `LIMIT`.

| Query | Before | After |
|---|---|---|
| `order=score.desc&limit=50` | timeout | 1.25 s |
| full row + `count=exact` | timeout | 1.70 s |
| `order=expected_profit.desc` | timeout | 0.50 s |
| `worth_x_price=gte.5` + score | 0.70 s | 0.20 s |

`refresh_item_scores()` runs in `sweep.py` **after** `refresh_brand_stats()` — score
multiplies by `demand_index`, so scoring first bakes in yesterday's brand demand.
`out_of_season_now` depends on `CURRENT_DATE`, which is the other reason it is
recomputed daily. Takes ~22 s over RPC.

⚠️ `Prefer: count=exact` is now the most expensive thing a client can ask for: a full
scan of 164k rows. Use `count=planned` unless the filter is narrow.

## Scoring, and the assumption under it

Circle asks are capped at **5× what you paid** (confirmed across several items).
You keep 84%. So for an item worth V bought at P:

    ask    = min(V, 5P)
    profit = 0.84 × ask − P

V is unobservable, so Sellpy's own estimate stands in: `V = P / priceToEstimateRatio`,
making V/P the inverse of that ratio. The cap therefore binds at ratio ≤ 0.2, and
there profit is a flat **3.2 × P** — so among cap-binding items the *dearest* wins.

This inverts the naive reading of the first four trades. Those returned exactly
5× because they were pinned at the ceiling, not because cheap items are better.

`expected_profit()` is a **ceiling**, not a forecast: it assumes the item sells,
and that Sellpy's estimate approximates resale value. Multiply by a sell-through
probability once the cohort supplies one.

## Two traps found while building it

- **Pagination is unstable.** Walking 335 pages of a live index returns some items
  twice and skips others. Postgres rejects a batch with a duplicate key outright.
  Sort on a value that does not change, and keep a seen set.
- **Brand demand needs shrinking.** A brand with two listings, one with 103
  favourites, scored 51× the catalogue median. Shrunk toward 1.0 with a prior
  weight of 10 items; computed on the mean, since most items have zero favourites
  and the median would be 0 for all but the hottest brands.

## Category exclusions

Edit `excluded_categories` — a table, not view SQL, because the list will change.
Currently only `Prylar > Hemelektronik`. Watches are deliberately unaffected:
wristwatches are `Accessoarer > Armbandsur`, clocks are `Inredning > Prydnad >
Klockor`. Smartwatches under `Hemelektronik > Mobil & Wearables > Wearables` ARE
excluded — reverse that if they should count.

---

# Frontend access (added 2026-08-05)

Reads are gated on **Supabase Auth plus an allowlist**. Being signed in is not
enough on its own — anyone can sign up for a Supabase project, and the curated
shortlist is the whole edge.

| Caller | Sees |
|---|---|
| Anonymous (publishable key) | **three dashboard views only** — see below |
| Signed in, not allowlisted | the same three views, nothing more |
| Signed in **and** in `app_users` | everything |

Add a reader: `insert into public.app_users (email, note) values (...)`.

`app_users` deliberately has RLS on and **no policy** — no client should read it.
`is_allowed()` reaches it as SECURITY DEFINER.

Most views are `security_invoker = on`, so they respect the caller's policies
instead of the view owner's. Without that, selecting through a view bypasses RLS
entirely.

## Analytics slices (added 2026-08-06)

`market_slice(dim, min_n)` and `outcome_slice(dim, min_n)` aggregate in the
database and return one row per value of the chosen dimension. `p_dim` never
reaches SQL as text — it selects a branch, and both the expression and the FROM
clause are fixed strings; an unrecognised dimension raises rather than defaulting.

**Two populations, deliberately not pooled:**

| | Rows | Knows | Cannot answer |
|---|---|---|---|
| `market_slice` → `catalogue` | 165,924 live | price, likes, ratio, multiple, size, material, defect rate | anything about outcomes — none of it has sold |
| `outcome_slice` → `v_resolved_items` | 2,380 resolved | days on market, opening → final price, sell-through | size and likes for the 1,747 legacy ladder rows |

⚠️ **The outcome population is thin and biased, and both facts must travel with
every number taken from it.**

- **1,133 brands, only 5 with ≥30 observations.** Per-brand outcome metrics are
  noise today. `outcome_slice` defaults `p_min_n` to 30 for that reason.
- **~91% of it sold**, which is a sampling artifact — ladder pulls and one-day
  vanish detection both over-select fast sellers. It is *not* a market
  sell-through rate, and quoting it as one would be the single most misleading
  thing this project could publish. Sell-through comes from the cohort.
- Season, material and size are arrays, so an item counts once per value and
  column totals legitimately exceed the population.

Real values as of 2026-08-06, useful as a wiring check: `outcome_slice('season')`
gives Vinter 69 median days and 62.0% of opening price kept, against Sommar at 56
days and 70.0% — the seasonality direction §3 Idea 1 predicts, on a sample far too
small and too biased to confirm it.

## The sweep ledger (added 2026-08-06)

`sweep_runs` records every sweep: expected count, rows written, status, and the
**database's** date. Three silent-corruption paths close with it, and two of the
three had already nearly fired.

| Failure | What used to happen |
|---|---|
| Sellpy returns nothing (blocked, filter broken) | Sweep writes 0 rows, brand stats and scores rebuild on stale data, job **exits 0** |
| Sweep dies partway | ~66,000 live rows keep an old `last_seen`, so `resolve_outcomes` sees them as vanished and writes them all as `below_floor` — plus a 20× traffic spike at Sellpy |
| Sweep run 00:00–02:00 local | `dt.date.today()` on a UTC+2 machine is a day ahead of Postgres, so every *other* row looks vanished |

Guards, all enforced in the database so a caller cannot skip them:

- **`sweep_begin(expected, floor)`** raises on zero/null, and on a >50% drop against
  the last good sweep at the same floor — a catalogue does not halve overnight.
  Returns the run id **and `current_date`**, which the sweep uses for `last_seen`.
  The runner's clock is never consulted again.
- **`sweep_finish(run_id, written)`** records `ok` or `failed` at a 90% threshold and
  **returns a verdict rather than raising**. This matters: the first version raised,
  which rolled back its own status update, leaving truncated runs recorded as
  `running` forever. A ledger that misdescribes what happened is worse than none.
- **`sweep_abort`** is called from a `BaseException` handler, so `KeyboardInterrupt`
  and a killed process still land in the ledger.
- **`resolve_precheck(max_pct)`** — `resolve_outcomes.py` asks before trusting
  `last_seen`, and exits non-zero unless the last sweep is `ok` and under the churn
  ceiling. Normal churn is **0.39%**; the ceiling is 5%, twelve times headroom.

⚠️ With no run on record, `resolve_outcomes` **refuses to run** — by design. The
daily workflow sweeps first, so it self-heals; a standalone resolve before the next
sweep will exit 1 and say so.

## Hardening from the red team (2026-08-06)

**Writes are revoked, not merely blocked.** Every table had granted
`INSERT`/`UPDATE`/`DELETE` to `anon` and `authenticated`. RLS did stop them — a POST
returned an RLS violation, and a PATCH against a *matching* row returned `200 []`,
zero rows — but every policy here is `FOR SELECT`, so the grants bought nothing and
left the data one mistake from world-writable. Disabling RLS on a table while
debugging, or adding one permissive policy, would have exposed `catalogue` and
`excluded_categories` (which decides what is filtered out of the buy list) to
anonymous tampering.

`ALTER DEFAULT PRIVILEGES` was changed too, or the fix decays: Supabase's defaults
re-grant writes on every **new** table, which is exactly how `item_scores` acquired
them. Verified after: 0 tables writable by `anon`/`authenticated`, all 14 still
writable by `service_role`, and the collectors confirmed working end to end.

Anonymous writes now answer `401 permission denied for table …` where they
previously answered `204`.

⚠️ **A `204` on PATCH/DELETE does not mean the write was rejected.** With RLS on and
no matching policy, the rows are simply invisible to modify, so PostgREST reports
success having changed nothing. Testing writes requires a filter that matches a real
row and `Prefer: return=representation` — an empty array back means RLS filtered it,
rows back mean the write landed.

**Still open, deliberately:**

- Public signup is enabled (`disable_signup: false`). Anyone can reach the
  `authenticated` role. Harmless while every policy gates on `is_allowed()`, but it
  makes the allowlist the sole barrier. Turn off in Authentication → Providers.
- `is_allowed()` is executable by `authenticated` via `/rest/v1/rpc/`, so a
  signed-up non-member can ask whether they are allowlisted. Revoking `EXECUTE`
  from `authenticated` does not break the gate — the policies call it as
  `SECURITY DEFINER`.

## The gate was reopened, then closed again (2026-08-06)

**Current state: closed.** The three views below were anonymous for roughly a day so
the dashboard could run without a login. They are now back to
`security_invoker = on`, so reads require a session **and** membership of
`app_users`. Verified over HTTP with the publishable key: every table and view
returns `[]`, `app_users` returns 401.

The publishable key **was served in a public bundle** at `loppan.lovable.app`
during that window and must be assumed compromised. With the gate restored that is
harmless by design — the key grants nothing without a session, which is the entire
point of a publishable key. Rotate it if you want belt and braces, but it is not
load-bearing.

⚠️ **Policies here are scoped `TO authenticated`.** An unscoped policy looks
equivalent and is not: anon then *evaluates* `is_allowed()`, has no `EXECUTE` right
on it, and the endpoint answers `401 permission denied for function is_allowed`
instead of `[]`. Same access outcome, but it leaks the existence and name of an
internal function to an anonymous caller. `item_scores` was created unscoped and
had to be corrected — match the existing pattern.

⚠️ **The same leak arrives by a second route: the grant, not the policy.** Supabase's
default privileges hand `anon` a `SELECT` on every **new view**, and the write fix
above does not cover it. A `security_invoker` view with that grant sends `anon`
straight into `is_allowed()` on the base table and produces the identical
function-name error. `v_shortlist` was created this way on 2026-08-10 and needed an
explicit `revoke all ... from anon` — granting only to `authenticated` is **not**
enough, because the default grant is additive and silent. After adding any view,
check rather than assume:

```sql
select has_table_privilege('anon', 'public.v_yours', 'SELECT');
```

Then confirm over HTTP with the publishable key, which is the only test that proves
anything — the admin role proves nothing. Closed state is `401 permission denied for
view v_yours`.

The section below describes the *opened* configuration, kept because it is the
reference for how to reopen it deliberately.

## The three public views (2026-08-05, since reverted)

The Lovable dashboard runs without a login, so `v_candidates`, `v_cohort_summary`
and `v_circle_outcomes` were switched to `security_invoker = off`. They resolve as
their owner (`postgres`) and therefore bypass RLS on the base tables. **Treat
everything in those three views as public** — the publishable key is in the
browser bundle, so anyone who opens devtools can query them directly with curl.
The "private Lovable page" protects the page, not the data.

Base tables are untouched: RLS on, no anon policy, so they still return `[]`.
Verified over HTTP with the publishable key rather than in SQL, because the admin
role proves nothing:

| Endpoint | Anonymous |
|---|---|
| `v_candidates` | 200 — 11,964 rows |
| `v_cohort_summary` / `v_circle_outcomes` | 200 — rows |
| `v_cohort_status` | 401 — grant revoked |
| `app_users` | 401 |
| `catalogue`, `cohort_items`, `circle_roundtrips`, `brand_stats` | 200 `[]` |

`v_cohort_summary` reads through `v_cohort_status`, so that inner view also had to
resolve as owner. It is **not** part of the public surface — `anon`'s grant on it
was revoked, so it cannot be selected directly while `v_cohort_summary` still
reads it, because permission checks run as the view owner.

**The security linter now reports four `security_definer_view` ERRORs** for these
views. That is the intended consequence, not a regression: the linter cannot tell
a deliberately curated public view from an accidental RLS bypass. Do not "fix"
them without also putting the login back.

To reverse: set `security_invoker = on` on all four and re-grant `select` on
`v_cohort_status` to `anon`.

**Two things the Supabase linter caught that were genuinely wrong:**

- `refresh_brand_stats()` sat at `/rest/v1/rpc/refresh_brand_stats`, callable by
  **anon**. Anything in `public` is exposed over REST unless execute is revoked —
  so this was an unauthenticated endpoint that rebuilt 12,000 rows on demand.
  Revoked from `public`, `anon` and `authenticated`; only the service role calls it.
- Several functions had a mutable `search_path`, which lets a caller who can
  create objects shadow the tables a SECURITY DEFINER function resolves. Pinned.

Run `get_advisors(type=security)` after any schema change. It found both.

## Connecting a frontend

```
url:  https://zgqywowejxtokqsybqnu.supabase.co
key:  the PUBLISHABLE key (safe in a browser — it grants nothing without a session)
```

Sign in, then query. Everything is filterable and sortable:

```
/rest/v1/v_shortlist?order=discount_pct.desc&price_kr=gte.400
/rest/v1/v_shortlist?out_of_season=is.true&order=favs_per_month.desc
/rest/v1/v_shortlist?brand_sell_pct_day=gte.2&order=discount_pct.desc
```

⚠️ `v_candidates` no longer exists — it went in the v2 rehaul along with `catalogue`
and `item_scores`, and with it `score`, `expected_profit`, `cap_binds` and
`premium_fibre`. Its replacement is `v_shortlist`, which is a different quantity:
cheap against *live peers*, not against Sellpy's value estimate. There is no profit
column, because `price_to_estimate` is null on every live item since the rehaul.

`v_shortlist` is ~500 rows and every column is stored, so **fetch it whole and sort
client-side** rather than issuing a query per sort. `thumbnail` and `images` are
ready-built CDN URLs on `prod.images.sellpy.net`. Sellpy honours no resize
parameters, so scale client-side.
