-- The BigQuery tables. docs/bigquery.md §4 is the reasoning; this is the contract.
--
-- Idempotent: every statement is CREATE ... IF NOT EXISTS or a keyed MERGE, so
-- bq-schema.yml applies the whole file on every change. A column change to an
-- existing table needs an explicit ALTER below the CREATE, never an edit to it alone.
--
-- ⚠️ All prices are in öre. Dates are run dates, not timestamps.

-- ─── items: one row per item, for its whole life ─────────────────────────────

CREATE TABLE IF NOT EXISTS loppan.items (
  item_id          STRING NOT NULL,

  -- attributes: written at first sight, never changed
  brand            STRING,
  brand_tier       INT64    OPTIONS (description = "The marketplace's own price_point tier, 1-6"),
  category         STRING   OPTIONS (description = "Full category path"),
  item_type        STRING,
  demography       STRING,
  size_code        STRING   OPTIONS (description = "GROUP-SYSTEM-VALUE, e.g. WMN-EU-38. The value alone means nothing"),
  condition        STRING,
  has_defect       BOOL,
  fabric           STRING,
  pattern          STRING,
  materials        ARRAY<STRING>,
  colours          ARRAY<STRING>,
  season_mask      INT64    OPTIONS (description = "Bitmask: Vår 1, Sommar 2, Höst 4, Vinter 8"),
  weight_g         INT64,
  p2p              BOOL     OPTIONS (description = "true = Circle listing. Different economics; never pool with consignment"),
  first_offered    DATE     OPTIONS (description = "True listing date. NOT sale_started, which is the current price step"),

  -- bookkeeping
  first_seen       DATE,
  last_seen        DATE     OPTIONS (description = "Last run that fetched it present"),
  history_complete BOOL     OPTIONS (description = "Listed on or after the pipeline started: its whole price path was observed"),
  updated_run      DATE     OPTIONS (description = "Last run that touched the row. The MERGE skips rows already at this run, which makes a rerun a no-op"),

  -- current state, as plain columns so daily jobs never unnest the arrays
  price_ore        INT64    OPTIONS (description = "Current price, öre"),
  old_price_ore    INT64    OPTIONS (description = "The marketplace's own previous price, one markdown step back, öre"),
  favourites       INT64,
  last_chance      BOOL,

  -- history: one element per change, never per day
  price_history    ARRAY<STRUCT<on_date DATE, price_ore INT64>>,
  fav_history      ARRAY<STRUCT<on_date DATE, favourites INT64>>,

  -- outcome
  outcome          STRING   OPTIONS (description = "NULL while listed; sold | expired | unknown. Only ever set from Parse adjudication"),
  resolved_on      DATE,
  final_price_ore  INT64    OPTIONS (description = "From Parse at adjudication, öre. An expired item's final price is an ask nobody paid"),

  -- Circle only: what the reseller paid the marketplace for it
  circle_origin    STRUCT<original_id STRING, bought_price_ore INT64,
                          opening_ore INT64, rungs INT64>
)
PARTITION BY DATE_TRUNC(resolved_on, MONTH)
CLUSTER BY brand, category
OPTIONS (description = "One row per item. Live items sit in the NULL partition: filter resolved_on IS NULL and only that partition is read. docs/bigquery.md");

-- ─── staging: what each run fetched, kept 7 days to match time travel ────────

CREATE TABLE IF NOT EXISTS loppan.sweep_staging (
  run_date         DATE NOT NULL,
  item_id          STRING NOT NULL,
  source           STRING NOT NULL OPTIONS (description = "track = fetched by id | new = found by listing-date search | census = the one-off seed"),
  present          BOOL NOT NULL   OPTIONS (description = "false = a tracked id came back empty. Candidate for adjudication, never a sale by itself"),
  is_for_sale      BOOL,
  fetched_at       TIMESTAMP,

  price_ore        INT64,
  old_price_ore    INT64,
  favourites       INT64,
  last_chance      BOOL,

  -- attributes: required for new and census rows, may be null on track rows
  brand            STRING,
  brand_tier       INT64,
  category         STRING,
  item_type        STRING,
  demography       STRING,
  size_code        STRING,
  condition        STRING,
  has_defect       BOOL,
  fabric           STRING,
  pattern          STRING,
  materials        ARRAY<STRING>,
  colours          ARRAY<STRING>,
  season_mask      INT64,
  weight_g         INT64,
  p2p              BOOL,
  first_offered    DATE
)
PARTITION BY run_date
OPTIONS (partition_expiration_days = 7,
         description = "Raw daily fetch, loaded by batch job (free). The replay source if a MERGE goes wrong");

CREATE TABLE IF NOT EXISTS loppan.adjudication_staging (
  run_date         DATE NOT NULL,
  item_id          STRING NOT NULL,
  outcome          STRING NOT NULL OPTIONS (description = "sold | expired | unknown, from Parse itemStatus"),
  final_price_ore  INT64,
  adjudicated_at   TIMESTAMP
)
PARTITION BY run_date
OPTIONS (partition_expiration_days = 7);

CREATE TABLE IF NOT EXISTS loppan.circle_origin_staging (
  run_date         DATE NOT NULL,
  item_id          STRING NOT NULL OPTIONS (description = "The Circle listing"),
  original_id      STRING,
  bought_price_ore INT64,
  opening_ore      INT64,
  rungs            INT64,
  fetched_at       TIMESTAMP
)
PARTITION BY run_date
OPTIONS (partition_expiration_days = 7);

-- ─── runs: one row per daily run; the completeness gate reads this ─────────────

CREATE TABLE IF NOT EXISTS loppan.runs (
  run_date         DATE NOT NULL,
  started_at       TIMESTAMP,
  finished_at      TIMESTAMP,
  live_ids         INT64   OPTIONS (description = "Live ids the run set out to fetch"),
  fetched          INT64   OPTIONS (description = "Ids that got any answer, present or not"),
  missing          INT64   OPTIONS (description = "Ids that came back empty"),
  new_found        INT64,
  completeness     FLOAT64 OPTIONS (description = "fetched / live_ids"),
  resolve_allowed  BOOL    OPTIONS (description = "completeness >= 0.995. If false, prices were written but nothing was resolved"),
  note             STRING
);

-- ─── parameters ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS loppan.brand_rules (
  rule             STRING NOT NULL,
  value            FLOAT64,
  note             STRING
)
OPTIONS (description = "docs/bigquery.md §12. A brand is in scope if expensive OR common");

MERGE loppan.brand_rules t
USING (
  SELECT 'min_price_kr' AS rule, 150.0 AS value,
         'Hard filter: no asking price under this is ever stored. A tracked item marked down below it is closed as below_floor' AS note
) s
ON t.rule = s.rule
WHEN NOT MATCHED THEN INSERT (rule, value, note) VALUES (s.rule, s.value, s.note);
-- Seeds only missing rules: a value tuned in the console is never overwritten here.

CREATE TABLE IF NOT EXISTS loppan.brand_exclusions (
  brand            STRING NOT NULL,
  reason           STRING
)
OPTIONS (description = "Always out of scope, before ranking. Starts with the unbranded placeholder, which would otherwise rank high on frequency");

CREATE TABLE IF NOT EXISTS loppan.cost_params (
  venue            STRING NOT NULL OPTIONS (description = "vinted | plick | circle | buy (the marketplace's buy-side shipping)"),
  fee_pct          FLOAT64,
  max_weight_g     INT64   OPTIONS (description = "Upper bound of the weight band this shipping cost applies to; NULL = any"),
  shipping_ore     INT64,
  note             STRING,
  updated_on       DATE
)
OPTIONS (description = "Hand-edited. Empty until open question 3 in docs/bigquery.md is answered");

-- ─── seasonal prior: loaded from seasonal_prior.csv by bq-schema.yml ──────────

CREATE TABLE IF NOT EXISTS loppan.seasonal_prior (
  season_group     STRING  OPTIONS (description = "warm = exactly Vår+Sommar, cold = exactly Höst+Vinter. Every other tag set uses 1.0"),
  sale_month       INT64,
  n                INT64,
  median_kept_pct  FLOAT64 OPTIONS (description = "Median share of the opening ask kept at sale"),
  seasonal_index   FLOAT64 OPTIONS (description = "median_kept_pct / its 12-month mean. Decay, not price level: docs/bigquery.md §6")
);

-- ─── additions: append below, never edit a CREATE above ────────────────────────

-- The price at first sight. For history_complete items that is the opening ask, which
-- the model's kept-share needs. A plain column so model.sql never reads price_history.
ALTER TABLE loppan.items ADD COLUMN IF NOT EXISTS first_price_ore INT64
  OPTIONS (description = "Price at first sight, öre. The opening ask only when history_complete");

CREATE TABLE IF NOT EXISTS loppan.model_params (
  rule             STRING NOT NULL,
  value            FLOAT64,
  note             STRING
)
OPTIONS (description = "Pooling weights and export limits read by model.sql. docs/bigquery.md §6-§7");

MERGE loppan.model_params t
USING (
  SELECT 'k_level' AS rule, 20.0 AS value,
         'Pseudo-sales pulling a brand x category price level toward its category' AS note
  UNION ALL SELECT 'k_season', 30.0,
         'Pseudo-sales of weight the seasonal prior keeps in each group x month cell'
  UNION ALL SELECT 'k_sell', 20.0,
         'Pseudo-resolutions pulling brand x category sell-through toward its category'
  UNION ALL SELECT 'window_days', 365.0,
         'How far back sales and resolutions count'
  UNION ALL SELECT 'export_max_pct_of_expected', 60.0,
         'Loosest bargain threshold exported to the shortlist; the dashboard filters tighter'
  UNION ALL SELECT 'export_top_n', 30000.0,
         'Cap on exported candidates, ranked by sell-through-weighted gross margin'
) s
ON t.rule = s.rule
WHEN NOT MATCHED THEN INSERT (rule, value, note) VALUES (s.rule, s.value, s.note);

-- 2026-10-02, from the fetcher's findings ──────────────────────────────────────

-- Parse's raw itemStatus beside the verdict, so the reason for an 'unknown' (and
-- distinctions the verdict folds together) survives.
ALTER TABLE loppan.adjudication_staging ADD COLUMN IF NOT EXISTS item_status STRING
  OPTIONS (description = "Parse itemStatus as returned, before mapping to outcome");

-- When the reseller bought the original. origin_of returns it; it was being dropped.
ALTER TABLE loppan.circle_origin_staging ADD COLUMN IF NOT EXISTS bought_on DATE;

-- A top-level column, not a new circle_origin field: DDL cannot add a field to an
-- existing STRUCT column.
ALTER TABLE loppan.items ADD COLUMN IF NOT EXISTS circle_bought_on DATE
  OPTIONS (description = "Circle only: the date the reseller bought the original");

ALTER TABLE loppan.items ALTER COLUMN category SET OPTIONS (
  description = "Category path as enrol.row_of reads it: the first level-2 path, three levels deep. A fourth level exists and is not kept");

ALTER TABLE loppan.brand_exclusions SET OPTIONS (
  description = "Named brands always out of scope. Unbranded items have no brand at all (brand IS NULL) and are excluded by that rule, not by a row here");

-- 2026-10-02: 150 kr is a hard filter. No price under it is ever stored ─────────────

-- Set by the fetcher when a tracked item's price is now under min_price_kr. It then
-- writes no price for that row. merge_sweep.sql closes the item as below_floor.
ALTER TABLE loppan.sweep_staging ADD COLUMN IF NOT EXISTS below_floor BOOL
  OPTIONS (description = "A tracked item now priced under min_price_kr. Its price is not written");

ALTER TABLE loppan.items ALTER COLUMN outcome SET OPTIONS (
  description = "NULL while listed; sold | expired | unknown from Parse adjudication; below_floor when a markdown took it under min_price_kr, closed without storing that price");

UPDATE loppan.brand_rules
SET note = 'Hard filter: no item or price under this is ever stored. A tracked item marked down below it is closed as below_floor'
WHERE rule = 'min_price_kr';

-- 2026-10-02: the kosher brand rule replaces the expensive / common gates (§12) ───────

-- One row per brand per count run, from `bq_fetch.py brands`: live listings at or above
-- the price floor. Read by kosher.sql.
CREATE TABLE IF NOT EXISTS loppan.brand_counts_staging (
  run_date         DATE NOT NULL,
  brand            STRING NOT NULL,
  listings         INT64 NOT NULL OPTIONS (description = "Live listings at or above min_price_kr")
)
PARTITION BY run_date
OPTIONS (partition_expiration_days = 35);

CREATE TABLE IF NOT EXISTS loppan.kosher_brands (
  brand            STRING NOT NULL,
  listings         INT64   OPTIONS (description = "Live listings at or above min_price_kr at the last count"),
  kosher_since     DATE    OPTIONS (description = "The count run that first saw min_listings"),
  counted_on       DATE
)
OPTIONS (description = "The kosher list: brands that have at some point had min_listings live listings at or above the floor. Add-only: once kosher, always kosher. Grown weekly by kosher.sql");

-- The list was first created with a kosher flag; membership is now the row itself.
ALTER TABLE loppan.kosher_brands DROP COLUMN IF EXISTS kosher;

MERGE loppan.brand_rules t
USING (SELECT 'min_listings' AS rule, 20.0 AS value,
              'Kosher gate: live listings at or above min_price_kr a brand needs to enrol new items' AS note) s
ON t.rule = s.rule
WHEN NOT MATCHED THEN INSERT (rule, value, note) VALUES (s.rule, s.value, s.note);

-- The expensive / common gates and the sold-median machinery they needed are gone.
DELETE FROM loppan.brand_rules
WHERE rule IN ('min_median_sold_kr', 'top_n_brands', 'min_sales_measured', 'exit_margin');

ALTER TABLE loppan.brand_rules SET OPTIONS (
  description = "docs/bigquery.md §12: a brand becomes kosher, for good, once it has min_listings live listings at or above min_price_kr");

-- GitHub drops scheduled runs, so bq-daily.yml fires daily mode at three slots. The
-- first to finish every step stamps its runs rows here; later slots see it and stop.
ALTER TABLE loppan.runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP OPTIONS (description = "Set when daily.sh daily finished every step, export included");

-- 2026-10-04: the daily progress row (docs/bigquery.md §5, step 10) ─────────────────

-- One row per run_date, merged ON run_date by progress.sql at the end of daily mode.
-- A snapshot at computed_at, not a reconstruction of run_date: a rerun overwrites it.
CREATE TABLE IF NOT EXISTS loppan.progress_daily (
  run_date              DATE NOT NULL,
  -- size
  live_items            INT64   OPTIONS (description = "Rows in the live partition"),
  items_ever            INT64   OPTIONS (description = "Every row in items, live or resolved"),
  enrolled_today        INT64   OPTIONS (description = "first_seen = run_date, live or already resolved"),
  -- today's resolutions
  sold_today            INT64,
  expired_today         INT64,
  below_floor_today     INT64,
  unknown_today         INT64,
  sold_total            INT64   OPTIONS (description = "Every sale recorded, all time"),
  -- movement among live items
  price_drops_today     INT64   OPTIONS (description = "Live items whose last price_history step is dated run_date and lower than the one before"),
  fav_changes_today     INT64   OPTIONS (description = "Live items whose last fav_history step is dated run_date. First sight is not a change"),
  -- from runs (the latest row for run_date)
  new_found             INT64,
  completeness          FLOAT64,
  -- model maturity, from price_level
  combos_ge1            INT64   OPTIONS (description = "brand x category groups with at least 1 sale in the model window"),
  combos_ge20           INT64   OPTIONS (description = "brand x category groups with at least 20 sales: k_level, where their own evidence outweighs the category"),
  categories_priced     INT64   OPTIONS (description = "Categories with a price level of their own"),
  -- shortlist, from shortlist_candidates
  shortlist_now         INT64,
  shortlist_season      INT64,
  -- accuracy: final price / (level x seasonal_index at the sale month), items sold in
  -- the 7 days to run_date. Median; split by the brand x category group's sales
  accuracy_median_ratio FLOAT64 OPTIONS (description = "Median of final price / expected sold price at the sale month, items sold in the last 7 days. In-sample: docs/bigquery.md"),
  accuracy_n            INT64,
  accuracy_thin_ratio   FLOAT64 OPTIONS (description = "The same, for items whose brand x category has under 20 sales"),
  accuracy_thin_n       INT64,
  accuracy_thick_ratio  FLOAT64 OPTIONS (description = "The same, for groups with 20 or more sales"),
  accuracy_thick_n      INT64,
  -- Circle origins: the live backlog daily mode works through
  circle_with_origin    INT64   OPTIONS (description = "Live Circle items with a purchase price"),
  circle_without_origin INT64   OPTIONS (description = "Live Circle items still without one"),
  -- cost
  storage_gib           FLOAT64 OPTIONS (description = "Logical GiB stored in the loppan dataset, from TABLE_STORAGE"),
  billed_gib_today      FLOAT64 OPTIONS (description = "GiB billed in the project on run_date (Stockholm day) up to computed_at"),
  computed_at           TIMESTAMP
)
OPTIONS (description = "One row per run_date: is the pipeline progressing? Merged by progress.sql at the end of daily mode. docs/bigquery.md §5");

-- The service account cannot read TABLE_STORAGE, so progress.sql falls back to __TABLES__.
ALTER TABLE loppan.progress_daily ALTER COLUMN storage_gib SET OPTIONS (
  description = "Logical GiB stored in the loppan dataset: TABLE_STORAGE, else loppan.__TABLES__");
ALTER TABLE loppan.progress_daily ALTER COLUMN combos_ge20 SET OPTIONS (
  description = "brand x category groups with at least 20 sales (k_level): their own evidence weighs at least as much as the category's");
