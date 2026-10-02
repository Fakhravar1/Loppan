-- Daily step 7 (docs/bigquery.md §5): Circle origins, then Parse's verdicts.
-- Parameter: @run DATE. Runs after merge_sweep.sql and the adjudication fetch.

DECLARE floor_ore INT64 DEFAULT CAST(
  (SELECT value FROM loppan.brand_rules WHERE rule = 'min_price_kr') * 100 AS INT64);

-- Circle purchase prices for live p2p items that lack one. First, so an item that
-- resolves in this same run still gets the price it was bought at.
MERGE loppan.items t
USING (
  SELECT *
  FROM loppan.circle_origin_staging
  WHERE run_date = @run
  QUALIFY ROW_NUMBER() OVER (PARTITION BY item_id ORDER BY fetched_at DESC) = 1
) s
ON t.item_id = s.item_id AND t.resolved_on IS NULL
WHEN MATCHED AND t.circle_origin IS NULL THEN UPDATE SET
  circle_origin = STRUCT(s.original_id AS original_id,
                         s.bought_price_ore AS bought_price_ore,
                         s.opening_ore AS opening_ore,
                         s.rungs AS rungs),
  circle_bought_on = s.bought_on;

-- Outcomes. Gated on the run's completeness (§3, change 3): unless the latest runs row
-- for @run allows it, nothing resolves. A partial fetch must never become a wave of
-- false sales. Safe to rerun: a resolved row has left the live partition.
MERGE loppan.items t
USING (
  SELECT a.*
  FROM loppan.adjudication_staging a
  WHERE a.run_date = @run
    AND IFNULL((SELECT resolve_allowed
                FROM loppan.runs
                WHERE run_date = @run
                ORDER BY finished_at DESC
                LIMIT 1), FALSE)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY a.item_id ORDER BY a.adjudicated_at DESC) = 1
) s
ON t.item_id = s.item_id AND t.resolved_on IS NULL
-- Parse's last ask can be under the floor if a markdown crossed it between runs. The
-- hard filter (§12) stores no asking price under 150 kr, so such an item closes as
-- below_floor with no final price: it did not sell at or above the floor.
WHEN MATCHED THEN UPDATE SET
  outcome         = IF(s.final_price_ore < floor_ore, 'below_floor', s.outcome),
  resolved_on     = @run,
  final_price_ore = IF(s.final_price_ore < floor_ore, NULL, s.final_price_ore),
  updated_run     = @run;
