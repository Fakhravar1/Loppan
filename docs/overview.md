# Loppan — what this is, and what it is for

Orientation document. Read this first; `handover.md` has the evidence and the arguments,
`api-notes.md` has the mechanics.

**Status as of 2026-08-06: measurement only.** Nothing here buys anything, automates a
purchase, or writes to Sellpy. The purpose is to find out whether a business exists before
committing money to it.

---

## 1. The idea

Sellpy sells second-hand clothing two ways, and the two halves behave differently.

**Consignment.** You post items in a bag, Sellpy photographs, prices and lists them. The
price then walks *downwards* automatically on a markdown ladder until the item sells or
reaches end-of-life. The seller does not control the price and cannot stop the clock.

**Circle.** A private seller lists an item they own, at a price they choose. No automatic
markdown. They keep 80% of the sale, or 84% if the payout is taken as Sellpy credit.

The asymmetry that follows from those published terms:

> Consignment sellers operate under a price ladder and an end-of-life. Circle sellers set
> their own price and do not auto-discount.
> **You own a shelf; they own a countdown.**

The proposed trade is to buy items cheaply from the consignment side, hold them, and
relist them on Circle at a price of your choosing.

The application exists to answer whether that trade is profitable often enough to be worth
doing, and if so, to identify which items to buy.

---

## 2. How the business would operate

One cycle:

1. **Find** an item on consignment priced below what it is worth.
2. **Buy** it, at price `P`.
3. **Hold** it — storage is free, capital is not.
4. **Relist** it on Circle at your own price.
5. **Sell** it, for `S`, keeping share `k`.
6. **Recycle** the proceeds into the next purchase.

The constraint that shapes everything is that step 5 is not guaranteed. Items that never
sell are the whole risk, and they are the hardest thing to observe.

### The equations

The identity the entire business rests on:

```
profit = s · k · S − P
```

| Term | Meaning |
|---|---|
| `s` | sell-through — the fraction of bought items that ever sell |
| `k` | the share of the sale price you keep |
| `S` | the price it sells for |
| `P` | what you paid |

**`k` is known and is a lever.** Three routes, all operator-supplied and not yet
independently verified:

| Route | You keep | Effective fee |
|---|---|---|
| Circle payout as cash | 0.800 | 20.0% |
| Cash, next purchase on Amex (1.35% cashback) | 0.811 | 18.9% |
| **Circle payout as Sellpy credit (+5%)** | **0.840** | **16.0%** |

Taking the payout as credit beats cash-plus-cashback by ~3.6 percentage points per cycle,
and the two do not stack — credit funds the next purchase directly, so no card is involved.
The rule: **recycle proceeds as credit; use a card only for fresh capital from outside.**

**`s` is unknown.** This is the number that decides whether the business exists, and it has
never been measured. It cannot be recovered from history, for reasons in §5.

**`S` is uncertain**, and §4.3 shows it is not simply "what the item is worth".

**Expected profit, as currently implemented:**

```
expected_profit = P × ( 0.84 / ratio − 1 )      capped at 3.2 × P
ratio           = current asking price ÷ Sellpy's own value estimate
```

This is `0.84 × estimate − P` rearranged. The cap binds at `ratio ≤ 0.2`, i.e. when an item
appears to be worth five times its price.

⚠️ **The 0.84 in that formula is doing two different jobs.** As a *fee* it is solid
arithmetic. As part of a *buy filter* it silently assumes the item sells at Sellpy's own
estimate — an assumption that has never been tested and, per §5, cannot be tested
retrospectively. The filter was withdrawn on 2026-08-06 for that reason; the ratio now
ranks candidates but no longer excludes any.

---

## 3. What we actually know

Four hand-picked round trips exist. They returned +106% on completed trades, three of four
sold. They establish that the upside exists and nothing else — they were selected after the
fact and there is no denominator.

Beyond that, as of 2026-08-06:

- **2,380 resolved items** with full price histories and outcomes.
- **1,300 items** enrolled in a forward-looking cohort on 2026-08-04, being followed to
  their fate. This is the only unbiased measurement of sell-through in existence here.
- **240 observed Circle round trips by other sellers** — people already attempting this
  trade. They ask **1.3–1.4×**, not 5×. Of those visible, 163 expired unsold after roughly
  six months and **none were observed to sell** — though successes are structurally
  invisible (§5), so this cannot be read as a sell-through rate.

---

## 4. Working theories

### 4.1 Idea 1 — Wrong month (seasonality) · **CONFIRMED 2026-08-06**

People clear out closets seasonally. Winter coats arrive in spring, get listed in April,
sit all summer and are cheapest in July when nobody wants a coat. They are cheap because of
the *month*, not because they are bad. Sellpy tags season natively, so no inference is
needed.

Measured across 1,317 seasonal items, as the median fraction of opening ask retained at
sale:

| Winter-tagged items | | Summer-tagged items | |
|---|---|---|---|
| August | **33.1%** (92 days) | January | **43.8%** (71 days) |
| December | **83.5%** (32 days) | June | **73.3%** (42 days) |

Two independent groups in opposite phases, both moving as predicted. Buying at the
out-of-season level and selling at the in-season level is roughly **2.4× gross, ~2.0× after
the Circle fee**.

This is the best-supported idea in the project and the only one with a quantified edge.

### 4.2 Idea 2 — Wrong price from the start

Sellpy's pricing sometimes gets an item badly wrong on day one. A wool-and-cashmere coat
opened at 55 kr; that is not decay, it is an error, and the error is the entire profit. The
two items Sellpy opened cheap (55 and 170 kr) both returned 5×; the two it opened
expensively (1,070 and 1,480 kr) returned 1.4× and 2.0×.

What to look for is a *contradiction between Sellpy's own two signals*: their model says the
item will sell, yet the opening price is low for the brand and condition. Neither signal
works alone — the two highest-scoring items were the two worst trades.

**Status: plausible, untested.** Testing it needs the opening ask, which requires the
ladder, which is only available per item.

### 4.3 Idea 4 — The refusal ceiling · **withdrawn**, but it left something behind

The claim was that a long markdown ladder records the market refusing an item at every price
above where you bought, capping resale. It does not survive: the sandals that supposedly
proved it were summer sandals listed in December, which Idea 1 explains completely, and they
sold after only a 6% cut.

What replaced it is stronger and more uncomfortable. **Ladders are monotone** — 46 increases
in 8,633 price transitions (0.53%). Therefore:

> **Every price you can buy a consignment item at is greater than or equal to the price that
> item later sells for.**

You cannot buy an item below its own clearing price. Buying and reselling into the *same*
market is structurally loss-making before fees. That leaves exactly three places an edge can
come from:

1. **Circle buyers pay more than consignment buyers** — different venue, different intent,
   no auto-discount. *Unmeasured.*
2. **Items that expire unsold** — their ladder never revealed a market price, so the final
   ask is not evidence of value. *Unexploited.*
3. **Seasonal timing** — §4.1. *Confirmed.*

"Spotting underpriced items" is not on that list. This is the single most important
structural finding in the project.

### 4.4 Idea 3 — Forgotten stock (warehouse dwell)

One coat sat 440 days between being sorted and being listed; the other three sat 2, 11 and 2
days. An item forgotten for a year may get dumped on the shelf at a clearance price
regardless of worth. **n = 1.** Recorded because it is free to record.

---

## 5. Constraints

### Hard rules — non-negotiable

- **Read-only.** Nothing authenticates as a user or writes to Sellpy, ever.
- **One request per second**, enforced in code. The risk that matters is the account, not
  the scraper.
- **Never submit fabricated data anywhere.**
- **Predictions are frozen before results are seen.** A prediction made afterwards is not one.

### Methodological hazards — the ones that have actually bitten

- **Sold items are deleted from the search indexes.** Both of them. This is the central
  hazard of the whole project: any backward-looking sample sees failures and survivors but
  never a success. It is why the cohort exists, why "91% sold" in historical data is an
  artifact, and why the 240 observed Circle round trips show zero sales.
- **The value estimate cannot be recovered historically.** `priceToEstimateRatio` exists only
  in one index, which holds only currently-listed items. Once an item resolves, the estimate
  it carried is gone. Neither term of the buy rule is recoverable backwards — both must be
  frozen on the way in.
- **Thin cells look like insight.** 2,380 resolved items spread across 1,133 brands. Per-brand
  outcome numbers are noise, and any dashboard showing them must say so.

### Technical limits

- **Supabase free tier: 500 MB.** Currently ~185 MB. This is the binding constraint on how
  much of the market can be tracked.
- **`Item` cannot be enumerated.** Discovery must run through search indexes or `MarketOffer`;
  `Item` is enrichment-by-id only.
- **Most `MarketOffer` query shapes time out.** Unordered pagination works to ~9,000 rows;
  date and price filters, ordering, and `first: true` all fail.
- **The buy signal covers ~5% of the market.** See §6.

---

## 6. Where the data comes from

Three sources, none sufficient alone.

| Source | Holds | Limitation |
|---|---|---|
| **Algolia** `prod_marketItem_se_relevance` | ~12.5M documents — the real storefront index | No value estimate, no sellability score |
| **Typesense** `market_items` | 586,746 documents | **A ~5% subset of Algolia** |
| **Parse** `MarketOffer` / `Item` | Full markdown ladders and outcomes, retained long after an item ends | One item at a time, or 60 per batched query |

The discovery on 2026-08-06 that reframed the project: **the storefront browses Algolia, and
we had been sweeping Typesense.** Verification showed only 6.8% of Algolia items exist in
Typesense, while 99.8% of Typesense items exist in Algolia — a clean subset relationship,
roughly uniform across price band, brand tier and listing age, so a *sample* rather than a
curation.

The consequence: population figures used in earlier decisions were 20–75× too small. Per-item
data remains valid; denominators do not.

The complication: **`priceToEstimateRatio` and `sellabilityEstimate` exist only in Typesense.**
The buy signal is structurally limited to ~5% of the market, and that may well be what the
Typesense index *is*.

---

## 7. What the application does

Four screens, all read-only, none of which buys anything.

**Candidates.** Everything currently purchasable, filterable and rankable. Filters on brand,
category, size, material, condition, defects, season, brand tier, price and the derived
signals. The headline control is the **multiple** — how many times the asking price the item
is estimated to be worth — with 5× as the stated target. Images are shown large, because
condition is judged by eye and the studio photographs hide what the phone photographs reveal.

**Cohort.** The live experiment: 1,300 items enrolled on a frozen date across six strata,
followed to their outcome. This screen exists to answer sell-through, and it is the only
screen whose numbers will settle whether the business is real.

**Round trips.** Completed Circle round trips — what was paid, what was asked, what it
fetched, how long it took.

**Insights.** Any dimension sliced by any measure, over two deliberately separated
populations: the *live market* (~165k items — price, likes, discount, materials) and
*resolved outcomes* (~2,380 items — days on market, opening and final price, sell-through).
These are never merged, because doing so would present asking prices as sale prices.

**How this works.** A plain-language explanation of the theories, the economics, and — most
importantly — what is not yet known.

The application's job is to make the *state of the evidence* legible, not to look confident.
Every computed number carries its caveat in the interface, not in a footnote.

---

## 8. The regression model

The intent is to learn, from items whose outcome we have observed, a rule for which items to
buy. **This is currently on hold** — see §8.3.

### 8.1 Columns we would test

Item attributes, from the storefront index:

| Group | Columns |
|---|---|
| Identity | brand, category, item type, demography, segment |
| Physical | size, materials, fabric, colour, pattern, **weight**, measurements (waist, length, inner leg) |
| Quality | condition, defects |
| Timing | **first offered at**, season tags |
| Price | current price, previous price (observed markdown), opening ask, price band |
| Demand | favourite count, regional favourite buckets (Nordic / EU / DACH) |
| Flags | last chance, reserved, Circle vs consignment |

Brand-level attributes, from a brand table rather than repeated per item — verified constant
per brand across 857 brands:

> price point (tier 1–6), styles, age groups, origin vibe, ethos, aesthetic tone

Three of these deserve their reasoning stated:

- **Weight** — shipping is a real per-item cost and weight is its direct input. A 0.22 kg vest
  and a 0.6 kg pair of jeans are not the same trade at the same margin.
- **First offered at** — the *true* listing date. The obvious-looking field, `saleStartedAt`,
  is when the current *price step* began; the median gap between the two is **79 days**. A
  "days on market" feature built on the wrong one measures time-at-current-price and calls it
  age.
- **Previous price** — an *observed* markdown, present on 95% of items, requiring no valuation
  model at all.

### 8.2 Columns we are deliberately leaving out

| Column | Why |
|---|---|
| `itemAbTestFraction` | Sellpy is running experiments on these items. Capture it to *detect* a confounder; never train on it. |
| `relevanceRanking*`, `proximityBucket*` | Sellpy's own ranking and personalisation. These *cause* sales by controlling visibility — endogenous, and they leak the outcome into the features. |
| `personalization_sizeCategory` | Derived from fields already included. |
| `estimateBid_rounded` | Present on ~1% of items. |
| `keywords`, `concept`, `style` | Generated text tags. Possibly useful later; not first. |
| `storeIds`, `storageSite`, `bag`, `itemIO`, `itemPackaging`, `user` | Operational identifiers, not item properties. |
| `images` | Not stored at scale — reconstructible, and 4.7 URLs per item is the single largest per-row cost. |
| Per-item brand classification | Normalised to the brand table instead. |

### 8.3 Why the model is on hold

A pilot on the 2,380 resolved items, cross-validated on held-out data:

- The best single feature is **brand, at R² 0.151** — and it fails to generalise on 45% of
  held-out rows, because most brands are seen once or twice. Condition and material score
  *worse than predicting the mean*.
- **Final price is 73% explained by the opening ask alone** (slope 1.025). A valuation model
  learned from these features would reproduce "0.72 × the opening ask" — a restatement of the
  ladder, not an edge.

Spending months collecting features that explain ~15% of variance would be expensive
preparation for a model the pilot predicts cannot clear a 16% fee.

The two questions that decide the business are **temporal, not cross-sectional**:

1. Do Circle buyers pay more than consignment buyers for comparable goods?
2. What is Circle sell-through?

Neither needs a new pipeline. The second is already running in the cohort.

---

## 9. How the crawler works

Four scheduled jobs, all read-only, all rate-limited to one request per second.

**Daily catalogue sweep.** Walks the searchable market above the price floor and records the
current state of every item — price, likes, size, materials, condition, season, flags. It
upserts blindly; price and favourite history are written by database triggers on change, so
the sweep does not need to read the previous state back.

**Outcome resolution.** An item still visible in today's sweep is, by definition, still
listed — that costs nothing. Only items that *vanished* need investigating, and vanishing has
two meanings: sold, or marked down past the collection floor. Parse is asked which, because it
is authoritative. Cost is proportional to churn (~0.4% daily), not to catalogue size.

**Weekly stragglers.** Items that fell below the price floor are still alive and still
resolve, and they matter disproportionately: of items that opened above 100 kr, 43% of sales
and **83% of failures** ended below it. Dropping them would discard failures at twice the rate
of successes and bias sell-through upward. Because ladders are retained long after an item
ends, checking weekly loses no information.

**Daily cohort check.** The 1,300 frozen items, followed to their outcome.

### What keeps it honest

The sweep writes to a **ledger** before it writes any data, and three guards sit on it:

- It refuses to run if the source reports zero items, or fewer than half the last good run —
  a catalogue does not halve overnight, so that means the source changed, not the shelf.
- It records a truncated run as **failed** rather than successful.
- Outcome resolution **refuses to run** unless the last sweep completed and the apparent churn
  is under 5% — twelve times normal. Without that, a sweep dying at 60% would make ~66,000 live
  items look vanished and get them recorded as gone.

The date stamped on every observation comes from the **database**, never the machine running
the job — a laptop at UTC+2 running a sweep near midnight would otherwise write tomorrow's
date and make every other row look stale.

### The intended direction

Discovery would move to the storefront index, which is ~20× larger, with the value estimate
retained from the smaller index where it exists. The limit is storage, not requests: tracking
the full market does not fit in 500 MB, so the design is a **stratified sample followed to
outcome** rather than a census — statistical power comes from the number of *resolved* items,
not listed ones.

---

## 10. The questions that decide it

In order of how much they matter:

1. **What is Circle sell-through?** Running now. ~60 days. A 2× multiple at 40% sell-through
   loses money.
2. **Do Circle buyers pay more than consignment buyers?** Unmeasured, and §4.3 means the
   trade needs this or seasonality to work at all.
3. **Does the seasonal edge survive contact with Circle?** §4.1 measured it on consignment
   prices on both sides.
4. **Does Sellpy's sellability score predict *our* sell-through?** They compute p(sell) per
   item and hand it over free. If it transfers, the hardest term is solved.

Everything else — the dashboard, the scoring, the crawler — exists to make those four
answerable.
