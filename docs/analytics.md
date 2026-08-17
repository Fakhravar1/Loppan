# The analytics layer

What gets computed after every track pass, what each number means, and the three ways
this data will lie to you if you query it naively.

`overview.md` is why the project exists, `schema.md` is what we collect, this is what we
derive from it. Sections 2–7 are rebuilt by `loppan/analytics.py`; §8, the dashboard
shortlist, by `loppan/shortlist.py` immediately after it. Both run as the closing steps of
`.github/workflows/track.yml`.

---

## 1. What runs, and why the order matters

```bash
python loppan/analytics.py
```

Eight database calls, in order, all *after* `track.py`. The first six are the peer
rebuild, split out of what used to be a single `refresh_peer_prices()`:

| | Function | Writes | Cost |
|---|---|---|---|
| 1 | `freeze_peer_prices()` | `items.peer_*_frozen` | ~10 s |
| 2 | `stage_peer_live()` | truncates `peer_prices`, fills `peer_live` | ~14 s |
| 3 | `score_peer_level(1)` | `peer_prices`, brand + garment + condition | ~10 s |
| 4 | `score_peer_level(2)` | `peer_prices`, brand + category | ~8 s |
| 5 | `score_peer_level(3)` | `peer_prices`, tier + garment | ~8 s |
| 6 | `release_peer_live()` | empties `peer_live`, clears `peer_stage_state` | <1 s |
| 7 | `snapshot_predictors()` | `predictor_daily` | ~51 s |
| 8 | `snapshot_brands()` | `brand_daily` | ~19 s |

Step 6 was added 2026-08-17 and is the only step here that exists for **disk** rather
than for data. See "Releasing the shelf" below.

`refresh_peer_prices()` still exists and still does all of 1–5 in one transaction. It is
for running **by hand against the database**, where there is no gateway — it takes
70–99 s and therefore cannot be called over the API at all. See below.

⚠️ **`snapshot_predictors()` crossed the 60 s limit on 2026-08-11**, hours after the peer
rebuild was split for the same reason. It is *not* split, because it builds two temp
tables and emits both targets from shared aggregates — splitting by target would not
halve it, and rewriting an 8 KB statistics function to chase a timeout risks changing
the numbers the project exists to produce.

Instead `analytics.py` **verifies rather than trusts the socket**. The statement commits
regardless of the hang-up — verified: it returned `RemoteDisconnected` while writing all
40 rows, both targets, correctly — so on a dropped call the step is re-checked against
`predictor_daily` for that `as_of` and treated as done if the rows are there. See
`SETTLED` in `analytics.py`.

Two properties that keep this honest, both tested: rows genuinely **absent** still fail,
and a verification that **cannot reach the database** fails too, because unknown belongs
in front of a human rather than quietly passing. It polls for up to 150 s, since the
rows land some seconds *after* the gateway hangs up — asking once, immediately, would
answer no and be wrong.

This is a workaround and reads like one. The real fix is to make the statement finish
inside 60 s; until then the day's data is correct and the job says so.

Step 1 must come before step 2, because step 2 reads the frozen peer position as one of
its features. All three must come after `track.py`, for two separate reasons: peer groups
are built from live items at their current prices, so refreshing first would rank against
yesterday's shelf; and the freeze reads outcomes `track.py` has only just written.

All three **replace their own day's rows** rather than appending. Re-running after a
failure is safe and is the intended response to a timeout.

The three are also **independent enough to carry on past each other's failures** —
losing the brand snapshot is no reason to also lose the predictors — and `analytics.py`
catches per-step failures to make that true.

⚠️ **That only worked for HTTP errors until 2026-08-11.** `db.rpc` wrapped
`urllib.error.HTTPError` into `RuntimeError`, which is what `analytics.py` catches, but
let every *connection-level* failure through as its own type — `RemoteDisconnected`,
`ConnectionResetError`, socket timeouts. Those escaped the handler and killed the whole
script at whichever step hit one. A dropped connection on step 1 on 2026-08-10 and again
on 08-11 therefore cost `predictor_daily` and `brand_daily` for both days, which cannot
be backfilled — see §5.4. `db.rpc` now normalises connection failures to `RuntimeError`
too, so a blip costs one step instead of three.

Note the wording of that error: it says the *call* failed, not that the work did not
happen. A long RPC usually commits server-side even when the socket dies — which is why
`peer_prices` carries a 2026-08-11 stamp from a pass that reported failure.

### ⚠️ `refresh_peer_prices` no longer fits in one HTTP request

**It takes 99 s.** Timed directly against the database on 2026-08-11, scoring 647,051
rows — up from the ~56 s in the table above, measured 2026-08-08. **Supabase's API
gateway cuts a request at 60 s**, so the call now fails every single time with
`RemoteDisconnected`, while the statement runs on and commits regardless.

Two false trails, recorded so they are not walked again. It looked like the VPN, because
it succeeded on 08-08 and 08-09 and failed on every run from 08-10, the day the tunnel
went up — pure coincidence, the function crossed 60 s at about that moment. TCP
keepalives were added on that theory and did not help, and could not have: **no
client-side setting extends a gateway's request limit**, and neither does raising
`RPC_TIMEOUT`, which only governs the socket.

It also matters that this is *step 1 of 3*. Each retry leaves a 99 s statement running
server-side, so re-running `track` while a previous one is still going produces
`55P03 canceling statement due to lock timeout` on `snapshot_predictors` — a second,
confusing failure caused entirely by retrying the first.

**Fixed 2026-08-11 by splitting it into five calls** (steps 1–5 in §1). Two calls would
not have been enough, which is worth knowing before someone tries the obvious thing:
freeze is only 10 s, so the rebuild half would still have been ~59 s warm and ~89 s cold
— sitting exactly on the limit and failing whenever the cache was cold.

Two structural obstacles shaped the split:

- **The old `_live` was a temp table**, `on commit drop`, and each API call is its own
  connection. The rebuild could not be split at all while it depended on one. It is now
  `peer_live`, a real UNLOGGED table — unlogged because it is rebuilt from `items` every
  pass and worthless after one, so crash-safety would be pure write cost.
- **`truncate` now commits on its own.** The rebuild used to be a single transaction, so
  readers saw the old data until it was complete; split, `peer_prices` is genuinely empty
  for ~40 s. That is survivable here and only here: no view reads `peer_prices`, and the
  one code reader — `shortlist.py` — runs after analytics in the same workflow and
  already exits cleanly on an empty table.

**Two guards exist because splitting removed the ordering that "it is all one statement"
used to give for free**, and this is the one place in the project where wrong order
destroys unrecoverable data:

- `stage_peer_live()` refuses to truncate while any resolved item still holds an unfrozen
  peer row. Without it, a failed freeze followed by a successful stage would throw away
  the peer position of every newly-resolved item — the exact bug §2 records as fixed.
- `score_peer_level()` refuses to run against staging older than 30 minutes, so a failed
  stage cannot leave yesterday's shelf to be scored as though it were today's.

`analytics.py` also skips the rest of the `peer` chain once one of its steps fails, so
the guards are the second line rather than the first. Both were verified by simulating
the failure, not by reading the code.

Verified after the split: 365,938 + 209,589 + 71,524 = **647,051 rows, identical to the
single-call total**, worst step 14.1 s.

### Releasing the shelf (step 6, added 2026-08-17)

Making `peer_live` a real table instead of a temp table is what made the split possible,
and it introduced a cost nobody accounted for: **a real table stays.** `peer_live` is one
row per live item — 603,693 of them — and ~62 MB with its three indexes. The line above
says it is "worthless after one [pass]", and that was true, but nothing emptied it. It
sat at full size in the meantime, and since `track.yml` runs `30 4 */2 * *` the meantime
is *two days out of every two days*. Those 62 MB were permanently allocated to a
scratch table.

That was a large part of what took the database to 642 MB against Supabase's 500 MB
limit. `release_peer_live()` empties it once the three levels have scored;
`stage_peer_live()` rebuilds it from `items` at the start of the next pass regardless, so
the release costs nothing but the <1 s truncate.

⚠️ **Clearing `peer_stage_state` is the substance of that function, not tidying up
beside the truncate.** `score_peer_level()` guards with

```sql
select now() - staged_at into age from public.peer_stage_state;
if age is null or age > interval '30 minutes' then raise exception
```

which reads the marker and **never inspects `peer_live`**. A fresh marker over an emptied
table is therefore the one state in which scoring passes its own guard, finds nothing to
score, inserts nothing, and returns 0 *as a success* — manufacturing precisely the silent
failure the guard exists to prevent, for the 30 minutes after every pass. Clearing the
marker lands the guard on its `age is null` branch, whose message is "was never staged
… Run stage_peer_live() first" — true, and the right instruction, since that is the
function which refills the table.

Verified by simulation rather than by reading, per the standard the guards above set:
after `release_peer_live()` returned 603,693, `score_peer_level(1)` raised
`P0001: peer_live is stale or was never staged (age: never)`.

The release is **inside** the `peer` chain, so a failed level skips it and leaves the
table populated — which is the state a retry wants. Do not fold the truncate into
`score_peer_level(3)`: a step that tidies up after itself is a step that cannot be re-run.

A fourth step runs after these three, as its own script rather than part of
`analytics.py`, because it is the only one that makes network requests:

```bash
python loppan/shortlist.py
```

It reads what all three write — `peer_prices` for the ranking, `brand_daily` for the
per-brand columns — so it must come last. See §8.

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

## 5. Four ways this data will lie to you

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

### 5.4 The series has a hole at 2026-08-10 and 2026-08-11

`predictor_daily` and `brand_daily` have **no rows for either day**. The last snapshot
before the gap is 2026-08-09.

This is a gap, not a zero, and the difference matters for every comparison in §4's
Trends and §3's board: a query that diffs "the last two snapshots" silently spans three
days across this hole, and any per-day rate computed from it is wrong by a factor of
three. Filter on `as_of` explicitly rather than trusting adjacency.

**It cannot be backfilled.** Both tables are snapshots of the *live* market on the day
they run — live listing counts, current asking prices, attention. Re-running
`analytics.py` now stamps today's market with today's date; it cannot reconstruct what
the shelf looked like on 10 August. The two days are simply gone.

Cause: the Raspberry Pi runner livelocked from 2026-08-10 (docs/pi-runner.md, "The
second livelock"), and a dropped connection aborted `analytics.py` before the two
snapshot steps ran — see §1. Both are fixed; the hole stays.

What is *not* affected, and it is the part that matters: **`cohort_checks` is unbroken**,
with an observation every day through the incident. The forward cohort is the project's
one live experiment and it runs on hosted runners, which is exactly why it survived a
dead Pi. `enrol` and `cohort check` staying hosted is a deliberate choice — see
docs/pi-runner.md — and this is the incident that justifies it.

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

---

## 8. `shortlist_daily` — the undervalued grid

The dashboard's buy list: ~500 live items priced low against comparable live listings,
rebuilt each pass by `refresh_shortlist()` and given its pictures by
`loppan/shortlist.py`.

### Why it is a table and not a view

Ranking the live shelf on peer-relative cheapness costs a parallel sequential scan over
693k `items` rows plus a sort — **3.0 s measured**, against the anon role's ~3 s statement
timeout. That is the same wall `item_scores` was built to get around in the v1 dashboard
(`api-notes.md`, "Why `score` is stored, not computed"), rediscovered because the v2
rehaul dropped `v_candidates` and nothing replaced it.

So everything the grid sorts on is **stored**, pre-joined and denormalised onto 500 rows.
The same query against the finished table is **4.9 ms** — 600× faster, and small enough
that the frontend fetches the whole thing once and sorts in the browser.

### What "undervalued" can mean today, and what it cannot

Only one thing: **cheap relative to live peers**, from `peer_prices`.

⚠️ **There is no profit estimate any more, and there cannot be one.** The old
`expected_profit` / `cap_binds` scoring divided by `priceToEstimateRatio`, which came from
the Typesense ~5% subset. `items.price_to_estimate` is now **null on all 671,075 live
rows**. Anything reintroducing a profit number needs a new source for V first — do not
reconstruct it from `discount_pct`, which is a comparison against other *asking* prices,
not against value.

### The gate

Each clause removes something that is *not an opportunity*, rather than something merely
unattractive — the grid sorts on everything, so taste is the reader's job.

| Clause | Default | Why |
|---|---|---|
| price floor | 200 kr | below it the peer signal **inverts** outright (§2) |
| `peer_n` | ≥ 30 | a percentile off six peers is noise |
| `peer_level` | ≤ 2 | level 3 averages 1,832 peers and compares across brands |
| `favourites` | ≥ 1 | 0 favourites sells at 0.17%/day against 3.77% at 21+ |
| `is_reserved` | false | someone is already buying it |
| per brand | ≤ 15 | one badly-formed peer group would otherwise flood all 500 slots |

Ranked on `discount_pct` descending. **`rank` is selection order, not a recommendation** —
it records how a row got in, not that rank 1 is the best buy.

### Two ways this screen will mislead a reader, by construction

**A large discount is often a mismatched peer group.** A Burberry *Foder* (a lining) shows
an 86% discount because its peer group is Burberry outerwear. Nothing filters this out, so
`peer_n` and `peer_level` are printed on every card and the interface names the grouping in
words rather than showing a level number.

**Cheap is not the same as sells.** The top of the list runs to evening gowns, and
`brand_daily` puts several of those brands at **0.00% sold/day** — Gown Gallery among
them. `brand_sell_pct_day` therefore sits on every card, and a zero is called out. A
discount on something nobody buys at any price is not a discount.

### Images

`shortlist_daily` is the only place in the database with any. They are fetched from
Algolia for the shortlist only — about five requests — because the URL is **not derivable
from `item_id`** (`schema.md`, "The image claim was wrong"). Paths are stored without a
host; `v_shortlist` prepends the CDN.

Absence from Algolia is recorded rather than discarded: sold items are deleted from that
index within the day, so `still_listed = false` is a free liveness check on the picks.
First run, 2026-08-10: **18 of 500 had already gone** in the hours since the pass.

### The pool is now complete, and paged server-side (2026-08-10)

`shortlist_daily` holds **every eligible item — 38,926 — not a top-N sample.** The old
`p_n` cap was a workaround for the browser, and it leaked into the answers: filtering to
men's INT L returned 29 of a real 680, and shoes in EU 42 returned 5 of 202.

| | Before | Now |
|---|---|---|
| Rows stored | 2,000 | **38,926** |
| Where filtering happens | browser | Postgres |
| Rows per page | all of them | **60** |
| Men's INT L | 29 | **680** |
| Shoes EU 42 | 5 | **202** |

Measured: the table is **65 MB**, a filtered-and-sorted page runs in **8.9 ms**, and over
HTTP a page lands in **65–90 ms** including the exact count.

Three pieces make it work:

- **`shortlist_facets()`** returns every distinct filter value as one `json` scalar
  (~52 KB: 1,163 brands, 306 item types, 497 size combinations). The grid holds 60 rows,
  so it can no longer build its own dropdowns. A `json` scalar rather than a set,
  because PostgREST truncates set-returning functions at 1,000 rows without erroring — §7.
- **`size_key`** — `area|system|value` in one column, so a saved size profile is a single
  `IN` list rather than an OR-chain over three columns.
- **Indexes** on `(as_of, …)` for every offered ordering, plus the size tuple.

⚠️ **The eligible set must never be re-derived per request.** Deriving it from `items` +
`peer_prices` is a 693k-row scan measured at 3.0 s — over the anon statement timeout. It
is materialised once per pass and only read afterwards.

The grid pages with `useShortlistInfinite`: 60 rows a request, the exact count on page 1
only, and an IntersectionObserver 600 px above the end so the next page arrives before
the reader does. Any filter, sort or profile change resets to page 1 — appending page 2
of a different question under page 1 of this one would silently blend two result sets.

### Why the pool is stored rather than swept live at browse time

A tempting alternative is to hold nothing and query Sellpy when someone browses. It does
not work, for two independent reasons.

**Ranking needs the group, not the item.** "Undervalued" means cheap against the median
of its peer group, so every item on screen needs its group's median. Live, that is one
Algolia request per distinct group — roughly 60 for a 60-item page, one to three seconds
per scroll, every scroll, billed to Sellpy per operation. From the table it is 65–90 ms.

**It would not save what it appears to save.** Projected at full coverage, the pool is
~280 MB of a ~1.3 GB total — `items` (~664 MB) and `peer_prices` (~302 MB) are the bulk,
and neither can go, because the peer comparison and the whole statistical layer are built
on them. Removing the pool leaves ~970 MB, still twice the free tier.

**The lever that does work, if storage binds:** keep the pool but make the row narrow.
Store only what can be sorted or filtered on — the scalars, ids and booleans, ~200 B
instead of ~1,750 — and join `items`/`brands`/`lookup` for the 60 rows being displayed.
That is ~32 MB rather than ~280 MB at full coverage, and server-side paging already makes
it straightforward, because the join only ever runs on a page's worth of rows.

### Retention: one day of the pool, ninety of what it recommended

The full pool is ~68 MB *per day*, so only the current day is kept. The longitudinal
question — *did the items we flagged actually sell?* — is preserved separately in
**`shortlist_flagged`**: the top 500, per-brand capped, at ~60 B a row, kept 90 days for
under 3 MB. Fat table for browsing, skinny table for history.

### Gates removed 2026-08-10, by decision rather than measurement

`is_reserved`, `favourites >= 1` and `price < peer_median` were dropped. Net effect on
the pool is small — **36,723 → 38,926** — because `peer_pct <= 0.25` dominates
everything downstream of it.

Worth knowing what was given up: items with zero favourites sell at **0.17%/day against
3.77% at 21+**, so ~45k items now in the pool are things nobody has shown any interest
in. That is deliberate — the grid's own `favourites` sort and filter do the work instead
of a hard gate — but if the pool starts feeling like landfill, `p_min_favourites` is
still there and setting it to 1 restores the old behaviour.

### ⚠️ "2,335,917" is not the market — correction, 2026-08-10

That figure is `sum(population)` over `brand_band_population`, and it was quoted as the
size of the market repeatedly before anyone checked. It is not. `brand_band_population`
is built from `algolia.brand_facets()`, which the API caps at **1,000 brands** — its own
docstring says that covers "~59% of items". So 2.34M is the frame for the largest
thousand brands, not the catalogue.

It was caught by an impossible comparison: Algolia reports ~5.1M wearables at **200 kr
and above**, which cannot be smaller than the same market at 100 kr and above.

**Use the design weights instead.** `sample_weight` exists precisely to estimate the
population from the sample, and it gives a coherent answer:

| | Weighted estimate |
|---|---|
| Live wearables | **5,925,145** |
| …at 200 kr and above | **3,293,860** |

Treat both as estimates with real error bars — Algolia's own large filtered counts come
back `exhaustiveNbHits: false` and disagree by tens of percent — but they are
design-based rather than an artefact of a truncated facet list.

Every projection made against 2.34M before this date is understated. The corrected ones
are below.

### What a full crawl would cost

The waterfall from the market to the pool, measured 2026-08-10:

| Gate | Survivors |
|---|---|
| Algolia frame — wearables 100 kr+ | 2,335,917 |
| **we actually hold** | **671,247** (28.7%) |
| stratum A/B/N | 659,889 |
| price ≥ 200 kr | 436,656 (66.2%) |
| has a peer group at level 1–2 with ≥ 12 members | 334,692 (76.7%) |
| **in the cheapest quarter of its group** | **38,926** (11.6%) |

Two cuts do nearly all the work: **coverage**, and the cheapest-quarter rule — which is
not a filter so much as the definition of the thing.

Projecting to full coverage, the last ratio is scale-invariant (a quarter of each group
is a quarter however big the group gets) while the peer-group ratio *rises*, because
thin groups thicken and clear the 12-member floor. At 88–93% clearing:

> **~150,000–170,000 items in the pool, central estimate ~160,000.** About 4× today.

⚠️ **That does not fit the free tier, and neither does the crawl behind it.** Projected:
`items` ~664 MB (from 185), `peer_prices` ~302 MB (from 81), `shortlist_daily` ~280 MB
(from 65) — **roughly 1.3 GB against a 500 MB limit.** The full crawl is a paid-tier
decision before it is an engineering one; Supabase Pro's 8 GB covers it with room.

### ⚠️ Sorting is not selection — resolved, kept for the reasoning

The grid sorts on 21 columns, client-side, over every row it holds. But those rows are
**chosen** by `p_n` — top N by `discount_pct` — so sorting reorders that set and cannot
reach past it. Filtering makes this visible immediately, and it is the first thing a
real user hit.

**Measured 2026-08-10.** Filtering the grid to men's INT L returned **8 items**. The
same filter against everything that passes the gate returns **680**, and against every
live item we hold, **16,325**. The shortage was never coverage — it was `p_n`.

`p_n` moved 500 → **2,000**, and `p_keep_days` 30 → **14** to pay for it: at ~1,851 B a
row the full 36,943-row eligible pool is ~68 MB *per day*, while 2,000 × 14 days is
~52 MB against the 281 MB already in use. Men's INT L went 8 → 29.

**It is still a sample, and still selected by the most error-prone criterion we have.**
Discount ranking pulls toward the extreme tail, where a big number is more often a
mismatched peer group than a bargain. An item 45% below a well-evidenced 200-listing
group is a better buy signal than 90% below a 12-listing group, and it still may not
appear.

Two ways further, neither taken:

| | Effect | Cost |
|---|---|---|
| Store the whole eligible pool | Filtering becomes exact rather than a sample | ~68 MB/day; forces server-side sort and filter, since a browser cannot hold 37k rows |
| Rank on `peer_pct` instead of discount | Less mismatch-prone | Loses "how cheap" as the organising idea |

The first is the real answer once storage allows it. It also stops being a browser
problem and becomes an indexed-query problem, which is the easier of the two —
`shortlist_daily` is indexed on `(as_of, size_area)` and `(as_of, size_group,
size_system, size_value)` for exactly that future.

---

## 9. The rotation sweep — covering a market you cannot afford to store

Built 2026-08-10. The pool stops being derived from `items` and starts being swept
directly, a quarter of the brands at a time.

### The idea it rests on

You do not have to **keep** a peer group, only to know it long enough to rank against
it. That distinction is what makes the whole market affordable.

Levels 1 and 2 group on `(brand, item_type, condition)` and `(brand, category)` — both
entirely inside a brand. So a sweep that takes **whole brands** holds every usable group
*complete* while it works, ranks against it exactly, keeps the cheap tail, and throws the
rest away.

| | Storing everything | Rotation |
|---|---|---|
| Held to compute the comparison | ~558 MB | **~61 MB** (one bucket) |
| Coverage | full market | full market, over the cycle |
| Peer groups | complete | **complete** |

### Buckets

`crc32(brand) % 24`. No mapping table, new brands assign themselves, and the buckets
divide the target market roughly evenly (at twelve they averaged 118,031 items and 557
brands, the largest holding 10.37% against an even 8.33%; twenty-four halves both).

**Four, then twelve, then twenty-four — each step forced by a different ceiling, and
the sequence is worth keeping.**

The first attempt used four. A bucket is held whole in `sweep_staging` while its groups
are computed, and at a measured 433 bytes a staged row, a quarter of the market projects
to ~142 MB against a 500 MB **storage** tier. The first full-bucket run was stopped
partway when the arithmetic became clear. Twelve put that peak at ~61 MB.

Twenty-four came from a different ceiling: **memory, not storage**. Peak RSS of a pass
scales with the items it stages — 3.7, 4.3 and 4.1 KB per staged item, measured across
buckets 5, 4 and 3 on 2026-08-11 — and bucket 3's 66,003 items peaked at 264 MB against
a 400 MB cgroup ceiling on the Pi. That was the thinnest margin on the box. Halving the
bucket halves the term that was growing. See docs/pi-runner.md, "The second livelock".

Raising the count costs nothing in cycle time if the job runs more often: twenty-four
buckets at twelve runs a day is a **two-day** rotation, faster than the three days
twelve-at-four gave. It also makes each run shorter, so a failure costs less.

⚠️ **Changing this reshuffles every brand**, since the bucket is `crc32(brand) % BUCKETS`.
That is safe and self-healing rather than a migration: the new buckets hold no rows, so
`next_sweep_bucket` — which orders by `max(swept_on)` with `nulls first` — sweeps them
before anything else, and `refresh_pool_bucket`'s delete-by-`item_id` clears each brand's
stale rows from its old bucket as that brand reappears in staging. Expect the pool to
look lopsided for a rotation while that works through.

⚠️ **`pool_refresh` is not bucket-scoped** — it reads the whole pool every time it runs,
so its cost scales with how often the *workflow* fires, not with bucket size. When the
sweep went to twelve runs a day the refresh stayed at four (00/06/12/18 UTC, gated in
`pool.yml`), so prices are no staler than before and the extra sweep frequency is close
to free. If you raise the run count again, gate that step again with it.

`public.crc32()` reproduces Python's `zlib.crc32` exactly, verified on ASCII and UTF-8
brand names, so SQL and `loppan/sweep_pool.py` can never drift apart. Deliberately not
`hashtext()` (unreproducible outside Postgres) and not Python's `hash()` (salted per
process, so it would reshuffle every brand on every run).

### What runs

```bash
python loppan/sweep_pool.py            # today's bucket
python loppan/sweep_pool.py --bucket 2
python loppan/sweep_pool.py --limit 40 # a slice, for testing only
```

`sweep_staging` holds the bucket, `refresh_pool_bucket()` computes its peer groups,
writes the survivors into `shortlist_daily` with `swept_on`, and **truncates staging**.

⚠️ **A row with no bucket used to be immortal, and is now reclaimed (2026-08-13).** The
cleanup was `delete from shortlist_daily where bucket = p_bucket`, and `bucket = null` is
never true — so the 37,010 un-bucketed rows `shortlist.py` wrote over the pool on
2026-08-11 were invisible to every sweep's cleanup. They could only be removed by the
delete-by-`item_id` above, and then only if the same item happened to be staged again;
**28,364 were still being served on 08-13, half of them in buckets that had already been
re-swept.** Rankings frozen on 08-11 that nothing would ever replace, kept price-fresh by
`pool_refresh` so they read as current.

The clause is now `where bucket = p_bucket or bucket is null`. A row without a bucket is
by definition not owned by the rotation, so the rotation is free to reclaim it, and any
future writer that bypasses the bucket design gets undone within one sweep.

`not null` on `bucket` would be the stronger guard and **cannot be used**: `pool_refresh`
upserts a partial payload keyed on `item_id`, and PostgREST validates a complete insert
tuple before resolving the conflict — see the note on `db.update` — so it would fail the
whole daily price-and-liveness refresh.

### First full bucket, 2026-08-10 — measured

| | |
|---|---|
| Brands in bucket 0 | 1,550 |
| …with 8+ live items, so able to form a group | **446** |
| Items staged | **41,212** |
| Kept in the pool | **8,363** |
| Peak staging | **17 MB** |
| Wall time | **13.2 min** |

Every invariant held: `peer_n` never below 12, `peer_pct` never above 25, only levels 1
and 2, every row imaged, and staging emptied at the end.

**Twelve buckets therefore projects to ~494,500 items swept and ~100,400 in the pool —
about 77 MB — with a 17 MB staging peak. Roughly 388 MB in total, comfortably inside
the tier.**

⚠️ **The weighted extrapolation was 2.9× too high**, and this is the first *measured*
figure to check it against. `sample_weight` put bucket 0 at 118,031 items; the sweep
found 41,212. Two reasons, both structural rather than a bug: 1,104 of 1,550 brands
turned out to have fewer than 8 live items in the target sizes and were skipped, and
per-brand weights are noisy where we hold few items at high weight. Treat design-weight
projections as an upper bound from here.

### Three constraints worth writing down

⚠️ **`peer_level` 3 becomes permanently uncomputable.** It groups on brand tier across
the whole market, and no single bucket holds that. The shortlist gate is already
`peer_level <= 2` so nothing is lost today — but that gate can never be relaxed while
the sweep is bucketed by brand. Rare luxury needs a different mechanism.

⚠️ **Freshness is uneven by design.** A brand swept on Monday is four days old by
Thursday. `swept_on` carries that so the interface can show it rather than hide it.
Prices and `still_listed` can be refreshed daily across the whole pool for ~1,300
Algolia requests; the peer median cannot, and is as old as its bucket.

⚠️ **The ten largest brands are swept incompletely, and the bias runs the wrong way.**
A single Algolia query shape stops paginating at about 2,000 results, so a brand is
capped at ~6,000 items across the three size shapes. Ten brands market-wide exceed that
— Zara 21,390, & Other Stories 9,998, COS 9,219, H&M 9,162, Levi Strauss 8,889, Adidas,
Nike, Arket, Hugo Boss, Polo Ralph Lauren — with 54 more between 2,000 and 6,000 and so
at risk per shape.

For those brands the peer group is **not** complete, which is the premise the whole
rotation rests on. Worse, the truncation is not random: `enrol.py` measured that Algolia's
relevance order favours expensive items, so a capped brand's sample skews expensive, its
median comes out too high, and its items look cheaper than they are. That is the same
direction of error as a thin peer group — it manufactures false bargains.

The fix is the one `enrol.py` already uses: fan a large brand out by price band so each
shape stays under the cap. Until then, treat discounts on those ten brands with suspicion.

⚠️ **`sweep_staging` must never be copied into `items`.** It is deliberately biased — a
quarter of the brands, and only sizes that fit two specific people. `items` is the
stratified sample with known inclusion probabilities that `brand_daily`,
`predictor_daily` and every sell rate depend on. Pooling them would quietly turn every
market estimate into "…among things that happen to fit us". §8 makes the same point
about the candidate pool; this is the same rule one level further out.

### `sizes` is what an item FITS; `metadata.size` is what it is LABELLED

This distinction caused two false alarms in one afternoon and is worth stating plainly.

The sweep filters on Algolia's **`sizes` array**, which lists every size an item actually
fits. What we store in `shortlist_daily.size` is **`metadata.size`**, the single display
label. They frequently differ:

| Stored label | `sizes` array | Why it matched |
|---|---|---|
| `MEN-INT-L/XL` | `['MEN-INT-L', 'MEN-INT-XL']` | straddle labels are expanded into their halves |
| `MEN-EU_REGULAR-42R` | `['MEN-EU_REGULAR-42R', 'MEN-EU-52']` | a 42R suit **is** an EU 52; both are carried |

So a row whose stored size is not in `target_sizes` can be perfectly on-profile. Verified
on bucket 1: of 11,877 rows, 11,320 carry an exact target code, 556 are straddle labels
covering one, and the single remaining outlier was the 42R suit above — **zero genuine
leaks**.

⚠️ Two consequences. Any audit of "is the pool within profile?" must check the array
semantics, not the stored label, or it will report a 5–8% leak that does not exist. And
the straddle rows in `target_sizes` are **redundant** — enabling them adds nothing,
because the plain codes already match those items. They are kept, disabled and
annotated, so nobody re-adds them expecting more stock.

### The legacy pool was dropped, 2026-08-11

The 33,460 rows left from the old full-rebuild path were deleted once the rotation was
proven. They came from the stratified sample across **all** sizes, so **67.4% of them
were sizes that fit nobody here** — and because they carry no bucket, no sweep would ever
have claimed them and the rotation would never re-find them. They would have sat there
indefinitely with peer medians frozen at the day they were built.

After the drop the grid is 58,357 rows across five swept buckets, of which **58,352 fit
the two profiles**.

**The five that do not are all suits**, and the reason is worth knowing before someone
calls it a leak:

```
Kostym  label = WMN-EU-34   fits = ['WMN-EU-34', 'WMN-EU-38']
```

A two-piece carries a size for each piece. That one matched on the trousers being EU 38
— a target size — while the jacket it is labelled with is EU 34. So half the garment
fits. At 5 rows in 58,357 it is not worth filtering, but it is the one case where
matching on the `sizes` array over-reaches.
