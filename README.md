# Loppan

Research project: does it pay to buy underpriced second-hand clothes on the marketplace,
hold them, and resell them (Circle, Vinted, Plick)?

**Status: measurement only. No buying logic, no automation of purchases.** The number
that decides whether this is a business, the share of items that ever sell, is now
measured daily across the whole in-scope market (about 2.2M live listings at or above
150 kr from the kosher brand list).

## How it runs

Once a day, `.github/workflows/bq-daily.yml` runs `deploy/bigquery/daily.sh`:

1. **Track** every live item by id, and **find** everything listed since the last run.
2. **Load** the rows into BigQuery staging and **merge** them into `loppan.items`
   (one row per item, price and favourite history in arrays).
3. **Adjudicate** items that vanished (sold, expired or unknown), behind a 99.5%
   completeness gate, so a partial fetch can never record false sales.
4. **Model** a seasonally adjusted sold-price reference and **export** the shortlist to
   Supabase, where the dashboard reads it.
5. Write the day's **progress** row, print the `PROGRESS` block, stamp `completed_at`.

`bq-health.yml` checks every day that a run completed, passed the gate, adjudicated
something and stayed under the cost line; a failed check emails the owner. GitHub
starts scheduled runs late (5-9 h so far), so a day's run often lands in the afternoon.

| Path | What it is |
|---|---|
| `docs/bigquery.md` | **Start here.** The design, every decision and why, cost, the migration record |
| `docs/overview.md` | The idea, the constraints, the theories and their standing |
| `docs/api-notes.md` | What the marketplace's search index and backend will and won't do |
| `docs/schema.md` | Column semantics (written for the Supabase schema; the meanings carry over) |
| `deploy/bigquery/` | `schema.sql`, the merges, the model, `daily.sh`, `health.sql`, `test.sh` |
| `loppan/bq_fetch.py` | The fetcher: census, track, new, adjudicate, origins, brands, validate |
| `loppan/bq_export.py` | The shortlist export to Supabase |

**Check a run:** `gh run view <id> --log` and look for the `PROGRESS` block.
**Dashboard:** <https://loppan.lovable.app>, sign-in and allowlist only.

## Ground rules

- **Read-only.** Nothing here authenticates as a user or writes to the marketplace.
- **Polite.** The backend is asked strictly serially at one request a second.
- **Never name the marketplace** in anything committed. The repository is public.
- **Never submit fabricated data anywhere**, and never fake a profit figure: profit
  stays empty until fees and shipping are known.

## History

The first version (Supabase, a 1,300-item forward cohort, a Raspberry Pi runner) was
stood down on 2026-08-19 and replaced by the BigQuery pipeline on 2026-10-02. Its code,
workflows and notes are preserved at the git tag `v1-final`. `docs/handover.md` and
`docs/analytics.md` describe that version.
