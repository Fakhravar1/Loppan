-- Grow the kosher brand list from one count run (docs/bigquery.md §12).
-- Parameter: @run DATE. Run weekly, after `bq_fetch.py brands` has been loaded into
-- brand_counts_staging for @run.
--
-- Once kosher, always kosher. A brand joins the first time it has min_listings (20) live
-- listings at or above the price floor, and is never removed: a dip under the threshold
-- does not drop a brand that has shown it is real. Membership is the row itself.
-- Because nothing is ever removed, a partial or failed count can only delay an addition,
-- never shrink the list.

DECLARE min_listings FLOAT64 DEFAULT (SELECT value FROM loppan.brand_rules WHERE rule = 'min_listings');

MERGE loppan.kosher_brands t
USING (
  SELECT brand, MAX(listings) AS listings
  FROM loppan.brand_counts_staging
  WHERE run_date = @run
  GROUP BY brand
) s
ON t.brand = s.brand

-- Already kosher: keep the latest count for reference; membership is unaffected.
WHEN MATCHED THEN UPDATE SET
  listings   = s.listings,
  counted_on = @run

WHEN NOT MATCHED BY TARGET AND s.listings >= min_listings THEN
  INSERT (brand, listings, kosher_since, counted_on)
  VALUES (s.brand, s.listings, @run, @run);
