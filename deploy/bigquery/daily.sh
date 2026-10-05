#!/usr/bin/env bash
# Loppan's BigQuery run (docs/bigquery.md §5). Run by bq-daily.yml.
#
#   daily.sh census    one-off backfill: brand counts -> kosher list -> every live
#                      kosher item at or above the floor -> items
#   daily.sh daily     the daily sweep: track every live id, find new listings, merge,
#                      adjudicate what vanished, Circle origins, resolve, model
#   daily.sh origins   work through the Circle-origin backlog (up to ORIGINS_MAX)
#   daily.sh export    rebuild the model (model.sql) from the current items, then
#                      re-export shortlist_candidates to Supabase
#
# daily ends by exporting the shortlist to Supabase (§7), which needs
# LOPPAN_SUPABASE_KEY. Everything before it is BigQuery-only. Then it writes the
# progress row and prints the PROGRESS block, and last stamps runs.completed_at;
# daily exits at once if today already has that stamp, unless FORCE=1.
#
# Brand counts and the kosher list refresh on census, on Mondays, and whenever the
# list is empty. Every SQL file is idempotent, so rerunning a day is safe: staging
# rows dedupe in the merges, and the resolve is gated on the latest runs row.
# Needs an authenticated bq and python; no third-party packages.
set -euo pipefail
cd "$(dirname "$0")/../.."

MODE=${1:?usage: daily.sh census|daily|origins|export}
RUN=${RUN_DATE:-$(TZ=Europe/Stockholm date +%F)}
OUT=${OUT_DIR:-$(mktemp -d)}
ORIGINS_MAX=${ORIGINS_MAX:-1500}      # Parse is serial at ~2 s an item: ~50 min
CHUNK=300000                          # rows per load job
STARTED=$(date -u +%FT%TZ)

BQ=(bq --location=EU --quiet)
FETCH=(python loppan/bq_fetch.py)

step()     { echo "::group::$*"; T0=$SECONDS; }
done_()    { echo "  took $((SECONDS - T0)) s"; echo "::endgroup::"; }
sqlfile()  { "${BQ[@]}" query --use_legacy_sql=false --format=none \
               --parameter="run:DATE:$RUN" < "deploy/bigquery/$1"; }
# With Workload Identity credentials bq prints a "WARNING: --scopes ..." line to stdout,
# ahead of the result. Drop it before parsing anything.
bqout()    { "${BQ[@]}" query --use_legacy_sql=false "$@" | { grep -v '^WARNING:' || true; }; }
scalar()   { bqout --format=csv "$1" | tail -n +2 | head -1; }
# One query, its result pages read in full. Never repeated queries (§10).
column()   { bqout --format=json --max_rows=100000000 "$1" \
               | python -c "import json,sys; [print(r['$2']) for r in json.load(sys.stdin)]"; }

# Validate, then load in chunks so no single load job carries a multi-GB file.
load() {
  local table=$1 file=$2
  if [ ! -s "$file" ]; then echo "  $table: nothing to load"; return 0; fi
  "${FETCH[@]}" validate "$file" --table "$table"
  local n; n=$(wc -l < "$file")
  if [ "$n" -le "$CHUNK" ]; then
    "${BQ[@]}" load --source_format=NEWLINE_DELIMITED_JSON "loppan.$table" "$file"
  else
    split -l "$CHUNK" -d "$file" "$file.part."
    for part in "$file".part.*; do
      "${BQ[@]}" load --source_format=NEWLINE_DELIMITED_JSON "loppan.$table" "$part"
    done
  fi
  echo "  $table: loaded $n rows"
}

echo "mode=$MODE run=$RUN out=$OUT"

# ── Already done today? ─────────────────────────────────────────────────────────
# bq-daily.yml fires daily mode at three slots because GitHub drops scheduled runs.
# The first run to finish every step stamps runs.completed_at (the last step below);
# a later slot sees the stamp and stops here, before any work, brand counts included.
# FORCE=1 skips the check and runs the day again: every merge is idempotent.
if [ "$MODE" = daily ]; then
  if [ "${FORCE:-0}" = 1 ]; then
    echo "FORCE=1: not checking for a completed run today"
  else
    completed=$(scalar "select count(*) from loppan.runs
                        where run_date = '$RUN' and completed_at is not null")
    if [ "${completed:-0}" -gt 0 ]; then
      at=$(scalar "select format_timestamp('%FT%TZ', max(completed_at)) from loppan.runs
                   where run_date = '$RUN'")
      echo "today's run already completed at $at; nothing to do"
      exit 0
    fi
  fi
fi

# ── Brand counts -> kosher list (§12) ───────────────────────────────────────────
kosher_count=$(scalar 'select count(*) from loppan.kosher_brands')
if [ "$MODE" = census ] || [ "$(TZ=Europe/Stockholm date +%u)" = 1 ] || [ "$kosher_count" = 0 ]; then
  step "brand counts -> kosher list"
  "${FETCH[@]}" brands --run-date "$RUN" --out "$OUT/brand_counts.ndjson" --summary "$OUT/brands.json"
  load brand_counts_staging "$OUT/brand_counts.ndjson"
  sqlfile kosher.sql
  echo "  kosher brands: $(scalar 'select count(*) from loppan.kosher_brands') (was $kosher_count)"
  done_
fi
column 'select brand from loppan.kosher_brands' brand > "$OUT/kosher.txt"
echo "kosher list: $(wc -l < "$OUT/kosher.txt") brands"

# A runs row for this run. track writes one; census and origins build their own.
# new_found is filled in here, and started_at is the whole run's start.
runs_row() {   # file live_ids fetched missing new_found completeness note
  python - "$@" "$RUN" "$STARTED" <<'PY'
import json, sys, datetime as dt
f, live, fetched, missing, new_found, comp, note, run, started = sys.argv[1:10]
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
c = float(comp)
row = {"run_date": run, "started_at": started, "finished_at": now,
       "live_ids": int(live), "fetched": int(fetched), "missing": int(missing),
       "new_found": int(new_found), "completeness": c,
       "resolve_allowed": c >= 0.995, "completed_at": None, "note": note}
open(f, "w", encoding="utf-8").write(json.dumps(row) + "\n")
PY
}

origins() {   # Circle purchase prices for live p2p items that lack one, newest first
  local limit=$1
  column "select item_id from loppan.items
          where resolved_on is null and p2p and circle_origin is null
          order by first_seen desc limit $limit" item_id > "$OUT/origin_ids.txt"
  if [ -s "$OUT/origin_ids.txt" ]; then
    "${FETCH[@]}" origins --run-date "$RUN" --ids "$OUT/origin_ids.txt" \
      --out "$OUT/origins.ndjson" --summary "$OUT/origins.json"
    load circle_origin_staging "$OUT/origins.ndjson"
  fi
  echo "  origins asked: $(wc -l < "$OUT/origin_ids.txt")"
}

export_shortlist() {   # one query for every candidate, then Supabase (§7)
  step "export shortlist to Supabase"
  bqout --format=json --max_rows=1000000 'select * from loppan.shortlist_candidates' \
    > "$OUT/candidates.json"
  python loppan/bq_export.py --candidates "$OUT/candidates.json" --summary "$OUT/export.json"
  done_
}

# The day's progress row (progress.sql), then the PROGRESS block: plain lines between
# fixed markers, outside any ::group:: so the log shows it open, and so a morning check
# can cut it out of `gh run view --log` (docs/bigquery.md §5, step 10). A report, not a
# pipeline step: a failure here warns and never fails a run whose data is already in.
progress() {
  step "progress"
  sqlfile progress.sql || echo "::warning::progress.sql failed: no progress row for $RUN"
  done_
  { bqout --format=json "select * except (computed_at) from loppan.progress_daily
           where run_date between date_sub(date '$RUN', interval 6 day) and date '$RUN'" \
      || true; } | python deploy/bigquery/progress_block.py "$RUN" \
    || echo "::warning::the PROGRESS block failed"
}

case "$MODE" in
census)
  step "census: every live kosher item at or above the floor"
  "${FETCH[@]}" census --run-date "$RUN" --brands "$OUT/kosher.txt" \
    --out "$OUT/census.ndjson" --summary "$OUT/census.json"
  load sweep_staging "$OUT/census.ndjson"
  before=$(scalar 'select count(*) from loppan.items where resolved_on is null')
  sqlfile merge_sweep.sql
  live=$(scalar 'select count(*) from loppan.items where resolved_on is null')
  runs_row "$OUT/runs.ndjson" "$live" "$live" 0 $((live - before)) 1.0 \
    "census: $(wc -l < "$OUT/census.ndjson") staged, $((live - before)) enrolled"
  load runs "$OUT/runs.ndjson"
  echo "  live items: $before -> $live"
  done_
  ;;

daily)
  step "live ids"
  column 'select item_id from loppan.items where resolved_on is null' item_id > "$OUT/live_ids.txt"
  echo "  $(wc -l < "$OUT/live_ids.txt") live ids"
  [ -s "$OUT/live_ids.txt" ] || { echo "no live items: run the census first" >&2; exit 1; }
  done_

  step "track"
  "${FETCH[@]}" track --run-date "$RUN" --ids "$OUT/live_ids.txt" --retries 1 \
    --out "$OUT/track.ndjson" --runs-out "$OUT/track_runs.ndjson" \
    --gone-out "$OUT/gone.txt" --summary "$OUT/track.json"
  done_

  step "new listings"
  since=$(scalar "select ifnull(cast(date_sub(max(run_date), interval 1 day) as string),
                                cast(date_sub(date '$RUN', interval 2 day) as string))
                  from loppan.runs where run_date < date '$RUN'")
  "${FETCH[@]}" new --run-date "$RUN" --since "$since" --brands "$OUT/kosher.txt" \
    --out "$OUT/new.ndjson" --summary "$OUT/new.json"
  echo "  since $since: $(wc -l < "$OUT/new.ndjson") found"
  done_

  step "load and merge"
  load sweep_staging "$OUT/track.ndjson"
  load sweep_staging "$OUT/new.ndjson"
  python - "$OUT/track_runs.ndjson" "$OUT/new.ndjson" "$STARTED" <<'PY'
import json, sys
path, new, started = sys.argv[1:4]
row = json.loads(open(path, encoding="utf-8").readline())
row["new_found"] = sum(1 for _ in open(new, encoding="utf-8"))
row["started_at"] = started
open(path, "w", encoding="utf-8").write(json.dumps(row) + "\n")
PY
  load runs "$OUT/track_runs.ndjson"
  sqlfile merge_sweep.sql
  done_

  allowed=$(scalar "select resolve_allowed from loppan.runs where run_date = '$RUN'
                    order by finished_at desc limit 1")
  step "adjudicate (gate: $allowed)"
  if [ "$allowed" = true ] && [ -s "$OUT/gone.txt" ]; then
    "${FETCH[@]}" adjudicate --run-date "$RUN" --ids "$OUT/gone.txt" \
      --out "$OUT/adjudication.ndjson" --summary "$OUT/adjudication.json"
    load adjudication_staging "$OUT/adjudication.ndjson"
  else
    echo "  skipped: gate closed or nothing vanished ($(wc -l < "$OUT/gone.txt" 2>/dev/null || echo 0) candidates)"
  fi
  done_

  step "Circle origins (up to $ORIGINS_MAX)"
  origins "$ORIGINS_MAX"
  done_

  step "resolve and model"
  sqlfile merge_resolve.sql
  sqlfile model.sql
  echo "  live: $(scalar 'select count(*) from loppan.items where resolved_on is null')"
  echo "  candidates: $(scalar 'select count(*) from loppan.shortlist_candidates')"
  done_

  export_shortlist
  progress

  # Keep this the very last step of daily: the stamp means every step above finished,
  # and the guard at the top trusts it.
  step "mark the run complete"
  bqout --format=none --parameter="run:DATE:$RUN" \
    'UPDATE loppan.runs SET completed_at = CURRENT_TIMESTAMP()
     WHERE run_date = @run AND completed_at IS NULL'
  echo "  completed_at: $(scalar "select format_timestamp('%FT%TZ', max(completed_at))
                                  from loppan.runs where run_date = '$RUN'")"
  done_
  ;;

export)
  step "model"
  sqlfile model.sql
  echo "  candidates: $(scalar 'select count(*) from loppan.shortlist_candidates')"
  done_

  export_shortlist
  ;;

origins)
  step "Circle origin backlog (up to $ORIGINS_MAX)"
  origins "$ORIGINS_MAX"
  sqlfile merge_resolve.sql   # writes the origins; resolves nothing without adjudications
  done_
  ;;

*) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

echo "billed this run: $(scalar "select round(ifnull(sum(total_bytes_billed), 0) / pow(1024, 3), 3)
  from \`region-eu\`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  where creation_time >= timestamp('$STARTED')") GiB"
