#!/usr/bin/env bash
# Tests merge_sweep.sql, merge_resolve.sql and model.sql against synthetic rows in
# throwaway loppan._t_* tables, which are dropped on exit whatever happens.
# Run by bq-schema.yml after schema.sql is applied. Needs only an authenticated bq.
#
# Every check is a BigQuery ASSERT: a failed one fails the query, and set -e fails the
# script. seasonal_prior is read for real; everything else is retargeted to _t_ copies.
set -euo pipefail
cd "$(dirname "$0")"

BQ=(bq --location=EU --quiet query --use_legacy_sql=false --format=none)
TABLES=(items sweep_staging adjudication_staging circle_origin_staging runs model_params
        seasonal_index price_level sell_through shortlist_candidates)

cleanup() {
  for t in "${TABLES[@]}"; do bq --location=EU rm -f -t "loppan._t_$t" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT

retarget() {
  local alt; alt=$(IFS='|'; echo "${TABLES[*]}")
  sed -E "s/\bloppan\.($alt)\b/loppan._t_\1/g" "$1"
}
run_file() { retarget "$1" | "${BQ[@]}" --parameter="run:DATE:$2"; }
sql()      { "${BQ[@]}"; }
pass()     { echo "  ok  $*"; }

D1=$(date -u -d '-2 day' +%F)
D2=$(date -u -d '-1 day' +%F)
D3=$(date -u +%F)

echo "fixtures: run dates $D1, $D2, $D3"
sql <<EOF
CREATE OR REPLACE TABLE loppan._t_items                 LIKE loppan.items;
CREATE OR REPLACE TABLE loppan._t_sweep_staging         LIKE loppan.sweep_staging;
CREATE OR REPLACE TABLE loppan._t_adjudication_staging  LIKE loppan.adjudication_staging;
CREATE OR REPLACE TABLE loppan._t_circle_origin_staging LIKE loppan.circle_origin_staging;
CREATE OR REPLACE TABLE loppan._t_runs                  LIKE loppan.runs;
CREATE OR REPLACE TABLE loppan._t_model_params          LIKE loppan.model_params;
INSERT loppan._t_model_params (rule, value) VALUES
  ('k_level', 20), ('k_season', 30), ('k_sell', 20), ('window_days', 365),
  ('export_max_pct_of_expected', 60), ('export_top_n', 30000);
EOF

# ── Day 1: two items enrol ──────────────────────────────────────────────────
sql <<EOF
INSERT loppan._t_sweep_staging (run_date, item_id, source, present, is_for_sale, fetched_at,
  price_ore, favourites, last_chance, brand, category, season_mask, p2p, first_offered)
VALUES
  ('$D1', 'A', 'census', TRUE, TRUE, CURRENT_TIMESTAMP(), 30000, 2, FALSE,
   'Acme', 'Kvinna > Jackor', 12, FALSE, DATE_SUB('$D1', INTERVAL 40 DAY)),
  ('$D1', 'B', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(), 50000, 0, FALSE,
   'Acme', 'Kvinna > Jackor', 3, TRUE, '$D1');
EOF
run_file merge_sweep.sql "$D1"
run_file merge_sweep.sql "$D1"   # the rerun must change nothing
sql <<EOF
ASSERT (SELECT COUNT(*) FROM loppan._t_items) = 2 AS 'day 1: two rows';
ASSERT (SELECT ARRAY_LENGTH(price_history) FROM loppan._t_items WHERE item_id = 'A') = 1
  AS 'day 1 rerun appended a second price';
ASSERT (SELECT first_price_ore FROM loppan._t_items WHERE item_id = 'A') = 30000
  AS 'first_price_ore is the price at first sight';
ASSERT (SELECT history_complete FROM loppan._t_items WHERE item_id = 'A') = FALSE
  AS 'a census item listed 40 days ago is left-censored';
ASSERT (SELECT history_complete FROM loppan._t_items WHERE item_id = 'B') = TRUE
  AS 'an item found by listing date has its whole life';
EOF
pass "day 1: enrol, rerun is a no-op, first price, history_complete"

# ── Day 2: a markdown, a like, a new item seen twice, a stray tracked id ─────
sql <<EOF
INSERT loppan._t_sweep_staging (run_date, item_id, source, present, is_for_sale, fetched_at,
  price_ore, favourites, last_chance, brand, category, season_mask, p2p, first_offered)
VALUES
  ('$D2', 'A', 'track',  TRUE, TRUE, CURRENT_TIMESTAMP(), 27000, 2, FALSE, NULL, NULL, NULL, NULL, NULL),
  ('$D2', 'B', 'track',  TRUE, TRUE, CURRENT_TIMESTAMP(), 50000, 3, FALSE, NULL, NULL, NULL, NULL, NULL),
  ('$D2', 'C', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(),  9000, 0, FALSE,
   'Acme', 'Man > Skor', 0, FALSE, '$D2'),
  ('$D2', 'C', 'census', TRUE, TRUE, TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR), 9000, 0, FALSE,
   'Acme', 'Man > Skor', 0, FALSE, '$D2'),
  ('$D2', 'Z', 'track',  TRUE, TRUE, CURRENT_TIMESTAMP(),  1000, 0, FALSE, NULL, NULL, NULL, NULL, NULL);
EOF
run_file merge_sweep.sql "$D2"
run_file merge_sweep.sql "$D2"
sql <<EOF
ASSERT (SELECT COUNT(*) FROM loppan._t_items) = 3
  AS 'day 2: A, B, C. The duplicate C collapses and the stray tracked Z never enrols';
ASSERT (SELECT ARRAY_LENGTH(price_history) FROM loppan._t_items WHERE item_id = 'A') = 2
  AS 'A markdown appended once';
ASSERT (SELECT price_history[OFFSET(1)].price_ore FROM loppan._t_items WHERE item_id = 'A') = 27000
  AS 'A latest price element';
ASSERT (SELECT price_history[OFFSET(1)].on_date FROM loppan._t_items WHERE item_id = 'A') = '$D2'
  AS 'A latest price dated to the run';
ASSERT (SELECT ARRAY_LENGTH(fav_history) FROM loppan._t_items WHERE item_id = 'A') = 1
  AS 'unchanged likes append nothing';
ASSERT (SELECT ARRAY_LENGTH(fav_history) FROM loppan._t_items WHERE item_id = 'B') = 2
  AS 'B new like appended once';
ASSERT (SELECT ARRAY_LENGTH(price_history) FROM loppan._t_items WHERE item_id = 'B') = 1
  AS 'unchanged price appends nothing';
ASSERT (SELECT brand FROM loppan._t_items WHERE item_id = 'A') = 'Acme'
  AS 'a track row with null attributes must not blank them';
ASSERT (SELECT COUNTIF(updated_run = '$D2') FROM loppan._t_items) = 3 AS 'all stamped day 2';
EOF
pass "day 2: change-only appends, dedupe, no stray enrolment, attributes kept"

# ── Day 3: A vanishes and sold, B gets its Circle origin; gate closed, then open ──
sql <<EOF
INSERT loppan._t_sweep_staging (run_date, item_id, source, present, fetched_at, price_ore, favourites)
VALUES ('$D3', 'A', 'track', FALSE, CURRENT_TIMESTAMP(), NULL, NULL),
       ('$D3', 'B', 'track', TRUE,  CURRENT_TIMESTAMP(), 45000, 3);
INSERT loppan._t_adjudication_staging (run_date, item_id, outcome, final_price_ore, adjudicated_at)
VALUES ('$D3', 'A', 'sold', 27000, CURRENT_TIMESTAMP());
INSERT loppan._t_circle_origin_staging (run_date, item_id, original_id, bought_price_ore,
  opening_ore, rungs, fetched_at)
VALUES ('$D3', 'B', 'B0', 12000, 40000, 6, CURRENT_TIMESTAMP());
INSERT loppan._t_runs (run_date, finished_at, live_ids, fetched, completeness, resolve_allowed)
VALUES ('$D3', TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR), 1000, 990, 0.99, FALSE);
EOF
run_file merge_sweep.sql "$D3"
run_file merge_resolve.sql "$D3"
sql <<EOF
ASSERT (SELECT outcome FROM loppan._t_items WHERE item_id = 'A') IS NULL
  AS 'gate closed: nothing may resolve on a 99% run';
ASSERT (SELECT last_seen FROM loppan._t_items WHERE item_id = 'A') = '$D2'
  AS 'a missing item keeps its last sighting';
ASSERT (SELECT ARRAY_LENGTH(price_history) FROM loppan._t_items WHERE item_id = 'B') = 2
  AS 'B markdown on day 3';
ASSERT (SELECT circle_origin.bought_price_ore FROM loppan._t_items WHERE item_id = 'B') = 12000
  AS 'Circle origin written';
EOF
pass "day 3: completeness gate holds, missing is not sold, Circle origin"

sql <<EOF
INSERT loppan._t_runs (run_date, finished_at, live_ids, fetched, completeness, resolve_allowed)
VALUES ('$D3', CURRENT_TIMESTAMP(), 1000, 999, 0.999, TRUE);
EOF
run_file merge_resolve.sql "$D3"
run_file merge_resolve.sql "$D3"
sql <<EOF
ASSERT (SELECT outcome FROM loppan._t_items WHERE item_id = 'A') = 'sold' AS 'A resolved once the gate opens';
ASSERT (SELECT resolved_on FROM loppan._t_items WHERE item_id = 'A') = '$D3' AS 'resolved on the run date';
ASSERT (SELECT final_price_ore FROM loppan._t_items WHERE item_id = 'A') = 27000 AS 'final price from Parse';
ASSERT (SELECT COUNT(*) FROM loppan._t_items WHERE resolved_on IS NULL) = 2 AS 'A left the live partition';
ASSERT (SELECT COUNT(*) FROM loppan._t_items) = 3 AS 'resolving moved the row, it did not copy it';
EOF
pass "day 3: gate open, resolves once, row moves partition"

# Dry run: what the daily MERGE would read, against the whole table.
merge_bytes=$(retarget merge_sweep.sql | bq --location=EU query --use_legacy_sql=false \
  --dry_run --parameter="run:DATE:$D3" 2>&1 | grep -oE '[0-9]+ bytes' | head -1 || true)
table_bytes=$(bq --location=EU query --use_legacy_sql=false --dry_run \
  'select * from loppan._t_items' 2>&1 | grep -oE '[0-9]+ bytes' | head -1 || true)
echo "  info  merge_sweep dry run reads ${merge_bytes:-?}; the whole items table is ${table_bytes:-?}"

# ── Model: a fresh items table with known answers ───────────────────────────
sql <<'EOF'
CREATE OR REPLACE TABLE loppan._t_items LIKE loppan.items;
INSERT loppan._t_items (item_id, brand, category, season_mask, outcome, resolved_on,
                        final_price_ore, price_ore, history_complete)
SELECT CONCAT('As', CAST(i AS STRING)), 'A', 'C', 0, 'sold', DATE '2026-09-01', 10000, 10000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 5)) AS i
UNION ALL
SELECT CONCAT('Ax', CAST(i AS STRING)), 'A', 'C', 0, 'expired', DATE '2026-09-01', 30000, 30000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 5)) AS i
UNION ALL
SELECT CONCAT('Bs', CAST(i AS STRING)), 'B', 'C', 0, 'sold', DATE '2026-09-01', 20000, 20000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 15)) AS i
UNION ALL SELECT 'L1', 'A', 'C', 0,  NULL, NULL, NULL,  5000, FALSE
UNION ALL SELECT 'L2', 'A', 'C', 0,  NULL, NULL, NULL, 50000, FALSE
UNION ALL SELECT 'L3', 'B', 'C', 12, NULL, NULL, NULL,  6000, FALSE;
EOF
run_file model.sql 2026-10-02
sql <<'EOF'
ASSERT (SELECT MAX(ABS(si.seasonal_index - p.seasonal_index))
        FROM loppan._t_seasonal_index si
        JOIN loppan.seasonal_prior p
          ON p.season_group = si.grp AND p.sale_month = si.sale_month) < 0.01
  AS 'with no measured sales the index is the prior';
ASSERT (SELECT COUNTIF(seasonal_index = 1.0) FROM loppan._t_seasonal_index WHERE grp = 'flat') = 12
  AS 'flat is 1.0 in every month';
ASSERT (SELECT ROUND(level_ore) FROM loppan._t_price_level WHERE brand = 'A' AND category = 'C') = 18000
  AS 'A pooled toward C: (5 x 10000 + 20 x 20000) / 25';
ASSERT (SELECT ROUND(level_ore) FROM loppan._t_price_level WHERE brand IS NULL AND category = 'C') = 20000
  AS 'the category level is its own median';
ASSERT (SELECT ROUND(sell_through, 4) FROM loppan._t_sell_through WHERE brand = 'A' AND category = 'C') = 0.7
  AS 'A sell-through: (5 + 20 x 0.8) / (10 + 20)';
ASSERT (SELECT gross_margin_ore FROM loppan._t_shortlist_candidates WHERE item_id = 'L1') = 7600
  AS 'L1 margin: 0.7 x 18000 - 5000';
ASSERT (SELECT COUNT(*) FROM loppan._t_shortlist_candidates WHERE item_id = 'L2') = 0
  AS 'L2 is overpriced and excluded';
ASSERT (SELECT peak_month FROM loppan._t_shortlist_candidates WHERE item_id = 'L3')
     = (SELECT sale_month FROM loppan.seasonal_prior
        WHERE season_group = 'cold' ORDER BY seasonal_index DESC LIMIT 1)
  AS 'a cold item peaks in the cold prior peak month';
ASSERT (SELECT LOGICAL_AND(expected_profit_ore IS NULL) FROM loppan._t_shortlist_candidates)
  AS 'profit stays NULL without cost_params';
EOF
pass "model: prior reproduced, pooling, sell-through, margin, exclusion, seasonal peak"

echo "all checks passed"
