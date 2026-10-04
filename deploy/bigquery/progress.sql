-- Daily step 10 (docs/bigquery.md §5): one row saying whether the pipeline is progressing.
-- Parameter: @run DATE. Runs last in daily mode, after the model and the export.
--
-- Merged ON run_date into loppan.progress_daily, so a rerun overwrites the day's row.
-- The row is a snapshot at computed_at: live counts are now, not as of @run.
--
-- Cost (on-demand bytes; the minimum is 10 MB per table referenced):
--   live partition    first_seen, p2p, circle_origin and the two history arrays. The
--                     arrays dominate at 16 bytes an element: at 2.3M live items and
--                     1-3 elements each, ~50-150 MB. Nothing outside the partition.
--   resolved rows     outcome over the resolved partitions for sold_total (~6-9 bytes a
--                     resolved row: ~100 MB after a year); today's counts and the
--                     7-day accuracy read only @run's month partitions.
--   model tables      price_level, seasonal_index, shortlist_candidates, runs: KB.
--   INFORMATION_SCHEMA  TABLE_STORAGE and JOBS_BY_PROJECT, 10 MB minimum each.
-- So ~0.1-0.3 GB a day, against the 3-4 GB the run itself bills.

DECLARE storage_gib FLOAT64;
DECLARE billed_gib FLOAT64;

-- Logical bytes, the unit the free 10 GiB is measured in. TABLE_STORAGE can lag a
-- little behind the day's writes. If it is unreadable, fall back to the dataset's own
-- __TABLES__, which the dataset grant always covers.
BEGIN
  SET storage_gib = (
    SELECT ROUND(SUM(total_logical_bytes) / POW(1024, 3), 3)
    FROM `region-eu`.INFORMATION_SCHEMA.TABLE_STORAGE
    WHERE table_schema = 'loppan' AND NOT deleted);
EXCEPTION WHEN ERROR THEN
  SET storage_gib = (SELECT ROUND(SUM(size_bytes) / POW(1024, 3), 3) FROM loppan.__TABLES__);
END;

-- Every job in the project on the run's Stockholm day, up to now. A script's parent
-- job repeats its children's bytes, so SCRIPT rows are left out to count them once.
-- Needs roles/bigquery.resourceViewer; without it the column stays NULL.
BEGIN
  SET billed_gib = (
    SELECT ROUND(IFNULL(SUM(total_bytes_billed), 0) / POW(1024, 3), 3)
    FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE creation_time >= TIMESTAMP(@run, 'Europe/Stockholm')
      AND creation_time <  TIMESTAMP(DATE_ADD(@run, INTERVAL 1 DAY), 'Europe/Stockholm')
      AND IFNULL(statement_type, '') != 'SCRIPT');
EXCEPTION WHEN ERROR THEN
  SET billed_gib = NULL;
END;

MERGE loppan.progress_daily t
USING (
  WITH live AS (   -- the live partition, read once
    SELECT COUNT(*) AS live_items,
           COUNTIF(first_seen = @run) AS enrolled,
           -- The arrays only ever grow at the end, one element per change, so the last
           -- element is today's if anything changed today. SAFE_ORDINAL of 0 is NULL,
           -- so a one-element (first sight) or empty array counts as no change.
           COUNTIF(price_history[SAFE_ORDINAL(ARRAY_LENGTH(price_history))].on_date = @run
               AND price_history[SAFE_ORDINAL(ARRAY_LENGTH(price_history))].price_ore
                 < price_history[SAFE_ORDINAL(ARRAY_LENGTH(price_history) - 1)].price_ore)
             AS price_drops,
           COUNTIF(fav_history[SAFE_ORDINAL(ARRAY_LENGTH(fav_history))].on_date = @run
               AND ARRAY_LENGTH(fav_history) >= 2) AS fav_changes,
           COUNTIF(p2p AND circle_origin IS NOT NULL) AS circle_with,
           COUNTIF(p2p AND circle_origin IS NULL) AS circle_without
    FROM loppan.items
    WHERE resolved_on IS NULL
  ),
  today AS (       -- @run's month partition only
    SELECT COUNTIF(outcome = 'sold') AS sold, COUNTIF(outcome = 'expired') AS expired,
           COUNTIF(outcome = 'below_floor') AS below_floor,
           COUNTIF(outcome = 'unknown') AS unknown,
           COUNTIF(first_seen = @run) AS enrolled
    FROM loppan.items
    WHERE resolved_on = @run
  ),
  totals AS (      -- COUNT(*) is metadata; outcome is NULL, so free, in the live partition
    SELECT COUNT(*) AS items_ever, COUNTIF(outcome = 'sold') AS sold_total
    FROM loppan.items
  ),
  -- Expected sold price at the sale month, exactly as model.sql prices a live item:
  -- the brand x category level, else the category's, times the season group's index.
  acc AS (
    SELECT s.final_price_ore / (COALESCE(pl.level_ore, plc.level_ore) * si.seasonal_index)
             AS ratio,
           IFNULL(pl.n_sales, 0) >= 20 AS thick
    FROM loppan.items s
    JOIN loppan.seasonal_index si
      ON si.grp = CASE s.season_mask WHEN 3 THEN 'warm' WHEN 12 THEN 'cold' ELSE 'flat' END
     AND si.sale_month = EXTRACT(MONTH FROM s.resolved_on)
    LEFT JOIN loppan.price_level pl  ON pl.brand = s.brand AND pl.category = s.category
    LEFT JOIN loppan.price_level plc ON plc.brand IS NULL AND plc.category = s.category
    WHERE s.outcome = 'sold' AND s.final_price_ore > 0
      AND s.resolved_on > DATE_SUB(@run, INTERVAL 7 DAY) AND s.resolved_on <= @run
  ),
  accuracy AS (    -- APPROX_QUANTILES ignores NULLs, so each split medians its own rows
    SELECT APPROX_QUANTILES(ratio, 2)[SAFE_OFFSET(1)] AS med, COUNT(ratio) AS n,
           APPROX_QUANTILES(IF(thick, NULL, ratio), 2)[SAFE_OFFSET(1)] AS thin_med,
           COUNTIF(NOT thick AND ratio IS NOT NULL) AS thin_n,
           APPROX_QUANTILES(IF(thick, ratio, NULL), 2)[SAFE_OFFSET(1)] AS thick_med,
           COUNTIF(thick AND ratio IS NOT NULL) AS thick_n
    FROM acc
  ),
  maturity AS (
    SELECT COUNTIF(brand IS NOT NULL AND n_sales >= 1)  AS combos_ge1,
           COUNTIF(brand IS NOT NULL AND n_sales >= 20) AS combos_ge20,
           COUNTIF(brand IS NULL) AS categories_priced
    FROM loppan.price_level
  ),
  shortlist AS (
    SELECT COUNTIF(signal = 'now') AS now_n, COUNTIF(signal = 'season') AS season_n
    FROM loppan.shortlist_candidates
  ),
  run_row AS (     -- the latest runs row for @run, as health.sql and the gate read it
    SELECT ARRAY_AGG(STRUCT(new_found, completeness)
                     ORDER BY finished_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS r
    FROM loppan.runs
    WHERE run_date = @run
  )
  SELECT @run AS run_date,
         live.live_items, totals.items_ever,
         live.enrolled + today.enrolled AS enrolled_today,
         today.sold AS sold_today, today.expired AS expired_today,
         today.below_floor AS below_floor_today, today.unknown AS unknown_today,
         totals.sold_total,
         live.price_drops AS price_drops_today, live.fav_changes AS fav_changes_today,
         run_row.r.new_found, run_row.r.completeness,
         maturity.combos_ge1, maturity.combos_ge20, maturity.categories_priced,
         shortlist.now_n AS shortlist_now, shortlist.season_n AS shortlist_season,
         ROUND(accuracy.med, 4) AS accuracy_median_ratio, accuracy.n AS accuracy_n,
         ROUND(accuracy.thin_med, 4) AS accuracy_thin_ratio, accuracy.thin_n AS accuracy_thin_n,
         ROUND(accuracy.thick_med, 4) AS accuracy_thick_ratio, accuracy.thick_n AS accuracy_thick_n,
         live.circle_with AS circle_with_origin, live.circle_without AS circle_without_origin,
         storage_gib, billed_gib AS billed_gib_today,
         CURRENT_TIMESTAMP() AS computed_at
  -- Every CTE is exactly one aggregate row, even over no input, so this cross join
  -- always yields the day's row: a missing runs row only leaves two NULLs.
  FROM live, today, totals, accuracy, maturity, shortlist, run_row
) s
ON t.run_date = s.run_date
WHEN MATCHED THEN UPDATE SET
  live_items = s.live_items, items_ever = s.items_ever, enrolled_today = s.enrolled_today,
  sold_today = s.sold_today, expired_today = s.expired_today,
  below_floor_today = s.below_floor_today, unknown_today = s.unknown_today,
  sold_total = s.sold_total, price_drops_today = s.price_drops_today,
  fav_changes_today = s.fav_changes_today, new_found = s.new_found,
  completeness = s.completeness, combos_ge1 = s.combos_ge1, combos_ge20 = s.combos_ge20,
  categories_priced = s.categories_priced, shortlist_now = s.shortlist_now,
  shortlist_season = s.shortlist_season, accuracy_median_ratio = s.accuracy_median_ratio,
  accuracy_n = s.accuracy_n, accuracy_thin_ratio = s.accuracy_thin_ratio,
  accuracy_thin_n = s.accuracy_thin_n, accuracy_thick_ratio = s.accuracy_thick_ratio,
  accuracy_thick_n = s.accuracy_thick_n, circle_with_origin = s.circle_with_origin,
  circle_without_origin = s.circle_without_origin, storage_gib = s.storage_gib,
  billed_gib_today = s.billed_gib_today, computed_at = s.computed_at
-- The source lists every column in the table's order, so INSERT ROW maps one to one.
WHEN NOT MATCHED THEN INSERT ROW;
