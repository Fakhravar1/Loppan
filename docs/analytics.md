# The analytics layer

What gets computed after every track pass, what each number means, and the three ways
this data will lie to you if you query it naively.

`overview.md` is why the project exists, `schema.md` is what we collect, this is what we
derive from it. Everything here is rebuilt by `loppan/analytics.py`, which runs as the
last step of `.github/workflows/track.yml`.

---

## 1. What runs, and why the order matters

```bash
python loppan/analytics.py
```

Three database functions, in sequence, all *after* `track.py`:

| | Function | Writes | Cost |
|---|---|---|---|
| 1 | `refresh_peer_prices()` | `items.peer_*_frozen`, then rebuilds `peer_prices` | ~59 s |
| 2 | `snapshot_predictors()` | `predictor_daily` | ~65 s |
| 3 | `snapshot_brands()` | `brand_daily` | ~19 s |

Step 1 must come before step 2, because step 2 reads the frozen peer position as one of
its features. All three must come after `track.py`, for two separate reasons: peer groups
are built from live items at their current prices, so refreshing first would rank against
yesterday's shelf; and the freeze reads outcomes `track.py` has only just written.

All three **replace their own day's rows** rather than appending. Re-running after a
failure is safe and is the intended response to a timeout.

---

## 2. The peer freeze, and the bug it replaces

`peer_prices` answers "is this item cheap for its kind?" — the empirical stand-in for
Sellpy's value estimate, which per `overview.md` §6 exists on only ~5% of the market.

It was rebuilt from live items every pass, and the rebuild **truncated first**. So the
moment an item sold, its peer position was deleted. On 2026-08-08 the damage was exact:

| | had a peer row |
|---|---|
| 656,376 still-listed items | 656,296 |
| 15,616 resolved items | **0** |

Which made the question the table exists to answer permanently unanswerable — group by
peer decile against outcome and every bucket reads 0% sold, by construction, forever.

**The fix.** `refresh_peer_prices()` now copies the peer position of everything that
resolved since the last pass onto the item itself — `items.peer_pct_frozen`,
`peer_median_ore_frozen`, `peer_n_frozen`, `peer_level_frozen`, `peer_frozen_on` — and
only then truncates. The values are written once, when the item resolves, and never
updated after, which is what makes them joinable to an outcome.

Only the ~12k rows that resolved get written, not all 656k live ones. Writing every live
row onto `items` each pass would churn dead tuples through the largest table in the
database for no gain, and `track.py` already goes out of its way to avoid exactly that.

**One-time backfill, and its caveat.** The 15,616 items that had already resolved were
scored against the shelf as it stood on 2026-08-08 rather than the shelf they actually
competed on. The current shelf is missing the items that have since left it — about 2% —
so those percentiles sit fractionally high. They are flagged by `peer_frozen_on` being
*later* than `resolved_on`; everything frozen from 2026-08-09 onward has no such gap.

**`peer_level` 3 is the loosest rung and its groups are large.** Check `peer_n` before
trusting one — the levels run 46, 85 and 1,832 average peers.

It used to be far worse, and invisibly so. Level 3 groups on (brand tier, garment), and
`brands.price_point` was null for all 16,067 brands after the v2 rehaul, so the tier
collapsed into a single bucket and level 3 silently became "same garment, anywhere in the
market" at ~7,020 peers while still reporting itself as a tier comparison. Fixed
2026-08-08 by `loppan/backfill_brand_classification.py` — §7.

### Does it work?

Yes, in the middle of the market. Daily sale rate by peer position, 2026-08-08:

| position in peer group | <200 kr | 200–299 | 300–499 | 500–999 |
|---|---|---|---|---|
| cheapest quarter | 2.63% | 2.64% | 2.65% | **2.32%** |
| below median | 2.76% | 2.27% | 2.17% | 1.61% |
| above median | 3.01% | 1.95% | 1.45% | 1.45% |
| priciest quarter | 2.97% | 1.96% | 1.43% | **0.93%** |

Clean and monotone from 200 kr up — 2.5× across the range in the 500–999 kr band. Below
200 kr it **inverts**: being cheapest for your group stops helping, presumably because
everything there is already cheap and what binds is whether anyone wants the thing.

---

## 3. `predictor_daily` — which columns are doing the work

One row per pass × target × feature. Two targets, deliberately kept apart:

- **`sale`** — does this column separate items that sold from items that did not?
- **`price`** — does this column explain what an item is *asked* for?

Conflating them is how you end up with a model that predicts asking prices and calls it
demand. `is_reserved` is the fourth-strongest sale predictor and has an eta² of **0.0000**
on price. `condition` is the reverse: it moves price by 23% and sale rate by 9%.

Three numbers per feature, because none is honest alone:

| column | what it is |
|---|---|
| `lift` | 90th-percentile bucket ÷ 10th-percentile bucket. **Rank on this.** Percentiles rather than extremes, so one empty bucket among 969 brands cannot make it zero or undefined |
| `strength` | Cramér's V (sale) or eta² (price). Comparable across features, but **both rise with bucket count** — always read next to `buckets` |
| `best_/worst_` | the true extreme buckets and their rates, so a big lift off a 0.05% floor is visible as such |

Buckets below `p_min_bucket` (default 200) are dropped rather than merged: a brand with
four listings has no rate, and letting it set `best_rate` would make the board noise.

### The board on 2026-08-08 — sale

| feature | buckets | lift | Cramér's V | best | worst |
|---|---|---|---|---|---|
| favourites | 6 | **10.11** | 0.092 | 21+ → 3.77% | 0 → 0.17% |
| brand | 969 | 6.56 | 0.098 | Polarn O. Pyret → 8.24% | — → 0.00% |
| favs_per_month | 5 | 4.23 | 0.077 | 12+ → 3.50% | 0–1 → 0.40% |
| is_reserved | 2 | 3.73 | 0.063 | true → 11.10% | false → 1.79% |
| p2p | 2 | 3.25 | 0.008 | consignment → 1.88% | circle → 0.38% |
| size | 166 | 2.81 | 0.027 | CHILD-CM-74/80 → 4.89% | — |
| item_type | 218 | 2.58 | 0.037 | Buttondown-skjorta → 5.05% | — |
| price_band | 5 | 2.57 | 0.047 | <200 kr → 2.62% | 1000+ → 0.79% |
| peer_quartile | 4 | 1.96 | 0.042 | cheapest quarter → 2.60% | priciest → 1.12% |
| age_days | 5 | 1.89 | 0.031 | 121+ → 2.66% | 0–14 → 1.16% |
| brand_origin | 10 | 1.59 | 0.021 | Japanese → 2.37% | Korean → 0.98% |
| brand_tier | 6 | 1.56 | 0.016 | tier 2 → 2.32% | tier 1 → 1.33% |
| brand_ethos | 9 | 1.40 | 0.016 | Fast fashion → 2.40% | Disposable → 1.01% |
| season | 4 | 1.12 | 0.007 | autumn → 2.02% | summer → 1.76% |
| condition | 4 | **1.09** | 0.005 | Bra → 1.92% | Acceptabelt → 1.71% |
| has_defect | 2 | **1.05** | 0.004 | false → 1.91% | true → 1.79% |
| weight_g | 5 | **1.05** | 0.003 | 800+ → 1.94% | 151–300 → 1.82% |
| last_chance | 1 | 1.00 | 0.000 | — | — |

Favourites wins by a distance, and it is Algolia-only — Parse does not store it. Condition,
defects and weight are worth nothing for predicting a sale. `last_chance` has a single
bucket because the flag is false on every row in the sample; it is being tracked so that
its arrival is visible, not because it currently says anything.

### The board on 2026-08-08 — price

| feature | buckets | price ratio | eta² | dearest | cheapest |
|---|---|---|---|---|---|
| brand | 969 | 3.16× | **0.307** | Zimmermann 1,895 kr | Gildan Softstyle 135 kr |
| **brand_tier** | 6 | 2.98× | **0.131** | tier 6 → 784 kr | tier 1 → 173 kr |
| item_type | 218 | 2.09× | 0.103 | Brudklänning 1,409 kr | Nylonstrumpbyxor 155 kr |
| category | 63 | 2.19× | 0.086 | Kavajer & Kostymer 583 kr | Barn underkläder 159 kr |
| brand_ethos | 9 | 2.03× | 0.087 | Luxury Heritage 537 kr | Disposable 166 kr |
| weight_g | 5 | 1.77× | 0.068 | 800+ → 451 kr | 0–150 → 213 kr |
| favourites | 6 | 1.41× | 0.048 | 21+ → 444 kr | 3–5 → 267 kr |
| brand_origin | 10 | 1.67× | 0.027 | French 400 kr | Chinese 219 kr |
| condition | 4 | 1.23× | 0.009 | Nytt 369 kr | Acceptabelt 285 kr |
| **p2p** | 2 | **1.00×** | **0.000** | consignment 309 kr | circle 308 kr |

The three brand-classification features are the sharpest illustration of why the two
targets are kept apart. `brand_tier` is the second-strongest thing in the database for
explaining **price** (2.98×, tier 6 asks 784 kr against tier 1's 173 kr) and close to
worthless for predicting a **sale** (1.56×, and tier 2 outsells tier 6). Sellpy's tier
describes what a brand costs, not whether anyone is buying it today.

That last row is a direct answer to `overview.md` §10 question 2, and it is a no: Circle
sellers do not ask a premium. They ask the same money as consignment and, per §5 below,
sell about a fifth as often.

`price_band` and `peer_quartile` are excluded from the price target on purpose — both are
functions of price, so their eta² would be ~1 and would top the board while saying nothing.

---

## 4. `brand_daily` — brands pound for pound

One row per pass per brand with 25+ live listings. That bar keeps 1,362 brands covering
**94%** of the shelf; the other ~14,000 brands are 6% of listings between them and produce
nothing but noise.

The column that needs explaining is **`attention_index`**. Raw average favourites is a
size contest — a brand listing 300 kr dresses collects more likes than one listing 90 kr
babygros regardless of how wanted either is, because likes track price, garment and how
long the thing has been up. `attention_index` divides a brand's actual likes by what the
market average would give **the exact same basket** of (garment type × price band × listing
age). So:

- `1.00` — gets exactly the attention its mix deserves
- `2.50` — draws two and a half times what its basket predicts
- `0.30` — effectively invisible for what it is

Thin cells fall back to the price-band-and-age average rather than being dropped, because
dropping them would quietly exclude a brand's rarest garments from its own expectation and
flatter whichever brands list unusual things.

### 2026-08-08, brands with 150+ live listings

| brand | listings | avg favs | expected | index | ask | sold/day |
|---|---|---|---|---|---|---|
| Zimmermann | 223 | 56.0 | 19.7 | **2.84** | 2,030 kr | 0.89% |
| Arket | 777 | 39.7 | 15.4 | 2.59 | 405 kr | **8.16%** |
| Mango | 847 | 36.1 | 14.2 | 2.54 | 315 kr | 5.36% |
| Massimo Dutti | 796 | 40.8 | 16.3 | 2.51 | 440 kr | 6.02% |
| House of CB | 441 | 52.7 | 21.2 | 2.49 | 1,130 kr | 0.68% |
| COS | 810 | 38.5 | 16.1 | 2.39 | 435 kr | 7.10% |
| … | | | | | | |
| Sorel | 697 | 4.1 | 12.2 | 0.33 | 300 kr | 1.27% |
| Stone Island Junior | 422 | 3.0 | 10.4 | **0.28** | 395 kr | 1.63% |
| Jacob Cohen | 424 | 4.0 | 14.8 | 0.27 | 695 kr | 0.93% |
| Gown Gallery | 228 | 6.6 | 25.4 | **0.26** | 1,535 kr | 0.00% |

An 11× spread, and the useful part is the *interaction*: attention converts to sales at
mid prices (Arket 2.59 → 8.2%/day) and does not at high ones (Zimmermann 2.84 → 0.89%,
House of CB 2.49 → 0.68%). Wanting a thing and buying a 2,000 kr thing are different acts.

### Inflow — what is arriving, not what is sitting

`inflow_7d` counts items whose true `first_offered` falls in the seven days before `as_of`;
`inflow_pct` expresses that as a share of the brand's shelf, i.e. its restock rate. On
2026-08-08 the top of that list was Havaianas at 26.2% and Seafolly at 14.7% — flip-flops
and swimwear arriving in bulk at the end of a Swedish summer, which is `overview.md` §4.1's
seasonal dumping showing up live rather than in hindsight.

### Trends

```sql
select * from public.brand_trend(7, 100) order by attention_change desc;
```

Compares the newest snapshot against the newest one at least `p_days` older, per brand.
**It returns nothing until two snapshots that far apart exist** — on 2026-08-08 there is
exactly one, so every trend is undefined and the function says so by returning no rows
rather than reporting zero change. `days_apart` reports the real gap, which drifts from
`p_days` because `track` runs every other day.

---

## 5. Three ways this data will lie to you

### 5.1 A change and a disappearance cannot be seen in the same pass

This is the one that produces confident, backwards conclusions.

When a pass runs, every item is in exactly one of two states: **it is there**, so we can
read its new price and like count, or **it is gone**, so we can read nothing. It can never
be both in the same pass. So an item whose price we watched drop is, by that very fact, an
item that had not yet sold when we looked.

Query it naively and you get:

| | items | sold | rate |
|---|---|---|---|
| price unchanged | 586,871 | 12,474 | **2.13%** |
| price cut | 82,232 | 177 | **0.22%** |

"Cutting the price makes an item 10× less likely to sell." It does not. The same shape
appears for likes — "gaining favourites makes items less likely to sell", 2.27% vs 0.32%.
Both are the artefact, not a finding.

The concrete case: item `pSI6GJNNT5`, a Killstar corsett with 393 favourites. At 10:25 UTC
it was still listed and had just been cut 420 → 350 kr. At 11:14 UTC it was gone, and Parse
confirmed it sold for 350 kr. It only appears as both because *three* passes ran that day.

**The rule: compare a change seen in pass N−1 against an outcome in pass N. Never same-pass.**
`predictor_daily` avoids this by only using state read at the start of the pass.

### 5.2 Weighted and unweighted answer different questions

The market-wide daily sale rate on 2026-08-08 is **1.85% unweighted** and **3.95%
weighted** — a factor of 2.1. Neither is wrong. The unweighted number describes the
*sample*; the weighted one, using `sample_weight`, estimates the *market*.

The gap is real and not a rounding artefact: within every single price band, rows with
`sample_weight > 20` sell 2.4–2.7× faster than rows below it. High weight means a
high-population brand that was under-sampled — and liquid brands sell faster. Any figure
put in front of a human must say which of the two it is.

### 5.3 The legacy rows are survivors

The 2,819 rows with `stratum = 'L'` are the migrated Typesense-era sample. They are 100%
resolved and read 86% sold, which is the survivor artefact `overview.md` §5 warns about,
not a sell-through rate. **Every function here filters to `stratum in ('A','B','N')`.** Any
new query must do the same.

---

## 6. Storage

`brand_daily` is ~424 kB per pass, or roughly 77 MB a year at the every-other-day cadence.
`predictor_daily` is negligible. The five frozen peer columns cost ~12 MB once and then
grow only as `items` does.

The database is at **281 MB of the 500 MB free tier**. `overview.md` §5 already names
storage as the binding constraint on how much of the market can be tracked; `brand_daily`
is now the fastest-growing thing that is not `items` itself, and is the first candidate for
a slower cadence or a retention window if that ceiling starts to bite.

---

## 7. The brand classification backfill

```bash
python loppan/backfill_brand_classification.py            # only brands still missing it
python loppan/backfill_brand_classification.py --all      # re-read every brand
```

All six of `price_point`, `styles`, `age_groups`, `origin_vibe`, `ethos` and
`aesthetic_tone` were null for all 16,067 brands after the v2 rehaul. `schema.md`
documented them as present; enrolment never carried them across. The cost was quiet —
level-3 peer groups eight times too wide (§2), and `dash_slice(p_dim => 'brand_tier')`
returning nothing at all.

They did not need a new source. Every Algolia document already carries the whole thing
inline as `brandClassification`, so the fix reads three live items per brand — 276 requests
— rather than re-sweeping 6,700. Run 2026-08-08: **14,895 of 15,492 brands resolved**, 596
had no live item left in the index, and only 972 live items (0.15%) remain on an
unclassified brand.

Three items per brand rather than one, for two reasons. Sold items vanish from Algolia
immediately, so a single sampled id can easily come back empty. And sampling more than one
lets the script *check* the assumption everything downstream rests on — that the
classification is constant per brand — rather than trusting it. `schema.md` claimed this
on 857 brands; it now holds on **14,896, with exactly one disagreement** (Conhpol), which
is left unwritten rather than resolved by coin flip.

### A trap this exposed, worth knowing before writing another RPC

**PostgREST silently truncates a set-returning function at 1,000 rows.** The first run
asked for ~46,000 rows, got 1,000, and cheerfully reported "334 brands to classify" against
a real figure of 15,492 — a wrong answer that looked entirely like a right one. `db.query`
pages around this with Range headers and says so in its docstring; `db.rpc` does not.

`brand_sample_items()` therefore returns a single `json` value instead of a set, which is
the same dodge `dash_overview()` uses. **Any new set-returning RPC expecting more than
1,000 rows has this bug until proven otherwise.**
