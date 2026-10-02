-- Daily step 5 (docs/bigquery.md §5): fold one run's fetch into items.
-- Parameter: @run DATE.
--
-- Safe to rerun: a row already stamped with this run is skipped, so a second pass
-- appends nothing. Reads only the live partition of items (resolved_on IS NULL).
-- Ids that came back empty (present = false) are ignored here; adjudication decides
-- what happened to them.
--
-- 150 kr is a hard filter (§12): no price under min_price_kr is ever stored. An item
-- under it never enrols, and a tracked item marked down under it is closed as
-- below_floor with its last price at or above the floor. The fetcher flags these rows
-- itself; the price comparison here is the backstop.
--
-- Only kosher brands enrol (§12, kosher.sql). Unbranded items never join, since NULL
-- matches no brand. Tracked items are never removed for their brand.

DECLARE floor_ore INT64 DEFAULT CAST(
  (SELECT value FROM loppan.brand_rules WHERE rule = 'min_price_kr') * 100 AS INT64);

MERGE loppan.items t
USING (
  SELECT st.*, k.brand IS NOT NULL AS is_kosher
  FROM loppan.sweep_staging st
  LEFT JOIN loppan.kosher_brands k ON k.brand = st.brand AND k.kosher
  WHERE st.run_date = @run AND st.present
  -- An id can arrive twice in one run (tracked, and also found as new on an overlap
  -- day). MERGE needs one source row per target row: prefer the tracked fetch.
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY st.item_id ORDER BY IF(st.source = 'track', 0, 1), st.fetched_at DESC) = 1
) s
ON t.item_id = s.item_id AND t.resolved_on IS NULL

WHEN MATCHED AND (t.updated_run IS NULL OR t.updated_run < @run)
     AND (IFNULL(s.below_floor, FALSE) OR s.price_ore < floor_ore) THEN UPDATE SET
  outcome     = 'below_floor',
  resolved_on = @run,
  last_seen   = @run,
  updated_run = @run

WHEN MATCHED AND (t.updated_run IS NULL OR t.updated_run < @run) THEN UPDATE SET
  price_history = IF(s.price_ore IS NOT NULL AND s.price_ore IS DISTINCT FROM t.price_ore,
      ARRAY_CONCAT(t.price_history, [STRUCT(@run AS on_date, s.price_ore AS price_ore)]),
      t.price_history),
  fav_history = IF(s.favourites IS NOT NULL AND s.favourites IS DISTINCT FROM t.favourites,
      ARRAY_CONCAT(t.fav_history, [STRUCT(@run AS on_date, s.favourites AS favourites)]),
      t.fav_history),
  price_ore     = IFNULL(s.price_ore, t.price_ore),
  old_price_ore = IFNULL(s.old_price_ore, t.old_price_ore),
  favourites    = IFNULL(s.favourites, t.favourites),
  last_chance   = IFNULL(s.last_chance, t.last_chance),
  last_seen     = @run,
  updated_run   = @run

-- Only search results enrol. A tracked id with no live row means the id list and the
-- table disagree, which is a bug to surface, not a row to invent.
WHEN NOT MATCHED BY TARGET AND s.source IN ('new', 'census')
     AND s.price_ore >= floor_ore AND s.is_kosher THEN INSERT (
  item_id, brand, brand_tier, category, item_type, demography, size_code, condition,
  has_defect, fabric, pattern, materials, colours, season_mask, weight_g, p2p,
  first_offered, first_seen, last_seen, history_complete, updated_run,
  price_ore, old_price_ore, favourites, last_chance, first_price_ore,
  price_history, fav_history
) VALUES (
  s.item_id, s.brand, s.brand_tier, s.category, s.item_type, s.demography, s.size_code,
  s.condition, s.has_defect, s.fabric, s.pattern, s.materials, s.colours, s.season_mask,
  s.weight_g, s.p2p, s.first_offered, @run, @run,
  -- Whole life observed: found by listing date, or listed no earlier than yesterday.
  IFNULL(s.source = 'new' OR s.first_offered >= DATE_SUB(@run, INTERVAL 1 DAY), FALSE),
  @run,
  s.price_ore, s.old_price_ore, s.favourites, s.last_chance, s.price_ore,
  IF(s.price_ore IS NULL, ARRAY<STRUCT<on_date DATE, price_ore INT64>>[],
     [STRUCT(@run AS on_date, s.price_ore AS price_ore)]),
  IF(s.favourites IS NULL, ARRAY<STRUCT<on_date DATE, favourites INT64>>[],
     [STRUCT(@run AS on_date, s.favourites AS favourites)])
);
