-- Daily step 5 (docs/bigquery.md §5): fold one run's fetch into items.
-- Parameter: @run DATE.
--
-- Safe to rerun: a row already stamped with this run is skipped, so a second pass
-- appends nothing. Reads only the live partition of items (resolved_on IS NULL).
-- Ids that came back empty (present = false) are ignored here; adjudication decides
-- what happened to them.

MERGE loppan.items t
USING (
  SELECT *
  FROM loppan.sweep_staging
  WHERE run_date = @run AND present
  -- An id can arrive twice in one run (tracked, and also found as new on an overlap
  -- day). MERGE needs one source row per target row: prefer the tracked fetch.
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY item_id ORDER BY IF(source = 'track', 0, 1), fetched_at DESC) = 1
) s
ON t.item_id = s.item_id AND t.resolved_on IS NULL

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
WHEN NOT MATCHED BY TARGET AND s.source IN ('new', 'census') THEN INSERT (
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
