-- Daily health check, run by bq-health.yml. An ASSERT that fails fails the workflow,
-- and a failed scheduled workflow emails the repository owner: that is the alert.
--
-- Before the daily pipeline exists, loppan.runs is empty and only the cost check runs,
-- so the schedule can be live from day one without failing every morning.

DECLARE last_run DATE DEFAULT (SELECT MAX(run_date) FROM loppan.runs);

IF last_run IS NULL THEN
  SELECT 'pipeline not started: loppan.runs is empty' AS status;
ELSE
  ASSERT last_run >= DATE_SUB(CURRENT_DATE('Europe/Stockholm'), INTERVAL 1 DAY)
    AS 'No pipeline run today or yesterday. Check the daily workflow.';

  ASSERT (SELECT resolve_allowed FROM loppan.runs
          WHERE run_date = last_run ORDER BY finished_at DESC LIMIT 1)
    AS 'Latest run was under the 99.5% completeness gate: prices were written, nothing was resolved.';

  SELECT run_date, live_ids, fetched, missing, new_found,
         ROUND(completeness, 4) AS completeness, resolve_allowed
  FROM loppan.runs
  WHERE run_date = last_run
  ORDER BY finished_at DESC
  LIMIT 1;
END IF;

-- Every job in the project, yours included. 10 GiB/day is ~300 GiB/month, well inside
-- the free 1 TiB and a third of the 30 GiB/day quota, so this warns long before the
-- quota starts refusing queries.
ASSERT (
  SELECT IFNULL(SUM(total_bytes_billed), 0)
  FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
) < 10 * 1024 * 1024 * 1024
  AS 'Over 10 GiB billed in the last 24 h. Find the job in INFORMATION_SCHEMA.JOBS_BY_PROJECT.';

SELECT ROUND(IFNULL(SUM(total_bytes_billed), 0) / POW(1024, 3), 3) AS gib_billed_24h,
       COUNT(*) AS jobs_24h
FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR);
