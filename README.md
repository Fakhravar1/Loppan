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
| `docs/handover.md` | The full design document: ideas, evidence, economics, open questions |
| `docs/api-notes.md` | **Read before writing a query.** Which Parse queries work, which time out, and why |
| `loppan/sellpy.py` | Read-only client for Sellpy's public Parse backend |

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

1,300 items enrolled 2026-08-04 and followed weekly to their outcome. This is the
project's one live experiment, and it exists because **sell-through cannot be
recovered from history**: items that sell are removed from the search index, so
any backward-looking sample sees survivors and failures but never a success.

```bash
python loppan/cohort.py check      # weekly. takes a couple of minutes
```

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

⚠️ The cohort lives in `data/`, which is gitignored, so it exists on one machine
only. Move it to Postgres before it represents months of elapsed time.
