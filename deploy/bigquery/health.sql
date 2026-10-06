-- Daily health check, run by bq-health.yml. An ASSERT that fails fails the workflow,
-- and a failed scheduled workflow emails the repository owner: that is the alert.
--
-- last_run is the latest run that finished every step (runs.completed_at). A runs row
-- alone is not enough: it is loaded before the merges, so a run that dies later every
-- day would otherwise keep this green.

DECLARE last_run DATE DEFAULT
  (SELECT MAX(run_date) FROM loppan.runs WHERE completed_at IS NOT NULL);

ASSERT IFNULL(last_run >= DATE_SUB(CURRENT_DATE('Europe/Stockholm'), INTERVAL 1 DAY), FALSE)
  AS 'No completed pipeline run today or yesterday. Check the daily workflow.';

ASSERT (SELECT resolve_allowed FROM loppan.runs
        WHERE run_date = last_run ORDER BY finished_at DESC LIMIT 1)
  AS 'Latest run was under the 99.5% completeness gate: prices were written, nothing was resolved.';

-- Adjudication swallows request errors (an id it could not ask stays live for the next
-- run), so a broken Parse step still completes. Below-floor closes come from the merge,
-- not from Parse, so they do not count here. Reads one month partition, two columns.
ASSERT (SELECT missing FROM loppan.runs
        WHERE run_date = last_run ORDER BY finished_at DESC LIMIT 1) < 1000
    OR (SELECT COUNT(*) FROM loppan.items
        WHERE resolved_on = last_run AND outcome != 'below_floor') > 0
  AS 'Items vanished but none was adjudicated sold, expired or unknown. Check the adjudicate step.';

SELECT run_date, live_ids, fetched, missing, new_found,
       ROUND(completeness, 4) AS completeness, resolve_allowed
FROM loppan.runs
WHERE run_date = last_run
ORDER BY finished_at DESC
LIMIT 1;

-- Every job in the project, yours included. 10 GiB/day is ~300 GiB/month, well inside
-- the free 1 TiB and a third of the 30 GiB/day quota, so this warns long before the
-- quota starts refusing queries. SCRIPT rows are left out: a script's parent job repeats
-- its children's bytes (as in progress.sql), which made this fire at ~5 GiB of real use.
ASSERT (
  SELECT IFNULL(SUM(total_bytes_billed), 0)
  FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    AND IFNULL(statement_type, '') != 'SCRIPT'
) < 10 * 1024 * 1024 * 1024
  AS 'Over 10 GiB billed in the last 24 h. Find the job in INFORMATION_SCHEMA.JOBS_BY_PROJECT.';

SELECT ROUND(IFNULL(SUM(total_bytes_billed), 0) / POW(1024, 3), 3) AS gib_billed_24h,
       COUNT(*) AS jobs_24h
FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND IFNULL(statement_type, '') != 'SCRIPT';
