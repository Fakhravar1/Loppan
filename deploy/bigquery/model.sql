-- The reference price and the shortlist candidates (docs/bigquery.md §6-§7).
-- Parameter: @run DATE. Rebuilt daily after the merges; every output is replaced whole.
--
-- Reads plain columns only, never price_history, so it costs tens of bytes an item.
-- Pooling weights and limits come from loppan.model_params, so tuning needs no deploy.
--
--   seasonal_index        season group x month: the year-1 prior blended with our own sales
--   price_level           brand x category (and brand NULL = the category itself):
--                         deseasonalised median sold price, pooled toward the category
--   sell_through          brand x category (and brand NULL): sold / (sold + expired), pooled
--   shortlist_candidates  live items under the loosest threshold, with their signal
--                         ('now' or 'season'), 'now' ranked first, then by gross margin

DECLARE k_level  FLOAT64 DEFAULT (SELECT value FROM loppan.model_params WHERE rule = 'k_level');
DECLARE k_season FLOAT64 DEFAULT (SELECT value FROM loppan.model_params WHERE rule = 'k_season');
DECLARE k_sell   FLOAT64 DEFAULT (SELECT value FROM loppan.model_params WHERE rule = 'k_sell');
DECLARE window_days INT64 DEFAULT CAST(
  (SELECT value FROM loppan.model_params WHERE rule = 'window_days') AS INT64);
DECLARE max_pct  FLOAT64 DEFAULT
  (SELECT value FROM loppan.model_params WHERE rule = 'export_max_pct_of_expected');
DECLARE top_n    INT64   DEFAULT CAST(
  (SELECT value FROM loppan.model_params WHERE rule = 'export_top_n') AS INT64);

-- warm = exactly Vår+Sommar (1+2), cold = exactly Höst+Vinter (4+8). Everything else is
-- flat: too thin to estimate (§6), so its index is 1.0 in every month.
CREATE TEMP FUNCTION season_group(mask INT64) AS (
  CASE mask WHEN 3 THEN 'warm' WHEN 12 THEN 'cold' ELSE 'flat' END
);

-- ─── seasonal_index ─────────────────────────────────────────────────────────
-- Blended in kept-share space, the prior's own unit, then normalised to a 12-month mean
-- of 1. With no sales of our own this reproduces the prior exactly. Measured kept-share
-- needs the opening ask, so only history_complete sales count toward it.

CREATE OR REPLACE TABLE loppan.seasonal_index AS
WITH measured AS (
  SELECT season_group(season_mask) AS grp,
         EXTRACT(MONTH FROM resolved_on) AS sale_month,
         COUNT(*) AS n,
         APPROX_QUANTILES(final_price_ore / first_price_ore, 2)[OFFSET(1)] AS kept
  FROM loppan.items
  WHERE outcome = 'sold' AND final_price_ore > 0
    AND history_complete AND first_price_ore > 0
    AND resolved_on >= DATE_SUB(@run, INTERVAL window_days DAY)
  GROUP BY 1, 2
),
blended AS (
  SELECT p.season_group AS grp, p.sale_month,
         IFNULL(m.n, 0) AS n_sales,
         (IFNULL(m.n, 0) * IFNULL(m.kept, 0) + k_season * p.median_kept_pct / 100)
           / (IFNULL(m.n, 0) + k_season) AS kept
  FROM loppan.seasonal_prior p
  LEFT JOIN measured m ON m.grp = p.season_group AND m.sale_month = p.sale_month
)
SELECT grp, sale_month, n_sales, kept,
       kept / AVG(kept) OVER (PARTITION BY grp) AS seasonal_index
FROM blended
UNION ALL
SELECT 'flat', sale_month, 0, CAST(NULL AS FLOAT64), 1.0
FROM UNNEST(GENERATE_ARRAY(1, 12)) AS sale_month;

-- ─── price_level ────────────────────────────────────────────────────────────

CREATE OR REPLACE TABLE loppan.price_level AS
WITH sales AS (
  SELECT i.brand, i.category, i.final_price_ore / si.seasonal_index AS deseason_ore
  FROM loppan.items i
  JOIN loppan.seasonal_index si
    ON si.grp = season_group(i.season_mask)
   AND si.sale_month = EXTRACT(MONTH FROM i.resolved_on)
  WHERE i.outcome = 'sold' AND i.final_price_ore > 0
    AND i.resolved_on >= DATE_SUB(@run, INTERVAL window_days DAY)
),
bc AS (
  SELECT brand, category, COUNT(*) AS n,
         APPROX_QUANTILES(deseason_ore, 2)[OFFSET(1)] AS med
  FROM sales WHERE brand IS NOT NULL GROUP BY 1, 2
),
c AS (
  SELECT category, COUNT(*) AS n,
         APPROX_QUANTILES(deseason_ore, 2)[OFFSET(1)] AS med
  FROM sales GROUP BY 1
)
SELECT bc.brand, bc.category, bc.n AS n_sales, bc.med AS raw_level_ore,
       (bc.n * bc.med + k_level * c.med) / (bc.n + k_level) AS level_ore
FROM bc JOIN c USING (category)
UNION ALL
SELECT CAST(NULL AS STRING), category, n, med, med
FROM c;

-- ─── sell_through ───────────────────────────────────────────────────────────
-- The share that sold at or above the floor. below_floor counts as not sold: it was
-- marked down past 150 kr without selling, which is what a reseller needs to know.
-- 'unknown' outcomes are left out of both sides rather than counted as either.

CREATE OR REPLACE TABLE loppan.sell_through AS
WITH r AS (
  SELECT brand, category, outcome
  FROM loppan.items
  WHERE outcome IN ('sold', 'expired', 'below_floor')
    AND resolved_on >= DATE_SUB(@run, INTERVAL window_days DAY)
),
bc AS (
  SELECT brand, category, COUNTIF(outcome = 'sold') AS sold, COUNT(*) AS resolved
  FROM r WHERE brand IS NOT NULL GROUP BY 1, 2
),
c AS (
  SELECT category, COUNTIF(outcome = 'sold') AS sold, COUNT(*) AS resolved
  FROM r GROUP BY 1
)
SELECT bc.brand, bc.category, bc.sold, bc.resolved,
       (bc.sold + k_sell * c.sold / c.resolved) / (bc.resolved + k_sell) AS sell_through
FROM bc JOIN c USING (category)
UNION ALL
SELECT CAST(NULL AS STRING), category, sold, resolved, sold / resolved
FROM c;

-- ─── shortlist_candidates ───────────────────────────────────────────────────
-- The peak depends only on the season group, not the item, so it is found once per
-- group over the next 12 months and joined, never expanded per item.
-- expected_profit_ore stays NULL until cost_params is filled: the README's rule is that
-- profit is never faked. gross_margin_ore is before fees and shipping.
-- signal: 'now' when the item is cheap right now for its brand x category (price at most
-- max_pct of expected_now); 'season' when only the seasonal bet qualifies it. The top_n
-- cap keeps every 'now' first, by gross margin, then 'season' by gross margin.

CREATE OR REPLACE TABLE loppan.shortlist_candidates AS
WITH months AS (
  SELECT off, EXTRACT(MONTH FROM DATE_ADD(@run, INTERVAL off MONTH)) AS sale_month
  FROM UNNEST(GENERATE_ARRAY(0, 11)) AS off
),
grp_peak AS (
  SELECT si.grp,
         MAX(IF(mo.off = 0, si.seasonal_index, NULL)) AS now_idx,
         ARRAY_AGG(STRUCT(mo.off, mo.sale_month, si.seasonal_index AS idx)
                   ORDER BY si.seasonal_index DESC, mo.off LIMIT 1)[OFFSET(0)] AS peak
  FROM months mo
  JOIN loppan.seasonal_index si USING (sale_month)
  GROUP BY si.grp
),
priced AS (
  SELECT l.item_id, l.brand, l.category, l.item_type, l.size_code, l.condition,
         l.demography, l.p2p, l.history_complete, l.weight_g, l.price_ore,
         season_group(l.season_mask) AS grp,
         COALESCE(pl.level_ore, plc.level_ore) AS level_ore,
         IFNULL(pl.n_sales, 0) AS n_sales,
         COALESCE(st.sell_through, stc.sell_through) AS sell_through
  FROM loppan.items l
  LEFT JOIN loppan.price_level pl   ON pl.brand = l.brand AND pl.category = l.category
  LEFT JOIN loppan.price_level plc  ON plc.brand IS NULL AND plc.category = l.category
  LEFT JOIN loppan.sell_through st  ON st.brand = l.brand AND st.category = l.category
  LEFT JOIN loppan.sell_through stc ON stc.brand IS NULL AND stc.category = l.category
  WHERE l.resolved_on IS NULL AND l.price_ore > 0
),
scored AS (
  SELECT p.* EXCEPT (grp),
         p.level_ore * g.now_idx  AS expected_now,
         p.level_ore * g.peak.idx AS expected_peak,
         g.peak.sale_month AS peak_month,
         g.peak.off AS months_to_peak
  FROM priced p
  JOIN grp_peak g USING (grp)
  WHERE p.level_ore IS NOT NULL AND p.sell_through IS NOT NULL
)
SELECT item_id, brand, category, item_type, size_code, condition, demography, p2p,
       history_complete, weight_g, price_ore, n_sales,
       ROUND(sell_through, 3) AS sell_through,
       CAST(ROUND(expected_now) AS INT64)  AS expected_now_ore,
       CAST(ROUND(expected_peak) AS INT64) AS expected_peak_ore,
       peak_month, months_to_peak,
       ROUND(100 * price_ore / expected_now, 1) AS pct_of_expected,
       CAST(ROUND(sell_through * expected_peak - price_ore) AS INT64) AS gross_margin_ore,
       CAST(NULL AS INT64) AS expected_profit_ore,
       IF(price_ore <= max_pct / 100 * expected_now, 'now', 'season') AS signal,
       @run AS as_of
FROM scored
WHERE price_ore <= max_pct / 100 * expected_now
   OR sell_through * expected_peak > price_ore
QUALIFY ROW_NUMBER() OVER (
  ORDER BY IF(price_ore <= max_pct / 100 * expected_now, 0, 1),
           sell_through * expected_peak - price_ore DESC) <= top_n;
