-- What was billed in the last 24 h, and by what. Read-only. Run by bq-query.yml.
-- SCRIPT rows are parents that repeat their children's bytes, so they are shown apart
-- and never added in. Reads INFORMATION_SCHEMA only: a few MB.

-- 1. By hour (UTC) and the workflow-ish source: the first words of each statement.
SELECT TIMESTAMP_TRUNC(creation_time, HOUR) AS hour_utc,
       ROUND(SUM(IF(IFNULL(statement_type, '') != 'SCRIPT', total_bytes_billed, 0))
             / POW(1024, 3), 3) AS gib_billed,
       ROUND(SUM(IF(statement_type = 'SCRIPT', total_bytes_billed, 0))
             / POW(1024, 3), 3) AS gib_script_parents,
       COUNTIF(IFNULL(statement_type, '') != 'SCRIPT') AS jobs
FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
GROUP BY 1
ORDER BY 1;

-- 2. The 25 most expensive statements (script children included, parents left out).
SELECT creation_time, statement_type,
       ROUND(total_bytes_billed / POW(1024, 3), 3) AS gib_billed,
       REGEXP_REPLACE(SUBSTR(query, 1, 110), r'\s+', ' ') AS query_start
FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND IFNULL(statement_type, '') != 'SCRIPT'
ORDER BY total_bytes_billed DESC
LIMIT 25;
