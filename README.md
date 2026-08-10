# Loppan

Research project: does it pay to buy underpriced second-hand items on Sellpy,
hold them, and relist them on Sellpy Circle at 80% seller share?

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
| `loppan/sellpy.py` | Read-only client for Sellpy's public Parse backend |
| `loppan/shortlist.py` | Builds the undervalued shortlist the dashboard grid reads, and fetches its pictures |

## The two things worth doing first

1. **Backtest on history.** Sellpy retains every price step of every item, with
   timestamps, readable long after the item sold. So a buying rule can be tested
   against months of real market behaviour without spending anything or waiting.
2. **Observation cohort.** Follow a stratified, systematically-selected set of
   live items to their outcomes — including the ones that quietly expire, which
   are the entire point.

Both are described in `docs/handover.md` §11–§12.

## Ground rules

- **Read-only.** Nothing here authenticates as a user or writes to Sellpy.
- **One request per second.** Enforced in `sellpy.py`. The risk that matters is
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
answers in 4.9 ms. Rebuild it with:

```bash
python loppan/shortlist.py
```

Runs automatically as the last step of `track.yml`, after `analytics.py`, and must
stay there — it reads `peer_prices` and `brand_daily`, which those steps rebuild.

**Two things it will mislead you about if you let it.** A large discount usually
means a mismatched peer group, so `peer_n` and the grouping are on every card. And
cheap is not the same as sells — several brands near the top sell **0.00%/day** —
so `brand_sell_pct_day` is there too. There is deliberately **no profit figure**:
`price_to_estimate` is null on all 671k live items since the v2 rehaul, so the old
`expected_profit` cannot be computed and must not be faked. See `docs/analytics.md` §8.
