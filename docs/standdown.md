# The 2026-08-19 stand-down

Loppan is paused. This file records what was stopped, what was reclaimed, what was
removed from `qvitta-pi`, and what it takes to bring each part back. It also corrects
one diagnosis that the other docs still state as fact.

Nothing here is a code change. The pause is entirely in workflow state, database
contents and the Pi's systemd units, so none of it is visible in a diff — which is
precisely why it needs writing down.

---

## 1. What was stopped

All four scheduled workflows are `disabled_manually` as of 2026-08-19 ~06:30 UTC:

| workflow | id |
| --- | --- |
| `cohort-check.yml` | 327347062 |
| `enrol.yml` | 329421368 |
| `pool.yml` | 331211009 |
| `track.yml` | 329252681 |

Nothing was in flight when they were disabled, so no pass was cut in half. To restore:

```
gh workflow enable pool.yml --repo Fakhravar1/Loppan
```

⚠️ **Read §4 before re-enabling.** The `route` cost gate is now wrong in a way that
will silently run the sweep at a third of its intended cadence.

---

## 2. What was reclaimed, and why it was there

The database was at **492 MB against Supabase's 500 MB free-tier ceiling** — 98.4%.
It is now **385 MB** (77%), with 115 MB of headroom.

| action | freed |
| --- | --- |
| `release_peer_live()` — released 588,556 rows | 60 MB |
| `REINDEX items` (64 → 32 MB of indexes) | 32 MB |
| `REINDEX peer_prices` (38 → 30 MB) | 8 MB |
| `REINDEX shortlist_daily` (15 → 8 MB) | 7 MB |

**The 60 MB was the interesting part.** `release_peer_live()` shipped in 38d59da and
was deployed to the database, but had **never once been called** — `peer_live` was
sitting at its full 588,556 rows. `docs/analytics.md` §1 already said the table is
"rebuilt from `items` every pass and worthless after one"; the function to act on that
existed; the chain simply never reached step 6. A function that is deployed but never
invoked looks identical to a fixed problem from the outside, and this one held 60 MB
for two days out of every two days for as long as it existed.

`peer_prices` and `shortlist_daily` were left populated. They are output, not staging —
`shortlist_daily` is the undervalued grid itself (§8) and rebuilding either needs a full
pass, which is exactly what is paused.

**`VACUUM FULL` was deliberately not used.** It rewrites a table into a new file before
dropping the old one, so `VACUUM FULL public.items` would have needed ~213 MB of
transient space on a database that had 8 MB of headroom at the time. `REINDEX` rebuilds
one index at a time and peaks at the size of the largest single index (43 MB for
`items_pkey`), which fit. Reach for `REINDEX` first whenever the ceiling is the reason
you are reclaiming — `VACUUM FULL` is the tool that needs room you do not have.

---

## 3. `qvitta-pi` no longer runs Loppan

**The repo is public now, so hosted Actions minutes are free and unlimited.** The Pi
existed to avoid billing on a private repo. That reason is gone, and the Pi was actively
harmful: measured on 2026-08-19 from the two runners' cgroups,

| | throttle events | file refaults | cumulative stall |
| --- | --- | --- | --- |
| **Loppan** | 77,333,572 | 369,273,104 | **9.8 hours** |
| Qvitta | 230,352 | 40,874,349 | 21 minutes |

Loppan was ~28× Qvitta's memory stall on a shared 899 MiB box. The two cgroup caps also
oversubscribed it — `MemoryMax` 400 MB (Loppan) + 600 MB (Qvitta) = 1000 MB of promises
against 899 MiB of RAM.

Removed from the Pi: the runner service and its drop-in directory, `loppan-netns.service`,
`loppan-pin-runtime.service` (which pinned the .NET runtime in RAM permanently),
`loppan-trail.service` + `.timer` (which sampled twice a minute, every minute), the
`run-netns-loppan.mount`, `/etc/wireguard/loppan.conf`, `/etc/netns/loppan`, the four
`/usr/local/sbin/loppan-*` helpers, and `/opt/actions-runner-loppan` (801 MB).

GitHub runner id 21 was deregistered. `Fakhravar1/Loppan` now has **zero** self-hosted
runners.

Result: Pi disk 6.0 G → 5.2 G, swap in use 244 MiB → 158 MiB. Qvitta's runner was
untouched and unaffected — verified by a `dbt run` completing in 3m46s immediately after.

**The VPN keys were not destroyed.** `/home/arian/loppan-pi-config-backup-20260819.tar.gz`
on the Pi holds all 15 files, including `loppan.conf`. The WireGuard private key in it
cannot be regenerated, so that tarball is the only copy — move it somewhere durable if
the Pi is ever reflashed.

### Restoring the Pi path

`deploy/pi-vpn/install` is idempotent and rebuilds the namespace plumbing, but it does
not re-register a runner and does not contain the WireGuard key. A full restore is:
untar the backup, `systemctl daemon-reload`, re-register a runner into
`/opt/actions-runner-loppan`, re-enable the five units. Given §4, prefer not to.

---

## 4. ⚠️ The cost gate in `pool.yml` is now wrong

`route` throttles hosted sweeps from 12/day to the four 6-hourly slots, on this
reasoning, quoted from the workflow:

> free pool, shared across every repo on the account = 2,000 min/month

**That premise is false as of this repo going public.** Public repositories get unlimited
free GitHub-hosted minutes; there is no shared 2,000-minute pool to protect. Left in
place, the gate now costs two thirds of the rotation's cadence to defend a budget that
does not exist.

It fails safe rather than dangerous — you get a slower rotation, not a broken one — which
is why it was left alone rather than edited during a stand-down. But it is the first
thing to fix when Loppan resumes, along with the `qvitta-pi` routing in `pool.yml` and
`track.yml`, which now always takes the "offline" branch.

`docs/pi-runner.md` and `docs/pi-vpn.md` describe a setup that no longer exists on the
box. They are kept as the record of how it was built and what it cost.

---

## 5. Correction: the sibling's storage diagnosis does not transfer

`claim-my-train`'s `CLAUDE.md` documents a "500 MB free-tier ceiling" incident whose
stated swing factor is index bloat between REINDEXes. Loppan inherited that framing
informally. Investigating the sibling's 2026-08-16→18 recurrence showed the framing is a
**proxy, not the mechanism**, and the distinction changes which lever works:

- The binding constraint there is the **hot working set (313 MB) against
  `shared_buffers` (224 MB) — 140%**, not the disk quota.
- Measured directly: the same aggregate over `int_stop_events` ran in **19,877 ms** cold
  and **4,969 ms** warm, on an identical plan with the same buffer counts. That is ~15 s
  of pure disk stall on ~700 pages, about 21 ms per page read.
- `REINDEX` helps because it drags the working set back toward the buffer pool — not
  because it frees quota. That is why the breach returned 14 h after a REINDEX rather
  than the 4 days the playbook predicted.

Four plausible causes were tested and eliminated: scheduling collisions (the failure
*rate* is flat at 9–15% regardless of how many jobs fire), `pg_net` (plain-SQL prunes
failed *more*), a stale replication slot pinning WAL (none exist), and CPU (5M rows in
0.94 s).

**Why this matters for Loppan:** Loppan's own database showed *zero* errors during its
sweep failures, so it was never the same problem. Loppan's failures were the Pi's runner
dying mid-job — steps with a `null` conclusion and no uploaded logs — and one 83-minute
hang on bucket 7 that the *same* bucket 7 completed in 5m01s on another pass. Do not
reach for the storage playbook when a Loppan pass fails; check the runner first. That is
moot while §3 stands, which is part of the point.
