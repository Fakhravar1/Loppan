-- Refresh the kosher brand list from one count run (docs/bigquery.md §12).
-- Parameter: @run DATE. Run weekly, after `bq_fetch.py brands` has been loaded into
-- brand_counts_staging for @run.
--
-- A brand is kosher once it has min_listings (20) live listings at or above the price
-- floor, and stops being kosher only below min_listings x (1 - exit_margin), i.e. 18.
-- Between the two it keeps its status, so a brand hovering around 20 does not flip
-- weekly. Only kosher brands enrol new items; a brand that drops off keeps its tracked
-- items until their outcome (merge_sweep.sql enrols, it never removes).

DECLARE min_listings FLOAT64 DEFAULT (SELECT value FROM loppan.brand_rules WHERE rule = 'min_listings');
DECLARE exit_margin  FLOAT64 DEFAULT (SELECT value FROM loppan.brand_rules WHERE rule = 'exit_margin');

-- A partial count would wipe brands it missed. Refuse anything that looks like one.
ASSERT (SELECT COUNT(*) FROM loppan.brand_counts_staging WHERE run_date = @run) >= 1000
  AS 'Fewer than 1,000 brands counted for this run: refusing to refresh the kosher list from a partial count.';

MERGE loppan.kosher_brands t
USING (
  SELECT brand, listings
  FROM loppan.brand_counts_staging
  WHERE run_date = @run
  QUALIFY ROW_NUMBER() OVER (PARTITION BY brand ORDER BY listings DESC) = 1
) s
ON t.brand = s.brand

WHEN MATCHED THEN UPDATE SET
  kosher = CASE
             WHEN s.listings >= min_listings THEN TRUE
             WHEN s.listings < min_listings * (1 - exit_margin) THEN FALSE
             ELSE t.kosher
           END,
  kosher_since = CASE
                   WHEN NOT t.kosher AND s.listings >= min_listings THEN @run
                   WHEN s.listings < min_listings * (1 - exit_margin) THEN NULL
                   ELSE t.kosher_since
                 END,
  listings   = s.listings,
  counted_on = @run

WHEN NOT MATCHED BY TARGET THEN INSERT (brand, listings, kosher, kosher_since, counted_on)
VALUES (s.brand, s.listings, s.listings >= min_listings,
        IF(s.listings >= min_listings, @run, NULL), @run)

-- Absent from a full count: no live listings at or above the floor at all.
WHEN NOT MATCHED BY SOURCE AND t.counted_on < @run THEN UPDATE SET
  listings = 0, kosher = FALSE, kosher_since = NULL, counted_on = @run;
