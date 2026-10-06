-- The blind spot: how many listings sell before the daily run first sees them?
-- Read-only. Run by bq-query.yml.
--
-- A listing that sells before its first sight is never enrolled, so it never enters
-- sell-through. It cannot be counted directly, but its rate can be estimated from the
-- listings we did see young: items listed since the pipeline started (history_complete)
-- are first seen at age 0-2 days, and the share of them sold by the next run is the
-- early sale hazard over one run gap. The time before first sight is on average half a
-- run gap, so roughly half that share of new listings is missed.
-- Dates are Stockholm days; first_seen and resolved_on are run dates.

-- 1. The run gaps: hours from each run date's first start to the next one's.
WITH run_days AS (
  SELECT run_date, MIN(started_at) AS first_start
  FROM loppan.runs GROUP BY run_date
)
SELECT run_date,
       LEAD(run_date) OVER (ORDER BY run_date) AS next_run,
       ROUND(TIMESTAMP_DIFF(LEAD(first_start) OVER (ORDER BY run_date), first_start,
                            MINUTE) / 60, 1) AS hours_to_next
FROM run_days
ORDER BY run_date;

-- 2. Young listings by the run that first saw them and their age then: how many were
--    sold, or closed below the floor, by the next run.
WITH run_days AS (
  SELECT run_date, MIN(started_at) AS first_start
  FROM loppan.runs GROUP BY run_date
),
gaps AS (
  SELECT run_date,
         LEAD(run_date) OVER (ORDER BY run_date) AS next_run,
         TIMESTAMP_DIFF(LEAD(first_start) OVER (ORDER BY run_date), first_start,
                        MINUTE) / 60 AS hours_to_next
  FROM run_days
)
SELECT i.first_seen, g.next_run, ROUND(g.hours_to_next, 1) AS hours_to_next,
       DATE_DIFF(i.first_seen, i.first_offered, DAY) AS age_at_sight,
       COUNT(*) AS seen,
       COUNTIF(i.outcome = 'sold' AND i.resolved_on <= g.next_run) AS sold_by_next,
       ROUND(100 * COUNTIF(i.outcome = 'sold' AND i.resolved_on <= g.next_run)
             / COUNT(*), 2) AS pct_sold_by_next,
       ROUND(100 * COUNTIF(i.outcome = 'sold' AND i.resolved_on <= g.next_run)
             / COUNT(*) / g.hours_to_next * 24, 2) AS pct_sold_per_24h,
       COUNTIF(i.outcome = 'below_floor' AND i.resolved_on <= g.next_run) AS below_floor_by_next
FROM loppan.items i
JOIN gaps g ON g.run_date = i.first_seen
WHERE i.history_complete AND g.next_run IS NOT NULL
GROUP BY 1, 2, 3, 4
HAVING seen >= 100
ORDER BY 1, 4;

-- 3. Every sale of a young listing so far, by its age at sale: how front-loaded selling is.
SELECT DATE_DIFF(resolved_on, first_offered, DAY) AS age_at_sale_days,
       COUNT(*) AS sold,
       ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM loppan.items
WHERE history_complete AND outcome = 'sold'
GROUP BY 1
ORDER BY 1;

-- 4. Scale: new listings enrolled per run, and sales per run, for the denominator.
SELECT run_date,
       ANY_VALUE(new_found) AS new_found,
       ANY_VALUE(live_ids) AS live_ids
FROM loppan.runs
GROUP BY run_date
ORDER BY run_date;
