# Analytics dashboard — architecture plan

Planning document. Supersedes the first Lovable dashboard, whose views were dropped in
the v2 rehaul.

> **Status, 2026-08-10.** Screen 3 (relative pricing) is **built and live** as the
> *Undervalued* grid — `shortlist_daily` / `v_shortlist`, `loppan/shortlist.py`, and a
> picture grid in the Lovable app. See `analytics.md` §8. Everything else below is still
> plan.
>
> Building it confirmed principle 1 the expensive way a second time: the ranking query
> was **3.0 s** against a ~3 s timeout until the results were stored, then **4.9 ms**.
>
> It also settled one thing this document assumed was available. Screen 3 was meant to
> replace Sellpy's value estimate — but `price_to_estimate` is now **null on all 671,075
> live items**, so the shortlist can only say *cheap against live peers*, never *cheap
> against worth*. The peer comparison is not a stand-in for value; it is a different
> question that happens to be answerable.

---

## Three principles, all learned the expensive way

**1. Aggregate in the database, never in the browser.** The first dashboard timed out
because `ORDER BY score` made Postgres evaluate two functions and a join across every
row on each request. A computed column in a view cannot be indexed. Anything the
dashboard sorts or filters on must be *stored*, and anything it aggregates must be
precomputed or served by a function.

**2. Separate what is live from what has ended.** They answer different questions and
blending them presents asking prices as sale prices. The old dashboard kept them apart
and the new one must too.

**3. Every number carries its sample size and its caveat, in the interface.** With
1,133 brands over a few thousand outcomes, per-brand numbers are noise until they
aren't. A dashboard that hides `n` invites confident nonsense.

---

## What the data can support today

| | Rows | Notes |
|---|---|---|
| Live items | ~664,000 | Full attributes, price, likes, refreshed daily |
| Items with an outcome | 2,897 → growing | ~1,800/day observed disappearing, adjudicated by Parse |
| Legacy resolved (with ladders) | 2,380 | Full price histories, the seasonal evidence |

**Outcomes accumulate fast enough to matter.** At the measured rate (53 of 20,000 in a
day), a week gives ~12,000 observed sales, a month ~50,000. Most screens below are thin
on day one and meaningful within a fortnight.

⚠️ **"Newly sent in for sale" does not work yet.** Our sample is frozen — nothing
enrols new listings, so the newest items we hold are the ones enrolled on 2026-08-07.
This screen needs the scheduled new-listing enrolment first. Until then the inflow
figure can only come from a live Algolia count, not from our own data.

---

## Screens

### 1 · Selling now

*What is actually moving, this week.*

- Sales per day, last 30 days — the top-line pulse
- Top item types by units sold, with median and mean price
- Top brands by units sold, and by revenue
- Median days from listing to sale, and its distribution
- **Markdown depth at sale** — how far below its own first price it went
- **Sold-after-markdown rate** — what share of sales happened within 7 days of a price
  cut. This is the closest thing to a causal read on whether cutting price works

Every panel filterable by category, price band, condition, season, demography.

### 2 · Feature overlap among sellers — *"what do the things selling have in common?"*

The interesting version of this is not "60% of sales were cotton" but **lift**: how much
more common a feature is among items that sold than among items sitting on the shelf.

```
lift(feature) = share of feature among SOLD  ÷  share among LIVE
```

Lift above 1 means the feature is over-represented in sales. Computed for material,
colour, brand, brand tier, price band, category, item type, condition, season, size
group, defect, weight band.

Two things make this honest rather than decorative:

- **A base-rate column next to every lift**, because a feature can have high lift purely
  by being rare.
- **Confidence bounds**, or at minimum a hard `n` threshold. With 12,000 sales spread
  across hundreds of materials, a lift of 3.0 on n=8 means nothing.

Second-order view: **feature pairs**. Which combinations sell together more than their
individual rates predict — e.g. does *wool × out-of-season* beat wool alone.

### 3 · Relative pricing — *"is this cheap for what it is?"*

**This is the most valuable screen, because it is the empirical replacement for Sellpy's
value estimate** — the number §5.3 records as untestable and only present on 5% of items.

For each live item, find its peers and place it among them:

| Peer definition | Fallback order |
|---|---|
| brand + item_type + condition + colour | most specific |
| brand + item_type + condition | if fewer than ~20 peers |
| brand + category | |
| brand tier + item_type + condition | if the brand itself is too thin |

Then store, per item:

- `peer_median_ore` — what comparable items ask
- `peer_price_pct` — its percentile within that group (0 = cheapest of its kind)
- `peer_n` — how many peers, so a thin comparison is visible
- `peer_level` — which fallback tier was used

A cheap item is then simply one with a low `peer_price_pct` and a large `peer_n`. That's
a buy signal built entirely from observed prices, with no dependence on Sellpy's model.

**The natural next step, once outcomes accumulate:** replace *asking* peers with *sold*
peers — `peer_sold_median` — so the comparison becomes "what do these actually clear
at", which is the quantity the business case needs.

### 4 · Crowding — *"how much competition will I face?"*

Your point that inflow drives competitiveness, made measurable:

- Live item count per peer group, and its trend over time
- **Supply/demand ratio**: live items in a group ÷ recent sales from that group. A high
  ratio means a glut and slow resale
- Inflow by category and brand, week over week
- Inventory age profile — how much of the shelf is stale, by segment

This is what tells you whether relisting into a given niche means waiting behind 400
identical items.

### 5 · Demand momentum

The one thing only Algolia carries, and the reason the sweep is daily:

- **Likes per day since listing**, not raw likes — an old item with 20 likes is not the
  same as a three-day-old item with 20
- Likes gathered in the first 7 days, as a leading indicator
- Whether likes-velocity predicts speed of sale, by segment
- Regional split (Nordic / EU / DACH) — an item wanted abroad is a different proposition

### 6 · Seasonal position

Driven by the one confirmed finding — winter items retain 33% of ask in August and 83%
in December:

- Current month's in-season and out-of-season segments
- Items whose season is approaching, ranked by discount — the buy list the seasonality
  theory implies
- Realised seasonal curve, updating as our own outcomes accumulate, against the
  historical ladder curve

### 7 · Experiment status

Not analytics, but the numbers that decide whether any of this is a business:

- Cohort sell-through by stratum, with the frozen predictions beside the results
- Circle vs consignment outcomes, when data exists
- Sample health — coverage per brand, weighted-vs-population checks, tracking freshness

---

## Computation layer

Three tiers, by cost:

**Precomputed nightly, after `track.py`** — expensive, changes slowly:

| Table | Contents | Cost |
|---|---|---|
| `peer_prices` | per item: peer median, percentile, n, level | window functions over 664k rows |
| `agg_daily` | (date, dimension, value) → units, median price, median days | one pass over resolved items |
| `agg_feature_lift` | (feature, value) → sold share, live share, lift, n | two passes |
| `agg_crowding` | (peer group) → live count, recent sales, ratio | one pass |

**On-demand RPC**, mirroring the `market_slice` / `outcome_slice` pattern that worked:
`slice(dimension, measure, filters)` with a strict dimension whitelist — `p_dim` selects
a branch, never reaches SQL as text.

**Direct table reads** for anything indexed — item lists, drill-downs.

The dashboard should never issue a query that scans `items` unaggregated. `count=planned`
rather than `count=exact`, always.

---

## Refresh

```
04:30  track.py            sweep, outcomes, price paths
05:15  refresh_analytics() peer prices, daily aggregates, lift, crowding
05:20  cohort check
```

Aggregates are a day stale at worst. Nothing here needs to be live-to-the-minute, and
pretending otherwise would cost far more than it's worth.

---

## What this needs that doesn't exist yet

1. **Scheduled enrolment of new listings** — without it, screen 1's "newly listed" is
   empty and the sample ages out as items resolve.
2. **Ladder pull at sale time** — gives the true opening price, which makes "markdown
   depth at sale" exact rather than measured from our arbitrary first sighting.
3. **A few weeks of outcomes.** Screens 1, 2 and 5 are thin until then. Screens 3, 4
   and 6 work on live data today.
4. **Auth back in the app**, and the Supabase user that still doesn't exist.

---

## Order I'd build it

1. **Relative pricing (3)** — works on today's data, needs no outcomes, and is the
   direct replacement for the value estimate we lost. Highest value per unit of work.
2. **Crowding (4)** — same, live data only, and it answers a question nothing else does.
3. **Selling now (1)** — becomes real within a fortnight.
4. **Feature lift (2)** — needs enough outcomes for the thresholds to pass.
5. **Momentum (5)** — needs a few weeks of daily observations to compute velocity.
6. **Seasonal (6)** and **experiment status (7)** — slowest-moving, least urgent.
