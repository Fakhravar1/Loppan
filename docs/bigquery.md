# The BigQuery move — design and migration

**Status, 2026-10-02: design agreed, nothing built.** Loppan has been paused since the
2026-08-19 stand-down (`standdown.md`). This file is the plan for restarting it on
BigQuery instead of Supabase, and the record of why each choice was made. Update it as
phases land; mark what was measured versus estimated, as the other docs do.

The short version: one row per item, its price and favourite history held in nested
arrays, updated once a day by a `MERGE`. A small derived shortlist is exported to
Supabase for the dashboard, which never queries BigQuery.

---

## 1. Why move

The stand-down was a storage ceiling: **492 MB against Supabase's 500 MB free tier**.
`analytics.md` had already projected that full coverage needs ~1.3 GB, so Supabase was
never going to hold the market. BigQuery's free tier is **10 GiB of storage and 1 TiB of
queries per month**, and beyond it storage costs about $0.02 per GB-month. That is the
right kind of database for a table that only grows.

What Supabase keeps: the shortlist and the dashboard behind it. Both are small, and
Postgres is the better engine for a grid that sorts and filters on every click.

---

## 2. Decisions

| | Decided | Why |
|---|---|---|
| **Scope** | The whole marketplace, brand-filtered by the §12 rule (expensive *or* common brands), asking price ≥ 150 kr at enrolment. No size restriction | Learning what resells needs the market, not only what fits you |
| **Grain** | **One row per item**, for its whole life | The arrays below make this cheap; one grain keeps every query honest |
| **History** | `ARRAY<STRUCT<…>>` for price and favourites, one element per *change* | An event costs ~16 bytes instead of re-storing ~300 bytes of attributes. Flat event rows would be ~8× larger |
| **Writes** | Daily batch load (free) into staging, then `MERGE` | Streaming inserts are billed; load jobs are not |
| **Cadence** | Once a day | Markdowns come every ~10 days, so daily catches every one |
| **Partitioning** | By `resolved_on`; live items sit in the `NULL` partition | The daily `MERGE` reads only live items, so its cost stays flat as history grows |
| **Billing** | Billing account linked, budget alert, custom daily query quota | The sandbox cannot do this (no DML, tables deleted after 60 days). See §8 |
| **`is_reserved`** | Dropped | Of no interest |
| **`stratum`, `sample_weight`** | Dropped | A census of the filtered universe needs no weights. Estimates then describe *that* universe, not the whole market |
| **Old data** | **Not migrated.** Start fresh | Accepted knowing the Circle origins already collected are lost (see §10) |
| **Reference price** | **Median sold price** of brand × category, seasonally adjusted | You want absolute bargains, not "the cheapest thing listed today" |
| **Seasonality, year 1** | A fixed prior from earlier data, replaced gradually by our own (§6) | A measured seasonal curve needs 12 months of sales |
| **Shortlist** | Exported daily to Supabase, replaced in full each time | Bargains are the fast-selling tail, and in-place upserts are what bloated Supabase last time |

---

## 3. ⚠️ What the design review changed

The first sketch was reviewed before anything was built. These changes came out of it.
The rest of this file already includes them.

1. **The population is ~5–9× larger than the first estimates assumed.** Early cost
   figures used ~670k live items. That was what the old pipeline *held*: 28.7% of the
   ≥100 kr wearables frame (`analytics.md`, "What a full crawl would cost"). The
   weighted estimate of **all live wearables is ~5.9M** (2026-08-10, ± tens of percent),
   and this design drops the price floor. The brand filter removes an unknown share, so
   plan on **3–6M live items** until the census in Phase 2 measures it. §8 is redone on
   that basis. BigQuery still costs only a few dollars. The real constraint moves to
   collection time (§5).
2. **Track known items by id; only search for new ones.** Re-searching the whole market
   every day cannot see past ~2,000 results per query shape, and an incomplete shape
   looks exactly like a mass sale. Instead, fetch every known live id with
   `get_objects_parallel` (100 per request). A missing id is unambiguous. Only items
   listed since the last run need search, filtered on `firstOfferedAt_SE`.
3. **Never resolve an item on a partial run.** If the run fetched less than 99.5% of
   live ids, prices still get written but nothing is marked gone. A partial run that
   marks items gone records false sales, which inflates sell-through. `track.py` calls
   that "the one error that would make the project worthless".
4. **The `MERGE` must be safe to rerun.** Every row carries `updated_run`, and the
   merge skips rows already touched by that run's date. Without this, rerunning a failed
   day appends every price change twice.
5. **Keep 7 days of staging.** `sweep_staging` partitions expire after 7 days, which
   matches BigQuery's time-travel window. A bad merge can be rebuilt from staging rather
   than lost.
6. **Pool thin groups toward their parent group; don't cut them off.** A brand ×
   category with 3 sales should lean on its category, not vanish. The same rule blends
   the seasonal prior into our own measurements (§6).
7. **Weight expected profit by sell-through.** A 2× multiple at 40% sell-through
   loses money (`handover.md` §3.6.5). The table records outcomes, so sell-through per
   brand × category comes almost free.
8. **Store current values as plain columns, next to the arrays.** The daily model jobs
   read about 40 bytes per item and never unpack arrays. This is the largest single
   saving in query bytes.
9. **Flag left-censored items.** Anything already listed when the pipeline starts has
   an unseen past. `history_complete` marks the items whose whole life we saw.
   Markdown-ladder analyses should filter on it.
10. **Authenticate with Workload Identity Federation, not a JSON key.** The repo is
    public, and a long-lived key in Actions secrets is the one credential that would hurt
    if it leaked.

---

## 4. The table

Sketch, not final DDL. Dataset `loppan`, location `EU`.

```sql
CREATE TABLE loppan.items (
  item_id          STRING NOT NULL,

  -- attributes: written at first sight, never changed
  brand            STRING,
  brand_tier       INT64,     -- the marketplace's price_point, 1–6
  category         STRING,    -- full path, e.g. Kvinna > Kläder > Byxor & Jeans
  item_type        STRING,
  demography       STRING,
  size_code        STRING,    -- WMN-EU-38 etc.; see schema.md on size_area
  condition        STRING,
  has_defect       BOOL,
  fabric           STRING,
  pattern          STRING,
  materials        ARRAY<STRING>,
  colours          ARRAY<STRING>,
  season_mask      INT64,     -- Vår 1, Sommar 2, Höst 4, Vinter 8
  weight_g         INT64,
  p2p              BOOL,      -- true = Circle listing. Different economics; never pool
  first_offered    DATE,      -- true listing date. NOT sale_started (schema.md)

  -- bookkeeping
  first_seen       DATE,
  last_seen        DATE,
  history_complete BOOL,      -- first_offered >= pipeline start: we saw its whole life
  updated_run      DATE,      -- last run that touched the row; makes MERGE idempotent

  -- current state, as plain columns so daily jobs never unnest
  price_ore        INT64,
  old_price_ore    INT64,
  favourites       INT64,
  last_chance      BOOL,

  -- history: one element per change, never per day
  price_history    ARRAY<STRUCT<on_date DATE, price_ore INT64>>,
  fav_history      ARRAY<STRUCT<on_date DATE, favourites INT64>>,

  -- outcome
  outcome          STRING,    -- NULL live · sold · expired · unknown
  resolved_on      DATE,
  final_price_ore  INT64,     -- from Parse at adjudication

  -- Circle only: what the reseller paid for it
  circle_origin    STRUCT<original_id STRING, bought_price_ore INT64,
                          opening_ore INT64, rungs INT64>
)
PARTITION BY DATE_TRUNC(resolved_on, MONTH)
CLUSTER BY brand, category;
```

**Prices stay in öre**, as before. **Monthly partitions, not daily.** About 40k
resolutions a day is ~20 MB per day, far below the size where a partition pays for
itself.

Alongside it:

| Table | Grain | Lifetime |
|---|---|---|
| `sweep_staging` | one row per item per run | partitions expire after 7 days |
| `cost_params` | one row per venue: fee %, shipping by weight band; plus the buy-side shipping fee | hand-edited |
| `seasonal_prior` | season group × sale month | loaded once from `deploy/bigquery/seasonal_prior.csv` |
| `price_level`, `seasonal_index`, `sell_through` | brand × category, or season group × month | rebuilt daily, a few thousand rows each |

---

## 5. The daily run

```
read live ids (BQ) ─► fetch by id (Algolia) ─┐
                                             ├─► load to sweep_staging ─► MERGE ─► adjudicate gone (Parse) ─► resolve MERGE
search new listings (Algolia) ───────────────┘                                              │
                                                    Circle origins for new p2p (Parse) ◄────┘
        ─► rebuild price_level / seasonal_index / sell_through ─► export shortlist ─► Supabase (+ images for those ids only)
```

1. **Read live ids** from the `NULL` partition. 5M ids is ~50 MB, which is negligible.
2. **Fetch by id**: `algolia.get_objects_parallel`, 100 per request. The 2026-08-08 pass
   did 666k items in 26.8 min. At 3–6M that is **~2–4 hours**, inside GitHub's 6-hour job
   limit but not comfortably. Shard into a matrix by `item_id` hash once it passes ~4 h.
   Set `attributesToRetrieve` to the fields we store.
3. **Search new listings**: `firstOfferedAt_SE` since the last run, minus a day of
   overlap, fanned out by category so no query shape passes ~2,000 results. Apply the
   brand filter here.
4. **Load** both into `sweep_staging` with a batch load job, which is free.
5. **Merge.** New ids are inserted. Known ids get a price and favourite element appended
   *only if the value changed*, and `last_seen` and `updated_run` are stamped.

   ```sql
   MERGE loppan.items t
   USING (SELECT * FROM loppan.sweep_staging WHERE run_date = @run) s
   ON t.item_id = s.item_id AND t.resolved_on IS NULL
   WHEN MATCHED AND t.updated_run < @run THEN UPDATE SET
     price_history = IF(s.price_ore != t.price_ore,
         ARRAY_CONCAT(t.price_history, [STRUCT(@run AS on_date, s.price_ore AS price_ore)]),
         t.price_history),
     price_ore   = s.price_ore,
     -- favourites likewise
     last_seen   = @run,
     updated_run = @run
   WHEN NOT MATCHED BY TARGET THEN INSERT (...) VALUES (...);
   ```

   ⚠️ **Dry-run this before trusting it** and check the bytes estimate is the live
   partition, not the table. Partition pruning inside `MERGE` depends on how the filter
   is written. Verify it rather than assuming.
6. **Adjudicate** every id that came back missing, or with `isForSale` false, against
   Parse (`track.adjudicate`, 60 ids per request, serial at 1 req/s). Roughly 25–50k
   resolutions a day is **~7–14 minutes**. **Skipped entirely if step 2 fetched < 99.5%**
   (§3, change 3).
7. **Resolve**: a second `MERGE` writes `outcome`, `resolved_on` and `final_price_ore`.
   The row moves out of the live partition.
8. **Circle origins** for newly seen `p2p` items, following the `preceding` pointer in
   Parse (as `backfill_item_origins.py` does). Collect them while the original is still
   reachable. `schema.md` explains why that cannot wait.
9. **Model tables and export** (§6, §7).

**Conduct.** Step 2 sends ~7× the old Algolia request volume (~50k requests a day). It
stays within `algolia.py`'s throttle, and Algolia is CDN infrastructure built for that.
Every Parse call stays strictly serial, as `api-notes.md` requires.

---

## 6. The reference price: sold medians, seasonally adjusted

`handover.md` §3.6.5 shows why the plain median sold price would mislead. A winter item
sold in August keeps ~33% of its opening ask; sold in December, ~83%. A low price in
August may be ordinary for August rather than a bargain. So the expected sold price is
split into two parts, estimated at different levels of detail:

> **expected_sold(brand, category, month m) = level(brand, category) × index(season group, m)**

- **`level`**: median sold price over the last 365 days, *with the season removed*
  (each sale divided by its month's index). It is pulled toward the category's level
  when sales are few: `(n·level_bc + k·level_c) / (n + k)`, starting at k = 20.
- **`index`**: how a season group sells in month m relative to its yearly average.
  Estimated per season group, not per brand, because brand × month would be mostly empty
  cells.

Each live item then gets **two signals**, which answer different questions:

| Signal | Meaning | Action |
|---|---|---|
| **Bargain now**: `price / expected_sold(now)` is low | Cheap even for this month: mispriced | Buy, relist soon |
| **Seasonal bet**: `expected_sold(peak month)` ≫ price | Cheap *because* of the season | Buy, hold until the peak month |

Both are ranked by **expected profit in kronor**, because you want absolute bargains:

```
expected_profit_kr = sell_through × expected_sold(target month) × (1 − venue fee)
                     − price − buy-side shipping − sell-side shipping(weight_g)
```

`sell_through` is sold ÷ resolved for the brand × category, pooled toward the category
the same way. Use the **worst venue's fee** from `cost_params` (Circle's 16% today)
until your own sales show which venue actually sells.

### The seasonal prior (option C)

`deploy/bigquery/seasonal_prior.csv` holds the year-1 index. It was derived on
2026-10-02 from `data/season_ladders.jsonl`: 1,336 sold items, with sale dates from
2023-12 to 2026-08. The measure is the median share of the opening ask kept, by month of
sale, divided by its 12-month mean.

| Month | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **warm** (Vår + Sommar, n = 600) | 0.73 | 0.91 | 0.99 | 1.09 | 1.09 | **1.40** | 1.28 | 1.09 | 0.85 | 0.81 | 0.87 | 0.91 |
| **cold** (Höst + Vinter, n = 306) | **1.45** | 1.12 | 1.00 | 0.91 | 0.85 | 0.85 | 0.91 | 0.82 | **0.72** | 0.83 | 1.28 | 1.27 |

The two groups run in opposite phases, as the handover found with a different cut.
**Every other season tag gets a flat 1.0**: all-season, single tags, and untagged items.
The other combinations are too thin to estimate.

⚠️ What this prior is not:

- **It is decay, not price level.** It measures how far items are marked down before
  they sell, which mixes demand with inventory age. Out-of-season items sit longer and
  the ladder walks them down. For a buyer the opportunity is the same, but it
  overstates *buyer* seasonality.
- **It is consignment clearing on the marketplace, not Vinted or Plick.** Every price here is
  a stand-in for what you will get until your own sales exist.
- **It does not match the handover's table**, which used single-tag items from a larger
  set (n = 459 + 858). Same direction, different numbers. Some cells here rest on 9–17
  sales.

The prior fades out as our own data comes in:
`index = (n·measured + k·prior) / (n + k)`, per cell. After a year of sales, n is in the
thousands and the prior has effectively no weight.

---

## 7. The shortlist in Supabase

- BigQuery computes candidates daily and exports everything under your **loosest**
  threshold, for example ≤ 60% of `expected_sold(now)` *or* a seasonal bet with positive
  expected profit. The dashboard's 50% / 20% / custom control then filters in Postgres
  for free.
- Columns: `item_id`, brand, category, size, condition, `price_kr`, `expected_now_kr`,
  `expected_peak_kr`, `peak_month`, `pct_of_expected`, `expected_profit_kr`,
  `sell_through`, `n_sales` (the group's evidence), `history_complete`, image paths.
- **Images are fetched for the shortlisted ids only**, as `shortlist.py` already does.
  They are not stored in BigQuery (`schema.md`, "Deliberately not collected").
- **Replaced in full each run**: truncate and insert in one transaction. Do not upsert.
  In-place churn is what built the bloat that hit the ceiling last time.
- Cap at the top ~20–30k rows by `expected_profit_kr`. That keeps Supabase in the
  low tens of MB.

---

## 8. Size and cost

Estimates, to be replaced with measurements after Phase 2. They assume 3–6M live items,
roughly 25–50k listed per day (so 9–18M items seen per year), and ~0.5 KB per item at
resolution.

| | Year 1 | Year 3 |
|---|---|---|
| Storage | ~5–12 GB | ~20–35 GB, mostly long-term rate |
| Daily `MERGE` + staging | ~2–3.5 GB a run → ~60–100 GB/month | same; it reads only the live partition |
| Model jobs + export | ~15 GB/month | ~15 GB/month |
| Your own queries (guess: 100 a month) | ~100–500 GB/month | grows with the table |

**On the free tier:** queries use ~20–60% of the 1 TiB allowance. Storage reaches the
free 10 GiB around the end of year 1, after which it costs cents.

**With no free tier at all:** about **$1–4 a month (≈ 10–45 kr)** in year 1, and
$2–5 by year 3. Most of it is your own querying. The pipeline alone is under $1. The
habits that keep it there:

- Select only the columns you need. `SELECT *` reads every array.
- **Never point a dashboard at BigQuery.** A few hundred page loads a day costs more
  than everything above combined.
- Set the **custom query quota** (~30 GiB/day). A budget alert only sends an email; the
  quota actually stops spending.

---

## 9. Migration plan

**Phase 0 — Google Cloud (yours to do; it involves payment details)**
- [x] Project **`pre-loved-507312`** ("Pre-Loved"), linked to billing account
      "My Billing Account 1" on 2026-10-02. Out of sandbox mode
- [x] Budget alert **10 SEK/month** on that project, emails at 50 / 90 / 100%
      (budget "Loppan (pre-loved) 10 kr"). Alerts only
- [x] Custom quota `QueryUsagePerDay` = **30,720 MiB (30 GiB)**, granted. The default
      was 209,715,200 MiB (200 TiB). Quota preference `loppan-query-cap`
- [x] Dataset **`pre-loved-507312:loppan`**, location `EU`
- [x] Service account **`loppan-pipeline@pre-loved-507312.iam.gserviceaccount.com`**:
      `roles/bigquery.jobUser` on the project, `roles/bigquery.dataEditor` on the
      `loppan` dataset only. No keys exist
- [x] Workload Identity pool `github`, OIDC provider `loppan-repo`, condition
      `assertion.repository=='Fakhravar1/Loppan'`. Only that repo can impersonate the
      account. Provider resource:
      `projects/839195135409/locations/global/workloadIdentityPools/github/providers/loppan-repo`
- [x] GitHub repo variables (not secrets; none of these is secret): `GCP_PROJECT_ID`,
      `GCP_WIF_PROVIDER`, `GCP_SERVICE_ACCOUNT`, set 2026-10-02
- [x] Smoke test passes: `.github/workflows/bq-smoke.yml` reads as the service account
      (`session_user()` = `loppan-pipeline@…`), then creates and drops `loppan._smoke`
      in `EU` to prove the dataset grant. Green on run 37002433291, 2026-10-02. The first
      run failed only because `AT` is a reserved word. Auth was fine from the start

All of the above was done 2026-10-02 from Cloud Shell. The script is
`~/loppan_setup.sh` in that Cloud Shell home directory.

**Phase 1 — Tables**
- [x] `deploy/bigquery/schema.sql`, applied by `bq-schema.yml` on every change.
      Idempotent: `IF NOT EXISTS` plus a keyed `MERGE`. Tables: `items`,
      `sweep_staging`, `adjudication_staging`, `circle_origin_staging` (the three
      staging tables expire after 7 days), `runs` (the completeness gate),
      `brand_rules` (seeded with the §12 values), `brand_exclusions`, `cost_params`,
      `seasonal_prior`
- [x] `seasonal_prior` loaded from the CSV (`--replace`; the CSV is the source of truth).
      Verified 2026-10-02, run 37003550299: all 9 tables exist, `items` is partitioned
      monthly on `resolved_on` and clustered on brand, category, the three staging
      tables expire after 7 days, `brand_rules` is seeded, and the prior holds
      12 months × warm/cold
- [ ] Fill `cost_params` (open question 3) and `brand_exclusions` (the unbranded
      placeholder's exact label, found during Phase 2)

**Phase 2 — Census seed (one-off)**
- [ ] Fan-out search over brand × category × price band to enrol every live,
      brand-filtered item. Assert every query shape is exhaustive
- [ ] **Measure:** live count, bytes per row, listings per day over the first week.
      Redo §8 with the measured values

**Phase 3 — Daily run**
- [ ] Steps 1–8 of §5 as one workflow, with the 99.5% completeness gate
- [x] `merge_sweep.sql` and `merge_resolve.sql` (Circle origins, then gated outcomes),
      tested by `test.sh` in `bq-schema.yml` on synthetic rows: a rerun is a no-op,
      updates append only on change, duplicate source rows collapse, a stray tracked
      id never enrols, a 99% run resolves nothing, a resolved row moves partition.
      Green on run 37011068601, 2026-10-02
- [ ] Dry-run bytes on real volume. On synthetic rows the merge read 375 of 468
      table bytes, which proves nothing at that size
- [ ] Fetcher on branch `bigquery-fetch` (in progress)

**Phase 4 — Model and shortlist**
- [x] `model.sql` builds `seasonal_index`, `price_level`, `sell_through` and
      `shortlist_candidates`, reading `model_params`. Tested on known answers: with no
      measured sales it reproduces the prior, A pools to 18,000, sell-through 0.7,
      margin 7,600, an overpriced item is excluded, a cold item peaks in January.
      `expected_profit_ore` stays NULL until `cost_params` is filled
- [x] `bq-health.yml`, daily: missed run, closed completeness gate, > 10 GiB billed in
      24 h. The cost check needs `roles/bigquery.resourceViewer` on the service account
- [ ] Export to a **new** Supabase table; image fetch for shortlisted ids

**Phase 5 — Dashboard** reads the new table

**Phase 6 — Retire the old pipeline** (see §10). Dropping Supabase tables is
irreversible, so confirm each one before running the drop.

---

## 10. What carries over, what retires

**Reused**: `algolia.py` (client, `get_objects_parallel`, fan-out search),
`market.py` (Parse), `track.adjudicate`, the origin logic in
`backfill_item_origins.py`, `search.image_paths`, and the image step in `shortlist.py`. Most of the hard-won
collection code survives. Only the storage underneath it changes.

**Constraints carried over from the 2026-08-14 assessment.** Branch
`claude/loppan-bigquery-migration-av4mne`, `docs/architecture.md` §9, weighed a BigQuery
move before this design existed. Three of its points still bind:

- **Stdlib only, no `pip install`.** Talk to BigQuery through the `bq` CLI the runner
  already has (after `google-github-actions/auth` + `setup-gcloud`), or through REST
  with `urllib` and the short-lived access token the auth action issues. Do not add
  `google-cloud-bigquery`.
- **Never page with repeated queries.** That assessment found `query_pages` would be
  669 sequential queries, each rescanning the table: ~45 GB a pass, over 1 TiB within a
  month from one job. Run one query and page its *results* (`jobs.getQueryResults`), or
  export once and read the file. Step 1 of §5 is exactly one query.
- **The dashboard stays on Supabase.** BigQuery's ~0.5–2 s floor per query and the lack
  of a safe browser-facing path rule it out, which §7 already assumes.

It also noted that Supabase Pro (8 GB, ~$25/month) would have needed no code changes.
This design takes the rewrite in exchange for ~$0–4/month and room to grow past 8 GB.

**Rebuilt, not reused**: `size_group` / `size_system` / `size_value` / `size_area` were
Postgres generated columns (`schema.md`). They become expressions in a BigQuery view, or
are computed in the shortlist export. `sizes.py` is the size *targeting* for the
candidate sweep. It does not apply here, because scope is no longer size-restricted.

**Retires**: the Supabase `items`, `peer_prices`, `peer_live`, `shortlist_daily`,
`brand_band_population`, `cohort_items`, `circle_roundtrips` and `circle_origins`
tables; the strata and `sample_weight` machinery in `enrol.py`; `cohort.py`,
`pool_refresh.py`; and the four workflows paused on 2026-08-19.

⚠️ **Starting fresh discards `circle_origins`**, about 14.8k Circle purchase prices.
`schema.md` warns that these may not be collectable again once the original listings go.
That was accepted. `data/item_origins.jsonl` is a local copy of the raw pull if it is
ever wanted.

---

## 11. Open questions

1. ~~What counts as "no-name"?~~ **Decided 2026-10-02**, see §12.
2. **Condition in the reference price?** It moves price a lot. It could be a
   multiplicative factor per category, estimated the same pooled way.
3. **Fees and shipping in `cost_params`**: Vinted, Plick, Circle (16%), and the marketplace's
   buy-side shipping. Which costs are fixed and which depend on weight?
4. **Relisted ids.** If an expired item comes back under the same id, the
   `NOT MATCHED` branch would insert a second row. How often that happens is unknown.
   Guard against it once measured.
5. **Your own sales.** Venue, price and days to sell for items you actually buy belong
   in a separate small table keyed on `item_id`, not in `items`. That table eventually
   replaces the marketplace's prices as the stand-in for Vinted and Plick.

---

## 12. The brand rule

**Decided 2026-10-02.** A brand is in scope if **either** holds:

| Gate | Keeps | Starting value |
|---|---|---|
| **Expensive**: brand median sold price ≥ `min_median_sold_kr` | Dear brands, however rare | 200 kr |
| **Common**: brand is in the top `top_n_brands` by live listings | Cheap brands that sell in volume | 150 |

**And a price floor, added 2026-10-02:** an item enrols only if its asking price is
**≥ `min_price_kr` (150 kr)**. Like the brand gates, it applies **at enrolment only**.
A tracked item that a markdown takes below 150 kr is followed to its outcome. Dropping
it would delete exactly the marked-down items that then sell, which biases
sell-through, as with brands below. The cost of the rule is known and accepted: a dear
brand's item that is *already* under 150 kr when first seen is never enrolled.

What falls out is the **cheap *and* rare** long tail, which is what "no-name" meant in
practice. Both values are parameters in a `brand_rules` table, not constants in code.
150 is a starting point. Set it from measured coverage in Phase 2: how much of the
market the top N covers, and where adding more brands stops changing the shortlist.
200 kr matches the floor the old pool used (`analytics.md`).

Three details that make it work:

- **The unbranded placeholder is excluded explicitly.** Whatever the marketplace calls
  an item with no brand is likely to rank *high* on frequency, so the "common" gate
  would let it in. Exclude it by name before ranking.
- **Day one has no sold prices.** We start fresh, so on day one the "expensive" gate
  uses the brand's **median live ask** from Algolia, with `brand_tier` as a tiebreak. Live
  asks run higher than sold prices (unsold items linger at high asks), so the stand-in
  lets slightly more brands in, which is the safe direction. From the first monthly
  re-evaluation with ≥ 20 sales per brand, the measured sold median replaces it.
- **Re-evaluated monthly, with a margin.** A brand enters when it clears a gate and
  leaves only when it falls 10% below it, so brands near the line don't flip in and out.
  **A brand that leaves stops enrolling new items, but its tracked items are followed to
  their outcome.** Dropping them mid-life would delete exactly the unsold items and bias
  sell-through upward.

Availability comes from Algolia brand facet counts (`algolia.brand_facets`). The facet
list is truncated at ~1,000 values (`analytics.md`), which is harmless here because only
the top few hundred matter for the "common" gate.
