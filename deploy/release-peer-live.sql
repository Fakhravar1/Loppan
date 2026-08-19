-- release_peer_live() — hand the staging shelf back after the levels have scored.
--
-- `peer_live` is one row per live item, ~603,000 of them, ~62 MB with its three
-- indexes. `docs/analytics.md` §1 already says it is "rebuilt from `items` every pass
-- and worthless after one" — but nothing ever emptied it, so it sat at full size
-- between passes. That is 62 MB held for nothing, and because `analytics.py` runs from
-- `track.yml` on `30 4 */2 * *` it was held for **two days out of every two days**.
-- It is what put the database over Supabase's 500 MB limit alongside table bloat.
--
-- Runs as step 6 of the `peer` chain, after score_peer_level(3). Not folded into
-- level 3, because a chain step that also tidies up is a step that cannot be re-run.
--
-- ⚠️ **Deleting the peer_stage_state row is the point of this function, not
-- housekeeping alongside the truncate.** score_peer_level()'s guard is:
--
--     select now() - staged_at into age from public.peer_stage_state;
--     if age is null or age > interval '30 minutes' then raise exception
--
-- It reads the marker and *never looks at peer_live itself*. So a fresh marker over an
-- emptied table is the one state in which scoring passes its own guard, finds no rows
-- to score, inserts nothing, and returns 0 as a success. Truncating without clearing
-- the marker would manufacture exactly the silent-success failure the guard exists to
-- prevent — and would leave a 30-minute window after every pass in which a re-run of
-- `analytics.py --as-of ...` quietly wiped the peer layer.
--
-- Clearing it lands the guard on `age is null` instead, whose message is "was never
-- staged ... Run stage_peer_live() first" — which is both true and the right
-- instruction, since stage_peer_live() refills the table.
--
-- The delete precedes the truncate so the ordering reads as intended. A plpgsql body is
-- one transaction, so the two cannot separate in practice; if anyone ever splits them,
-- this order fails safe (a populated table with no marker refuses to score, which costs
-- a re-stage) and the reverse fails dangerous.

create or replace function public.release_peer_live()
returns integer
language plpgsql
security definer
set search_path to 'public', 'pg_temp'
as $function$
declare released integer;
begin
  select live_rows into released from public.peer_stage_state;

  delete from public.peer_stage_state;
  truncate public.peer_live;

  return coalesce(released, 0);
end $function$;

comment on function public.release_peer_live() is
  'Empties peer_live and clears the peer_stage_state marker after the levels have '
  'scored. Clearing the marker is mandatory: score_peer_level() guards on staged_at '
  'and not on peer_live''s contents, so an empty table under a fresh marker scores '
  'nothing and reports success. Returns the row count released.';
