# The BigQuery move — design and migration

**Status, 2026-10-06: live since 2026-10-02.** The daily pipeline runs from `main`
(§5), the shortlist feeds the dashboard (§7), and the v1 Supabase system it replaced is
removed from the repository (tag `v1-final`). This file is the design, and the record of
why each choice was made. Update it as
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
| **Scope** | The whole marketplace: kosher brands only (§12, ≥ 20 live listings ≥ 150 kr), and nothing under 150 kr ever stored. No size restriction | Learning what resells needs the market, not only what fits you |
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
   **Measured exactly, 2026-10-02 (sums of 473 and 1,298 disjoint shapes, every one
   exhaustive):** 10,273,249 live wearables at all prices; **2,494,188 at ≥ 150 kr**
   (68.8% Kvinna > Kläder); **~2.30M under the full §12 rule**. That is *below* the
   3–6M range. The 5.9M figure came from estimated counts, which miss in both
   directions: the ≥ 150 kr count was estimated at 6.84M one time and 4.74M another,
   against an exact 2.49M. Fetching by id is cheap too (§5 step 2), so collection time
   is not the constraint after all.
2. **Track known items by id; only search for new ones.** Re-searching the whole market
   every day cannot see past ~2,400 results per query shape, and an incomplete shape
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
| `progress_daily` | one row per run date: is the pipeline progressing? (§5, step 10) | kept; a rerun overwrites its day |

---

## 5. The daily run

```
read live ids (BQ) ─► fetch by id (Algolia) ─┐
                                             ├─► load to sweep_staging ─► MERGE ─► adjudicate gone (Parse) ─► resolve MERGE
search new listings (Algolia) ───────────────┘                                              │
                                                    Circle origins for new p2p (Parse) ◄────┘
        ─► rebuild price_level / seasonal_index / sell_through ─► export shortlist ─► Supabase (+ images for those ids only)
```

The Parse calls and the image-path stripping read the marketplace's endpoints from the
four `LOPPAN_MARKET_*` repository variables (`loppan/endpoints.py`), which `bq-daily.yml`
and `bq-fetch-sample.yml` pass in as environment. A local run needs them exported too.

1. **Read live ids** from the `NULL` partition. 5M ids is ~50 MB, which is negligible.
2. **Fetch by id**: `bq_fetch.py track`, over `algolia.get_objects_parallel` (100 per
   request, `attributesToRetrieve` limited to stored fields). **Measured 2026-10-02:
   0.53–0.60 s per 1,000 ids**, limited by the throttle at ~20 requests/s, so a full pass
   is **~25–30 min at 3M and ~50–60 min at 6M**. The 2026-08-08 pass took 26.8 min for
   666k only because it also wrote to Supabase. No sharding is needed. A chunk that
   errors counts as *not fetched*, never as missing (tested).
3. **Search new listings**: `bq_fetch.py new`, `firstOfferedAt_SE` since the last run
   minus a day of overlap. `bq_shapes.py` splits on `createdAt` (price as a fallback)
   into leaves of at most 1,000 hits and checks every leaf is exhaustive. Apply the price
   floor and the brand rule here. A few live items (7 in 30,563) carry no
   `firstOfferedAt_SE`, so only the census can find them.
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
   Parse (`bq_fetch.py adjudicate`, 60 ids per request, serial at 1 req/s). Measured
   inflow at all prices is 51k–181k listings a day, which would be up to ~50 minutes;
   the floor and brand rule cut it to the in-scope share. An id in a batch that errored
   gets no row at all, so a failed request never becomes an `unknown`. **Skipped entirely if step 2 fetched < 99.5%**
   (§3, change 3).
7. **Resolve**: a second `MERGE` writes `outcome`, `resolved_on` and `final_price_ore`.
   The row moves out of the live partition.
8. **Circle origins** for newly seen `p2p` items, following the `preceding` pointer in
   Parse (as `backfill_item_origins.py` does). Collect them while the original is still
   reachable. `schema.md` explains why that cannot wait.
9. **Model tables and export** (§6, §7).
10. **Progress**: `progress.sql` writes the day's row in `progress_daily`, and the log
    gets a PROGRESS block. See *Progress* below.

**Schedule and the completed-run guard.** GitHub drops or badly delays scheduled runs
under load: the daily run never fired on its own on 2026-10-03 or 2026-10-04, and both
days were dispatched by hand. So `bq-daily.yml` fires daily mode at three slots, **02:23,
04:47 and 07:13 UTC**, all off the hour and all on the same Stockholm run date.
`daily.sh` stamps `runs.completed_at` as its very last step, after the shortlist export,
so the stamp means every step finished. Before any work, brand counts included, daily
mode checks for a stamped row with today's `run_date`; if there is one it prints
"today's run already completed at …; nothing to do" and exits 0 in seconds. A slot after
a failed run finds no stamp and runs the day again, which is safe because every merge is
idempotent, just wasteful. The `bq-daily` concurrency group queues a slot behind a run
in progress instead of overlapping it; GitHub keeps one waiting run, so a newer slot
replaces a waiting one, which shows as cancelled. To redo a completed day, dispatch
daily with **force** ticked (`FORCE=1` skips the guard).

**Conduct.** Step 2 sends ~7× the old Algolia request volume (~50k requests a day). It
stays within `algolia.py`'s throttle, and Algolia is CDN infrastructure built for that.
Every Parse call stays strictly serial, as `api-notes.md` requires.

### Progress: is it moving? (step 10)

After the export, `progress.sql` merges one row into **`loppan.progress_daily`**,
`MERGE ... ON run_date`, so a rerun overwrites its day. The row is a snapshot taken at
`computed_at`: rerunning an old date records today's live counts under that date.

| Columns | What | Read from |
|---|---|---|
| `live_items`, `items_ever`, `enrolled_today` | size; enrolled = `first_seen` is the run date | live partition, plus today's resolutions |
| `sold_today`, `expired_today`, `below_floor_today`, `unknown_today`; `sold_total` | outcomes resolved today; every sale ever | `resolved_on` = run date; `outcome` |
| `price_drops_today` | live items whose last `price_history` step is dated today and lower than the one before | live partition |
| `fav_changes_today` | live items whose last `fav_history` step is dated today. First sight is not a change | live partition |
| `new_found`, `completeness` | the latest `runs` row for the date | `runs` |
| `combos_ge1`, `combos_ge20`, `categories_priced` | brand × category groups with ≥ 1 and ≥ 20 sales, and categories priced | `price_level` |
| `shortlist_now`, `shortlist_season` | candidates by signal (§7) | `shortlist_candidates` |
| `accuracy_*` | the accuracy check, below | sales of the last 7 days |
| `circle_with_origin`, `circle_without_origin` | live Circle items with and without a purchase price: the origins backlog | live partition |
| `storage_gib`, `billed_gib_today` | logical GiB in `loppan`; GiB billed in the project on the run's Stockholm day, up to `computed_at` | `TABLE_STORAGE`, `JOBS_BY_PROJECT` |

**The accuracy check.** Each item sold in the 7 days to the run date is priced the way
`model.sql` prices a live item: the brand × category level (else the category's) times
the season group's index at the sale month. `accuracy_median_ratio` is the median of
final price ÷ that expected price, over `accuracy_n` sales. 1.0 means the model's
expected price is the typical sale. Below 1 it expects too much, so margins on the
shortlist are overstated; above 1 it expects too little. The `thin` pair covers groups
with < 20 sales, which lean on their category; the `thick` pair covers groups with ≥ 20.

⚠️ **It is in-sample.** Today's model already contains these sales, because the window
is 365 days. While all sales are recent, groups with ≥ 20 sales read close to 1 by
construction. As the window fills, it becomes a real check: a week's sales against a
year's level and the seasonal index. The `thin` ratio shows what pooling toward the
category costs.

**Cost.** ~0.1–0.3 GB a day at 2.3M live items. The two history arrays in the live
partition dominate, at 16 bytes an element. `sold_total` reads `outcome` over the
resolved partitions. Today's counts and the accuracy check read one or two month
partitions. `test.sh` prints the dry-run bytes of the live-partition read on the real
table: **106 MB on 2026-10-04** (2.3M live). `storage_gib` falls back to
`loppan.__TABLES__` (also logical bytes) if `TABLE_STORAGE` is unreadable. On
2026-10-04 the service account could not read it, so the fallback is what runs.
`billed_gib_today` leaves out SCRIPT parent jobs, which repeat their children's bytes,
and stays NULL without `roles/bigquery.resourceViewer`.

**The PROGRESS block.** Next, `daily.sh` prints the day's numbers as plain lines between
fixed markers, outside any `::group::`, so the log shows the block open:

```
===== PROGRESS <run_date> =====
live items:        ...  (enrolled today ..., items ever ...)
sold today:        ...  (total ...; expired ..., below floor ..., unknown ...)
price drops today: ...  (like changes ...)
new found:         ...  (completeness ...)
model:             ... brand x category with >= 20 sales  (>= 1: ...; categories priced ...)
shortlist:         ... now, ... season
accuracy 7d:       price / expected ..., n ...  (< 20 sales: ... n ...; >= 20: ... n ...)
circle origins:    ... with, ... without
storage:           ... GiB
billed today:      ... GiB
trend 7d:          MM-DD..MM-DD  live ...  |  sold/day ...
===== END PROGRESS =====
```

A dash is a NULL. The trend lists only the days that have a row. `progress_block.py`
formats it, and `test.sh` prints one from its fixtures. If `progress.sql` or the block
fails, the run logs a `::warning::` and carries on: the data is already in.

To cut the block out of a run log, for example in the morning check:

```
gh run view <id> --repo Fakhravar1/Loppan --log \
  | sed -n '/===== PROGRESS /,/===== END PROGRESS =====/p' | cut -f3- | cut -d' ' -f2-
```

`gh` prefixes every line with the job, the step and a timestamp. The two `cut`s remove them.

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

**As built (2026-10-03).** Supabase migration `shortlist_v2`:
- **`public.shortlist`** is what the dashboard reads. Columns mirror
  `shortlist_candidates`, plus `image_paths` and the generated
  `size_group`/`size_system`/`size_value`. Prices are in öre, as in BigQuery.
- **`public.shortlist_staging`** is the landing table.
- **`promote_shortlist()`** truncates and inserts in one transaction, and **refuses an
  empty swap**, so a failed export never blanks the dashboard.
- **Access:** RLS is on. Signed-in users (`authenticated`) may read `shortlist`. Only
  the service role writes, and nobody else can touch staging or call either function.

`loppan/bq_export.py` runs as the last step of `daily.sh daily`, or alone with
`daily.sh export`. It reads every candidate with one `bq` query and attaches image paths
for those ids only. An id the search index no longer returns has sold since the
morning's run and is dropped. Then it stages in batches of 500 and calls the swap.
It needs the `LOPPAN_SUPABASE_KEY` repository secret, which `bq-daily.yml` passes in.
`daily.sh export` rebuilds the model (`model.sql`) first, so a dispatch of
`mode=export` re-exports a fresh shortlist, not yesterday's.
`size_area` (`schema.md`) is not rebuilt yet.

**Signal and first picture (2026-10-03).** Supabase migration `shortlist_signal`
(additive only):
- **`signal`** (`model.sql`, carried by `bq_export.py`) says why a row is on the list:
  - **`'now'`**: price ≤ `export_max_pct_of_expected` (60%) × `expected_now`. It is
    cheap right now for its brand × category: the bargain-now signal (§6).
  - **`'season'`**: only the seasonal bet qualifies it. It is too dear for today, but
    `sell_through × expected_peak` is above the price.
- **The cap ranks `'now'` first.** The `export_top_n` cap keeps every `'now'` row
  first, by gross margin, then `'season'` rows by gross margin. A high-margin seasonal
  bet never crowds out a bargain. `test.sh` proves it with `top_n = 2`.
- **`first_image_path`** is generated from `image_paths[1]`, stored in `shortlist`,
  so a card reads a single column. Prepend the image host in the app; the repo stores
  paths only.
- **An index on `(signal, pct_of_expected)`** serves the dashboard's default view:
  `signal = 'now'`, cheapest relative to expected first.
- `promote_shortlist()` copies `signal` and keeps its empty-swap refusal,
  `search_path = ''` and service-role-only execute.
- **Refreshed daily only.** An item that sells during the day stays on the list until
  the next morning's export. That was decided, rather than an hourly liveness check.

---

## 8. Size and cost

Estimates, to be replaced with measurements after Phase 2. They assume 3–6M live items,
roughly 25–50k listed per day (so 9–18M items seen per year), and ~0.5 KB per item at
resolution. **Measured 2026-10-02:** 2.30M live in scope, and **~42k listed a day
under the full rule** (46.2k at ≥ 150 kr, exact per day, weekdays 42–69k, Sundays
~18k; lower bounds, since items sold since listing are missing). That is ~15M items a
year, the top of the assumed range. Year-1 storage is therefore **~8–9 GB, at the free
10 GiB line**, and the daily `MERGE` reads ~1.2 GB of live partition (~35 GB a month).
Staging rows are 470–630 bytes.

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
- [x] Fetcher built and sample-tested on branch `bigquery-fetch` (2026-10-02):
      `bq_fetch.py` census / track / new / adjudicate / origins / brands / validate,
      stdlib only, no Supabase import (tested). Samples loaded into BigQuery from
      Actions three times, all green
- [x] Kosher list in SQL: `kosher_brands`, `brand_counts_staging`, `kosher.sql`, and
      `merge_sweep.sql` enrolling only kosher brands. Tested
- [x] `bq_fetch.py brands` counting at ≥ 150 kr and writing `brand_counts_staging` NDJSON.
      A shape is a leaf only when its counts are exhaustive *and* its brand facet list
      is under 1,000 values, so no brand is cut off; exit 2 if any shape falls short
- [x] Fetcher dates in Stockholm time (`first_offered`, `run_date`, `new --since`),
      plus `item_status`, `bought_on` and the `below_floor` flag. Branch
      `bigquery-fetch` at a5d042c, 21 tests, Actions sample load green
- [ ] `bought_on` is still Parse's UTC date (`outcomes.origin_of`), so a purchase in the
      last hour or two before UTC midnight lands on the wrong Stockholm day
- [x] Brand rule decided: the kosher list (§12), replacing the median gate
- [x] **Census backfill, 2026-10-02** (`bq-daily.yml` census mode, run 37034445772,
      12.5 min, 2.99 GiB billed). Brand counts: 73,395 brands counted exactly, **6,779
      kosher**. Census: 2,504,815 live items read at ≥ 150 kr, every shape exhaustive.
      238,007 were skipped as not kosher and 58,706 as unbranded. **2,266,808 enrolled**,
      against 2,266,566 expected from the kosher brand counts (~100.0%). No price under
      15,000 öre, no unbranded item, and no non-kosher brand in `items`
- [x] Circle-origin backlog: 11,146 Circle items enrolled without a purchase price,
      worked through in `origins` mode at ~2 s an item (6,000 a run), plus 1,500 a day
      in daily mode
- [ ] **Measure:** bytes per row and listings per day over the first week. Redo §8
      with the measured values

**Phase 3 — Daily run**
- [x] `daily.sh` + `bq-daily.yml`: steps 1–8 of §5 with the 99.5% completeness gate.
      **First daily run 2026-10-02** (run 37036030968, 76 min, 3.78 GiB billed): live
      ids read in 92 s; track 2,266,808 ids at **completeness 1.0**, 0.50 s per 1,000,
      19 min; new listings since 30 Sep 167,483 found in 42 s; 263 adjudicated in 10 s;
      1,500 Circle origins in 50 min; resolve + model 45 s. It ran on the census's own
      date, so the `updated_run` guard skipped same-day updates, as designed. The 527
      items already under 150 kr close on the next day's run
- [ ] Cron live from `main` (02:23 UTC daily), watched by `bq-health` and a morning
      Claude check
- [ ] Three daily slots (02:23, 04:47, 07:13 UTC) behind the `completed_at` guard (§5),
      because GitHub dropped the scheduled run on 2026-10-03 and 2026-10-04. The first
      slot to finish stamps `runs.completed_at`; later slots exit 0. `bq-health` runs at
      06:11 and 08:41 UTC so one dropped slot can't silence the alarm. Done once a day
      shows one full run and the later slots exiting on the guard
- [ ] `shortlist_candidates` hits the 30,000 cap on day one only because a few hundred
      sales price everything. Treat it as meaningless until weeks of sales exist
- [x] `merge_sweep.sql` and `merge_resolve.sql` (Circle origins, then gated outcomes),
      tested by `test.sh` in `bq-schema.yml` on synthetic rows: a rerun is a no-op,
      updates append only on change, duplicate source rows collapse, a stray tracked
      id never enrols, a 99% run resolves nothing, a resolved row moves partition.
      Green on run 37011068601, 2026-10-02
- [x] `progress.sql` + `progress_daily` + the PROGRESS block (§5, step 10), tested by
      `test.sh` on fixtures with known answers: counts by outcome, price drops, like
      changes, the latest runs row, model maturity, the accuracy ratio, an idempotent
      MERGE. Branch `daily-progress`
- [ ] Dry-run bytes on real volume. On synthetic rows the merge read 375 of 468
      table bytes, which proves nothing at that size
- [x] Fetcher on branch `bigquery-fetch` (PR Fakhravar1/Loppan#4)

**Phase 4 — Model and shortlist**
- [x] `model.sql` builds `seasonal_index`, `price_level`, `sell_through` and
      `shortlist_candidates`, reading `model_params`. Tested on known answers: with no
      measured sales it reproduces the prior, A pools to 18,000, sell-through 0.7,
      margin 7,600, an overpriced item is excluded, a cold item peaks in January.
      `expected_profit_ore` stays NULL until `cost_params` is filled
- [x] `bq-health.yml`, daily: missed run, closed completeness gate, > 10 GiB billed in
      24 h. The cost check needs `roles/bigquery.resourceViewer` on the service account
- [x] Export to a **new** Supabase table (`shortlist`, §7), with images fetched for the
      shortlisted ids only. Runs at the end of every daily run

**Phase 5 — Dashboard** reads the new table

**Phase 6 — Retire the old pipeline** (see §10)
- [x] Supabase project restored from pause (2026-10-02). All 26 old `public` tables and 8
      views dropped after the archive above, as one migration with no `CASCADE`
      (`drop_v1_pipeline_after_bigquery_move`). Database 385 MB → 13 MB. Your own
      app tables (`app_users`, `target_sizes`, `excluded_categories`,
      `shortlist_flagged`) went too, by choice: clean slate
- [x] The 33 orphaned v1 `public` functions dropped by exact signature, no `CASCADE`
      (`drop_v1_orphan_functions`, 2026-10-02). Supabase's `public` schema is now empty
- [x] v1 removed 2026-10-06: 24 modules, the four paused workflows, the Pi files and their
      docs. Preserved at tag `v1-final`

---

## 10. What carries over, what retires

The v1 files named below were removed on 2026-10-06 and are preserved at tag `v1-final`.

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

**The two irreplaceable tables were archived, not discarded.** On 2026-10-02,
`bq-archive.yml` (since removed: its one job was done) copied Supabase's `circle_origins` (16,172 Circle purchase prices) and
`season_clearings` (the 1,497 histories behind the seasonal prior) into
`loppan.archive_circle_origins` and `loppan.archive_season_clearings`. BigQuery's
counts match Supabase's exactly. They sit outside the pipeline. JSON columns are stored
as JSON strings.

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

## 12. The brand rule: the kosher list

**Decided 2026-10-02** (replacing the earlier expensive-or-common rule the same day).
A brand becomes **kosher** the first time it has **≥ `min_listings` (20) live listings at
or above the 150 kr floor**, and **stays kosher for good**. The list in `kosher_brands`
only ever grows, so a brand is never dropped because it dips under 20 for a while.
Only kosher brands enrol new items. The point is to keep out one-off
items and tiny labels, not cheap brands. Every former top-150 brand has ≥ 2,500
listings and passes automatically.

```
weekly:  count live listings ≥150 kr per brand ──► kosher_brands
                                                    joins at ≥ 20 · never leaves
daily:   fetch ≥150 kr ──► sweep_staging ──► merge_sweep enrols an item only if
                                             its brand is kosher (unbranded never is)
```

- **Counted at ≥ 150 kr**, the same set of items we store, so the list and the data
  agree. `bq_fetch.py brands` sums brand counts over shapes small enough to be exact.
  A single facet call over the whole scope inflates counts by up to 2.3×.
- **Add-only, so nothing flaps.** The list never shrinks. A dip below 20, or a brand
  missing from one week's count, changes nothing. `listings` and `counted_on` update
  each week for reference only.
- **Unbranded items are never kosher.** Measured 2026-10-02, an unbranded item has no
  brand key at all (`brand IS NULL`). It matches no row, so it never enrols.
- **Enforced in SQL, not in the fetcher.** The fetcher stages everything ≥ 150 kr, and
  `merge_sweep.sql` joins `kosher_brands`. Since nothing is ever removed, a partial or
  failed count can only delay an addition. On day one, the first `brands` count and
  `kosher.sql` run before the census. An empty list enrols nothing, which fails safe.
- `brand_exclusions` stays for naming any brand to keep out regardless.

**The hard price floor (2026-10-02): no asking price under 150 kr is ever stored.**
`min_price_kr` = 150, separate from the kosher list. An item under it never enrols.
When a markdown takes a tracked item under it, the fetcher sends no price
(`below_floor = TRUE` in staging) and `merge_sweep.sql` closes the row with outcome
**`below_floor`**, keeping its last price at or above the floor. A price comparison in
the merge is the backstop. **Final prices are held to it too:** Parse's last ask can be
under 150 kr if a markdown crossed the floor between runs, and `merge_resolve.sql` then
closes the item as `below_floor` with no final price. Sell-through counts `below_floor`
as **not sold**: the item went past 150 kr without selling, which is what a reseller
needs counted, and leaving it out would bias sell-through upward. `test.sh` asserts that
no current, historical or final price under 15,000 öre exists. The floor is about
*asking* prices: a Circle reseller's purchase price (`circle_origin.bought_price_ore`)
is what they paid the marketplace, and is stored whatever it is.

**Size, from the tester's exact counts (≥ 150 kr):** 42,365 brands counted. 4,962 have
≥ 20 listings, 10,836 have ≥ 5, and roughly 37k more appear only as one-offs. Those
~5,000 brands should hold most of the 2.49M items. The census gives the exact figure.

Tested in `test.sh`:
- The merge enrols kosher brands only, skipping a non-kosher brand and an unbranded item.
- The refresh adds brands at 20 and 25 but not at 19.
- It keeps a kosher brand that dipped to 5, and one missing from the count.
- A rerun adds nothing.
