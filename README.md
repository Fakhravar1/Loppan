# Loppan

Research project: does it pay to buy underpriced second-hand items on online second hand stores,
hold them, and relist them on online second hand stores Circle at 80% seller share?

**Status: measurement only. No buying logic, no automation of purchases.**

Four real round trips exist (see `docs/handover.md` §3.5). They returned +106% on
completed trades, but they were hand-picked after the fact and three of four sold,
so they establish that the upside exists and nothing else. The number that decides
whether this is a business — what fraction of bought items ever sell — has never
been measured.

## What's here

| Path | What it is |
|---|---|
| `docs/overview.md` | **Start here.** What the idea is, how the business would work, the constraints, the theories and their standing, and how the crawler operates |
| `docs/schema.md` | Every column we collect, what it means, and the three that will mislead you |
| `docs/handover.md` | The full design document: ideas, evidence, economics, open questions |
| `docs/analytics.md` | What is derived after each pass — the predictor board, brand attention index, peer prices — and the three ways this data misleads |
| `docs/api-notes.md` | **Read before writing a query.** Which Parse queries work, which time out, and why |
| `docs/pi-vpn.md` | How the crawl leaves the house through a tunnel, scoped so it cannot affect the Qvitta project |
| `loppan/online second hand stores.py` | Read-only client for online second hand stores's public Parse backend |
| `loppan/shortlist.py` | Builds the undervalued shortlist the dashboard grid reads, and fetches its pictures |

## The two things worth doing first

1. **Backtest on history.** online second hand stores retains every price step of every item, with
   timestamps, readable long after the item sold. So a buying rule can be tested
   against months of real market behaviour without spending anything or waiting.
2. **Observation cohort.** Follow a stratified, systematically-selected set of
   live items to their outcomes — including the ones that quietly expire, which
   are the entire point.

Both are described in `docs/handover.md` §11–§12.

## Ground rules

- **Read-only.** Nothing here authenticates as a user or writes to online second hand stores.
- **One request per second.** Enforced in `online second hand stores.py`. The risk that matters is
  the account, not the scraper.
- **Never submit fabricated data anywhere.** Inherited from the sibling project's
  standing rule, and it applies here too.

## The forward cohort

1,300 items enrolled 2026-08-04 and followed to their outcome. This is the
project's one live experiment, and it exists because **sell-through cannot be
recovered from history**: items that sell are removed from the search index, so
any backward-looking sample sees survivors and failures but never a success.

**It runs itself** — `.github/workflows/cohort-check.yml`, daily at 05:20 UTC. It
reads the cohort from Postgres, so the runner needs no local state, and it skips
items that have already resolved, so it gets cheaper every run and stops when the
last item resolves. Run it by hand with `python loppan/cohort.py check`.

| Stratum | n | Frozen expectation |
|---|---|---|
| `circle` | 500 | unknown — **this is the measurement** |
| `screen` | 150 | sells faster than baseline |
| `control_unwanted` | 150 | slower than `screen` — same discount, nobody watching |
| `control_wanted_pricey` | 150 | sells, but slowly |
| `premium` | 100 | slower — thinner buyer pool at 1,500 kr+ |
| `baseline` | 250 | the reference rate |

Predictions were frozen at enrolment in `data/cohort_manifest.json`. **Do not edit
the baseline** — the value of this is entirely in having chosen before knowing.

### After any enrolment, run the origin backfill

```bash
python loppan/backfill_circle_origin.py
```

Sell-through alone cannot say whether a trade is profitable — you also need the
multiple, and that needs the price the Circle seller *paid*. Every Circle listing
points back to the item it came from via `preceding`, but that link is not
captured by the enrolment snapshot. **Run this before tracked Circle items start
selling**, or you will know what they fetched and never what they cost.

It is idempotent (it only touches rows where `original_id` is null), so re-running
after any future snapshot is safe and is the intended habit. The result lands in
`v_circle_outcomes`.

The cohort is mirrored to Postgres (below). `data/` remains the local write-ahead
copy, so a database problem can never cost a week of observations.

## Postgres

Supabase project **`zgqywowejxtokqsybqnu`** (`Loppan`, region `eu-north-1`), free tier.

Tables: `items` · `brands` · `strata` · `cohort_items` · `cohort_checks` ·
`circle_roundtrips` · `circle_origins` · `item_ladders` · `peer_prices` ·
`predictor_daily` · `brand_daily` · `shortlist_daily`, plus the `v_*` views.

**Reads require a login *and* the allowlist.** Every table has RLS on with a policy
scoped `to authenticated using (is_allowed())`, and the views are
`security_invoker = on`, so they inherit it. An anonymous caller gets `401`; a
signed-in non-member gets `[]`; a member sees everything. Add one with
`insert into public.app_users (email, note) values (...)`.

⚠️ This README previously said three curated views were "readable without a login".
That was true for about a day in August 2026 and was reverted; the wording outlived
the change. Verified closed again 2026-08-10 over HTTP with the publishable key.
`docs/api-notes.md` has the history and the reasoning.

⚠️ **Supabase's default privileges grant `select` on every new view to `anon`.** RLS
still stops the read, but a `security_invoker` view then makes `anon` evaluate
`is_allowed()`, which it cannot execute — so the endpoint answers with the name of
an internal function instead of an empty result. Revoke explicitly when adding a
view; `v_shortlist` needed it.

The scripts authenticate with the service-role key, read
from the environment and never committed:

```powershell
setx LOPPAN_SUPABASE_KEY "<service_role key from the dashboard>"
```

⚠️ **`setx` only affects processes started afterwards.** An editor or agent that
was already running keeps the old environment and will report the key as missing
even though it is set. Either restart it, or pull the value from the user
environment explicitly:

```powershell
$env:LOPPAN_SUPABASE_KEY = [System.Environment]::GetEnvironmentVariable('LOPPAN_SUPABASE_KEY','User')
python loppan/load_to_db.py
```

`cohort.py check` syncs automatically once the key is set, and always writes the
local JSONL first — so a missing or expired key can never cost a week of
observations.

The whole experiment in one query:

```sql
select * from public.v_cohort_summary;
```

## Dashboard

A read-only Lovable app at <https://loppan.lovable.app>
(editor: <https://lovable.dev/projects/413a5b63-ebb6-4f60-a90a-72244eeb39f2>).
Three screens: **Undervalued**, the cohort strata, and completed round trips. It
signs in against Supabase Auth, never writes, and reads nothing the daily jobs do
not already collect.

### Undervalued — the picture grid

~500 live items priced low against comparable live listings, from `v_shortlist`,
sortable on every price- and demand-driving column and filterable on the rest.

It is a **precomputed table, not a live query**, because ranking the shelf costs a
seq scan over 693k rows and 3.0 s — over the anon statement timeout. Stored, it
answers in 4.9 ms. It is filled by the **pool rotation**:

```bash
python loppan/sweep_pool.py     # one bucket; runs 12x a day from pool.yml
python loppan/pool_refresh.py   # price and liveness across the whole pool
```

⚠️ **`loppan/shortlist.py` no longer builds this, and running it destroys the pool.**
It predates the bucket rotation: `refresh_shortlist()` knows nothing about `bucket` and
clears `shortlist_daily` wholesale, so it wipes every bucketed row the sweeps built and
resets the rotation to bucket 0. It did exactly that on 2026-08-11 — 8 covered buckets
and ~78,000 rows replaced by 37,010 with no bucket at all.

It had been silently skipped since 08-10 because `analytics.py` always failed ahead of
it, which is the only reason two systems claiming the same table never collided; fixing
analytics un-skipped it and it ran. The step is now `if: false` in `track.yml`. This
paragraph replaces a line that said it "must stay there", which predated the pool.

**Unresolved: which of the two owns `shortlist_daily`.** The pool is the live design —
bucket-aware, twelve runs a day, `docs/analytics.md` §9. `shortlist.py` is the pre-pool
path. Re-enable it only once `refresh_shortlist()` is retired or taught about buckets.

**The 08-11 rows could never have healed on their own, and now they can (2026-08-13).**
`refresh_pool_bucket` cleaned up with `delete ... where bucket = p_bucket`, and a NULL
bucket never matches that — so the un-bucketed rows were invisible to every sweep's
cleanup. 28,364 were still being served on 08-13, **half of them in buckets that had
already been re-swept**: rankings frozen on 08-11 that nothing would ever replace or
remove, with prices kept fresh by `pool_refresh` so they looked current. They have been
deleted, and the cleanup is now `where bucket = p_bucket or bucket is null` — a row
without a bucket is not owned by the rotation, so the rotation reclaims it on the next
sweep. Any future writer that bypasses the bucket design gets undone within one pass
instead of leaving immortal rows behind.

⚠️ `not null` on `bucket` would be the stronger guard and **cannot be used**:
`pool_refresh.py` upserts a partial payload keyed on `item_id`, and PostgREST validates a
complete insert tuple before resolving the conflict (see the note on `db.update`), so it
would fail the whole daily price-and-liveness refresh.

**Two things it will mislead you about if you let it.** A large discount usually
means a mismatched peer group, so `peer_n` and the grouping are on every card. And
cheap is not the same as sells — several brands near the top sell **0.00%/day** —
so `brand_sell_pct_day` is there too. There is deliberately **no profit figure**:
`price_to_estimate` is null on all 671k live items since the v2 rehaul, so the old
`expected_profit` cannot be computed and must not be faked. See `docs/analytics.md` §8.
