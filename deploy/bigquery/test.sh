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
        brand_rules kosher_brands brand_counts_staging
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
CREATE OR REPLACE TABLE loppan._t_brand_rules           LIKE loppan.brand_rules;
INSERT loppan._t_brand_rules (rule, value) VALUES
  ('min_price_kr', 150), ('min_listings', 20);
CREATE OR REPLACE TABLE loppan._t_kosher_brands         LIKE loppan.kosher_brands;
INSERT loppan._t_kosher_brands (brand, listings, kosher_since, counted_on)
VALUES ('Acme', 500, CURRENT_DATE(), CURRENT_DATE());   -- 'Tiny' has no row: not kosher
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
  ('$D2', 'C', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(), 19000, 0, FALSE,
   'Acme', 'Man > Skor', 0, FALSE, '$D2'),
  ('$D2', 'C', 'census', TRUE, TRUE, TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR), 19000, 0, FALSE,
   'Acme', 'Man > Skor', 0, FALSE, '$D2'),
  ('$D2', 'D', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(),  9000, 0, FALSE,
   'Acme', 'Man > Skor', 0, FALSE, '$D2'),
  ('$D2', 'F', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(), 25000, 1, FALSE,
   'Acme', 'Barn > Kläder', 0, FALSE, '$D2'),
  ('$D2', 'E', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(), 20000, 0, FALSE,
   'Acme', 'Kvinna > Skor', 0, FALSE, '$D2'),
  ('$D2', 'G', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(), 30000, 0, FALSE,
   'Tiny', 'Man > Skor', 0, FALSE, '$D2'),
  ('$D2', 'H', 'new',    TRUE, TRUE, CURRENT_TIMESTAMP(), 30000, 0, FALSE,
   NULL, 'Man > Skor', 0, FALSE, '$D2'),
  ('$D2', 'Z', 'track',  TRUE, TRUE, CURRENT_TIMESTAMP(),  1000, 0, FALSE, NULL, NULL, NULL, NULL, NULL);
EOF
run_file merge_sweep.sql "$D2"
run_file merge_sweep.sql "$D2"
sql <<EOF
ASSERT (SELECT COUNT(*) FROM loppan._t_items) = 5
  AS 'day 2: A, B, C, E, F. Duplicate C collapses; D is under the floor, G not kosher, H unbranded, Z stray';
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
ASSERT (SELECT COUNTIF(updated_run = '$D2') FROM loppan._t_items) = 5 AS 'all stamped day 2';
EOF
pass "day 2: change-only appends, dedupe, no stray enrolment, floor + kosher on enrolment, attributes kept"

# ── Day 3: A vanishes and sold, B gets its Circle origin; gate closed, then open ──
sql <<EOF
INSERT loppan._t_sweep_staging (run_date, item_id, source, present, fetched_at, price_ore,
  favourites, below_floor)
VALUES ('$D3', 'A', 'track', FALSE, CURRENT_TIMESTAMP(), NULL,  NULL, NULL),
       ('$D3', 'E', 'track', FALSE, CURRENT_TIMESTAMP(), NULL,  NULL, NULL),
       ('$D3', 'B', 'track', TRUE,  CURRENT_TIMESTAMP(), 45000, 3,    NULL),
       ('$D3', 'C', 'track', TRUE,  CURRENT_TIMESTAMP(), 14000, 0,    NULL),
       ('$D3', 'F', 'track', TRUE,  CURRENT_TIMESTAMP(), NULL,  1,    TRUE);
INSERT loppan._t_adjudication_staging (run_date, item_id, outcome, final_price_ore, adjudicated_at)
VALUES ('$D3', 'A', 'sold', 27000, CURRENT_TIMESTAMP()),
       ('$D3', 'E', 'sold', 12000, CURRENT_TIMESTAMP());
INSERT loppan._t_circle_origin_staging (run_date, item_id, original_id, bought_price_ore,
  opening_ore, rungs, fetched_at, bought_on)
VALUES ('$D3', 'B', 'B0', 12000, 40000, 6, CURRENT_TIMESTAMP(), DATE_SUB('$D3', INTERVAL 30 DAY));
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
ASSERT (SELECT circle_bought_on FROM loppan._t_items WHERE item_id = 'B') = DATE_SUB('$D3', INTERVAL 30 DAY)
  AS 'Circle purchase date written';
ASSERT (SELECT outcome FROM loppan._t_items WHERE item_id = 'C') = 'below_floor'
  AS 'a markdown under 150 kr closes the item (backstop: price comparison)';
ASSERT (SELECT outcome FROM loppan._t_items WHERE item_id = 'F') = 'below_floor'
  AS 'a fetcher-flagged item is closed without a price';
ASSERT (SELECT price_ore FROM loppan._t_items WHERE item_id = 'C') = 19000
  AS 'C keeps its last price at or above the floor';
ASSERT (SELECT COUNTIF(price_ore < 15000) FROM loppan._t_items) = 0
  AS 'no current price under 150 kr is stored';
ASSERT (SELECT COUNT(*) FROM loppan._t_items, UNNEST(price_history) AS p
        WHERE p.price_ore < 15000) = 0
  AS 'no historical price under 150 kr is stored';
EOF
pass "day 3: completeness gate holds, missing is not sold, Circle origin, hard 150 kr floor"

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
ASSERT (SELECT outcome FROM loppan._t_items WHERE item_id = 'E') = 'below_floor'
  AS 'a final ask under 150 kr closes as below_floor';
ASSERT (SELECT final_price_ore IS NULL FROM loppan._t_items WHERE item_id = 'E')
  AS 'and its sub-floor final price is not stored';
ASSERT (SELECT COUNTIF(final_price_ore < 15000) FROM loppan._t_items) = 0
  AS 'no final price under 150 kr is stored';
ASSERT (SELECT COUNT(*) FROM loppan._t_items WHERE resolved_on IS NULL) = 1
  AS 'A, C, E and F left the live partition; only B is live';
ASSERT (SELECT COUNT(*) FROM loppan._t_items) = 5 AS 'resolving moved rows, it did not copy them';
EOF
pass "day 3: gate open, resolves once, row moves partition"

# ── runs.completed_at: daily.sh's last statement, which its guard reads ──────
# One D3 row carries an old stamp: the update must keep it and stamp only NULLs.
sql <<EOF
INSERT loppan._t_runs (run_date, finished_at, completeness, resolve_allowed, completed_at)
VALUES ('$D3', CURRENT_TIMESTAMP(), 1.0, TRUE, TIMESTAMP '2000-01-01'),
       ('$D2', CURRENT_TIMESTAMP(), 1.0, TRUE, NULL);
EOF
"${BQ[@]}" --parameter="run:DATE:$D3" \
  'UPDATE loppan._t_runs SET completed_at = CURRENT_TIMESTAMP()
   WHERE run_date = @run AND completed_at IS NULL'
sql <<EOF
ASSERT (SELECT COUNTIF(completed_at IS NULL) FROM loppan._t_runs WHERE run_date = '$D3') = 0
  AS 'every row of the run is stamped';
ASSERT (SELECT COUNTIF(completed_at = TIMESTAMP '2000-01-01') FROM loppan._t_runs) = 1
  AS 'an existing stamp is kept';
ASSERT (SELECT completed_at IS NULL FROM loppan._t_runs WHERE run_date = '$D2')
  AS 'another day is not stamped';
EOF
pass "completed_at: stamps the run's rows only, keeps an earlier stamp"

# Dry run: what the daily MERGE would read, against the whole table.
merge_bytes=$(retarget merge_sweep.sql | bq --location=EU query --use_legacy_sql=false \
  --dry_run --parameter="run:DATE:$D3" 2>&1 | grep -oE '[0-9]+ bytes' | head -1 || true)
table_bytes=$(bq --location=EU query --use_legacy_sql=false --dry_run \
  'select * from loppan._t_items' 2>&1 | grep -oE '[0-9]+ bytes' | head -1 || true)
echo "  info  merge_sweep dry run reads ${merge_bytes:-?}; the whole items table is ${table_bytes:-?}"

# ── Kosher list: joins at 20, never leaves ──────────────────────────────────
sql <<EOF
CREATE OR REPLACE TABLE loppan._t_brand_counts_staging LIKE loppan.brand_counts_staging;
CREATE OR REPLACE TABLE loppan._t_kosher_brands LIKE loppan.kosher_brands;
INSERT loppan._t_kosher_brands (brand, listings, kosher_since, counted_on)
VALUES ('Dipped', 25, '$D1', '$D1'), ('Missing', 40, '$D1', '$D1');
INSERT loppan._t_brand_counts_staging (run_date, brand, listings) VALUES
  ('$D3', 'Dipped', 5), ('$D3', 'New20', 20), ('$D3', 'New25', 25), ('$D3', 'New19', 19);
EOF
run_file kosher.sql "$D3"
run_file kosher.sql "$D3"   # rerun: no change
sql <<EOF
ASSERT (SELECT COUNT(*) FROM loppan._t_kosher_brands) = 4
  AS 'Dipped and Missing stay, New20 and New25 join, New19 does not; the rerun added nothing';
ASSERT (SELECT listings = 5 AND kosher_since = '$D1' FROM loppan._t_kosher_brands WHERE brand = 'Dipped')
  AS 'a brand that dips to 5 stays kosher, with its count updated and its join date kept';
ASSERT (SELECT COUNT(*) FROM loppan._t_kosher_brands WHERE brand = 'Missing') = 1
  AS 'a brand absent from a count is not removed';
ASSERT (SELECT kosher_since = '$D3' FROM loppan._t_kosher_brands WHERE brand = 'New20')
  AS 'exactly 20 joins';
ASSERT (SELECT COUNT(*) FROM loppan._t_kosher_brands WHERE brand = 'New19') = 0
  AS '19 does not join';
EOF
pass "kosher: joins at 20, never leaves, absent is not removed, rerun is a no-op"

# ── Model: a fresh items table with known answers ───────────────────────────
sql <<'EOF'
CREATE OR REPLACE TABLE loppan._t_items LIKE loppan.items;
INSERT loppan._t_items (item_id, brand, category, season_mask, outcome, resolved_on,
                        final_price_ore, price_ore, history_complete)
SELECT CONCAT('As', CAST(i AS STRING)), 'A', 'C', 0, 'sold', DATE '2026-09-01', 30000, 30000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 5)) AS i
UNION ALL
SELECT CONCAT('Ax', CAST(i AS STRING)), 'A', 'C', 0, 'expired', DATE '2026-09-01', 90000, 90000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 5)) AS i
UNION ALL
SELECT CONCAT('Bs', CAST(i AS STRING)), 'B', 'C', 0, 'sold', DATE '2026-09-01', 60000, 60000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 15)) AS i
UNION ALL
SELECT CONCAT('Qs', CAST(i AS STRING)), 'Q', 'D', 0, 'sold', DATE '2026-09-01', 20000, 20000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 2)) AS i
UNION ALL
SELECT CONCAT('Qf', CAST(i AS STRING)), 'Q', 'D', 0, 'below_floor', DATE '2026-09-01', NULL, 16000, FALSE
FROM UNNEST(GENERATE_ARRAY(1, 2)) AS i
UNION ALL SELECT 'L1', 'A', 'C', 0,  NULL, NULL, NULL,  15000, FALSE
UNION ALL SELECT 'L2', 'A', 'C', 0,  NULL, NULL, NULL, 150000, FALSE
UNION ALL SELECT 'L3', 'B', 'C', 12, NULL, NULL, NULL,  18000, FALSE
UNION ALL SELECT 'L5', 'B', 'C', 12, NULL, NULL, NULL,  40000, FALSE;
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
ASSERT (SELECT ROUND(level_ore) FROM loppan._t_price_level WHERE brand = 'A' AND category = 'C') = 54000
  AS 'A pooled toward C: (5 x 30000 + 20 x 60000) / 25';
ASSERT (SELECT ROUND(level_ore) FROM loppan._t_price_level WHERE brand IS NULL AND category = 'C') = 60000
  AS 'the category level is its own median';
ASSERT (SELECT ROUND(sell_through, 4) FROM loppan._t_sell_through WHERE brand = 'A' AND category = 'C') = 0.7
  AS 'A sell-through: (5 + 20 x 0.8) / (10 + 20)';
ASSERT (SELECT resolved FROM loppan._t_sell_through WHERE brand = 'Q' AND category = 'D') = 4
  AS 'below_floor counts as a resolution';
ASSERT (SELECT ROUND(sell_through, 4) FROM loppan._t_sell_through
        WHERE brand = 'Q' AND category = 'D') = 0.5
  AS 'below_floor counts as not sold: (2 + 20 x 0.5) / (4 + 20)';
ASSERT (SELECT gross_margin_ore FROM loppan._t_shortlist_candidates WHERE item_id = 'L1') = 22800
  AS 'L1 margin: 0.7 x 54000 - 15000';
ASSERT (SELECT COUNT(*) FROM loppan._t_shortlist_candidates WHERE item_id = 'L2') = 0
  AS 'L2 is overpriced and excluded';
ASSERT (SELECT peak_month FROM loppan._t_shortlist_candidates WHERE item_id = 'L3')
     = (SELECT sale_month FROM loppan.seasonal_prior
        WHERE season_group = 'cold' ORDER BY seasonal_index DESC LIMIT 1)
  AS 'a cold item peaks in the cold prior peak month';
ASSERT (SELECT LOGICAL_AND(expected_profit_ore IS NULL) FROM loppan._t_shortlist_candidates)
  AS 'profit stays NULL without cost_params';
ASSERT (SELECT signal FROM loppan._t_shortlist_candidates WHERE item_id = 'L1') = 'now'
  AS 'L1 is cheap now: 15000 / 54000 = 28% of expected';
ASSERT (SELECT signal FROM loppan._t_shortlist_candidates WHERE item_id = 'L3') = 'now'
  AS 'L3 is cheap now: 18000 / (60000 x cold October ~0.83) = 36%';
ASSERT (SELECT signal FROM loppan._t_shortlist_candidates WHERE item_id = 'L5') = 'season'
  AS 'L5 is ~80% of expected now, in only as a seasonal bet';
ASSERT (SELECT LOGICAL_AND((signal = 'now') = (pct_of_expected <= 60))
        FROM loppan._t_shortlist_candidates)
  AS 'signal is now exactly when the price is at most 60% of expected now';
ASSERT (SELECT gross_margin_ore FROM loppan._t_shortlist_candidates WHERE item_id = 'L5')
     > (SELECT gross_margin_ore FROM loppan._t_shortlist_candidates WHERE item_id = 'L1')
  AS 'fixture: the season item L5 out-earns the now item L1, so the cap test means something';
UPDATE loppan._t_model_params SET value = 2 WHERE rule = 'export_top_n';
EOF
run_file model.sql 2026-10-02
sql <<'EOF'
ASSERT (SELECT STRING_AGG(item_id ORDER BY item_id) FROM loppan._t_shortlist_candidates) = 'L1,L3'
  AS 'top 2: both now items survive the cap, the higher-margin season item L5 does not';
UPDATE loppan._t_model_params SET value = 30000 WHERE rule = 'export_top_n';
EOF
pass "model: prior reproduced, pooling, sell-through, margin, exclusion, seasonal peak, signal, now-first cap"

echo "all checks passed"
