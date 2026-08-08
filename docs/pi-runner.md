# The Raspberry Pi runner — what happened, and the conditions for retrying

Written 2026-08-08, after a failed attempt to move `track` onto self-hosted
hardware. Revised the same day, once the tracker's memory was actually measured —
which changed the diagnosis and cleared two of the three conditions below. Read
this before touching `runs-on:` in any workflow.

## What the Pi is

`qvitta-pi` — a Raspberry Pi 4 with **1 GB of RAM**, Ubuntu 24.04 arm64, on the
owner's home LAN at 192.168.1.199 (user `arian`, SSH key auth). It belongs to the
sibling project (`C:\Users\arian\trafiklab`, the Qvitta train-delay app), where it
runs a `dbt build` every 15 minutes as a GitHub Actions self-hosted runner.

The attraction is simple: **self-hosted Actions runs are not billed.** The sibling
project cut $10.56/month of hosted minutes to zero by moving there.

A second runner instance is installed for this repo (personal accounts register
runners per-repo, so one runner cannot serve both):

| Path | Repo | systemd service |
|---|---|---|
| `/opt/actions-runner` | `Fakhravar1/claim-my-train` | `actions.runner.Fakhravar1-claim-my-train.qvitta-pi.service` |
| `/opt/actions-runner-loppan` | `Fakhravar1/Loppan` | `actions.runner.Fakhravar1-Loppan.qvitta-pi.service` |

Both are enabled and start on boot. The Loppan one is **online and idle** — it
stays installed, costs ~130 MB, and is ready once the remaining condition below is
met.

## What the saving actually is

Measured 2026-08-08, from real run durations rather than estimates:

| job | per run | runs/month | minutes |
|---|---|---|---|
| `track` | 26.8 min | ~15 | ~400 |
| `cohort check` | 1.5 min | ~30 | ~45 |
| `enrol` | a few min | ~15 | ~45 |
| | | **total** | **~490** |

An earlier draft of this document put `track` alone at ~990 min/month, from a
~66 min estimate for a full pass. The real figure is 26.8 min. That ~66 min looks
like it came from the old `catalogue sweep`, a different and now-deleted workflow
that genuinely did run 20–46 min.

**Judge that ~490 against the account, not against this repo.** The 2,000 free
minutes are a personal-account pool shared by every repo, and several projects
draw on it — some spending it directly, some holding it in reserve as a hosted
fallback. Loppan's ~490 is ~490 the sibling cannot use. Against Loppan's budget
alone the Pi would be optional; against the pool it is worth having, which is why
the plan is to go through with it.

## What happened on 2026-08-07 (the part that matters)

`track` was pointed at the Pi and dispatched. **Fourteen minutes in, the box
livelocked in swap thrash.** Memory exhaustion starved sshd, cron and both runners
— the machine could not be reached to kill the job. It went fully unresponsive
overnight and needed a power cycle the next morning. (The filesystem survived; the
SD card had already died once, on Jul 31, for unrelated reasons.)

Reverted the same night in commit `6e45bf2`.

Collateral: the sibling project's dbt degraded to its hourly hosted fallback for
~12 hours. That is its designed failure mode, not a Loppan problem — but it is the
reason this is not a free experiment to repeat casually. **The Pi is shared
production infrastructure for another project.**

## What the measurement found, and why the first diagnosis was wrong

The revert blamed a 1 GB box for not fitting a working set that "fits GitHub's
7 GB hosted runners". That was wrong in the direction that mattered. A full pass
peaked at **~7.3 GB**, and this repo is private, so `ubuntu-latest` is the 2-core
**7 GB** runner. The margin was negative on hosted too. Nobody knew because
nothing ever printed the number, and `track` had never once completed a run — the
Pi dispatch was the only run in its history.

Two causes, both unbounded in the size of the enrolled sample:

- `live_items` read all 668,961 unresolved rows before the first Algolia call —
  288 MB at 452 B/row, resident for the whole pass, growing every time `enrol`
  adds items.
- `get_objects_parallel` submitted every chunk up front and kept the futures list.
  `as_completed` drops its own references but cannot drop the caller's, and a
  finished `Future` holds its result — so that one list pinned every record the
  pass ever fetched, ~10.7 KB an item with no ceiling. Isolated from the network,
  keeping the list costs **26x** what dropping each future does.

Fixed in `fa70afa`: the pass runs in pages of 20k live rows, and Algolia
submission is windowed with each future released as it is consumed.

| | before | after |
|---|---|---|
| peak RSS | ~7.3 GB | **84 MB** (86,208 KB) |
| growth | linear in sample size | flat |

The 7.3 GB is extrapolated from a measured linear slope (10.90 KB/item at a 20k
sample, 10.71 at 60k); the as-written pass was never run at full scale on purpose,
since reproducing a 7 GB peak on a 7.6 GB workstation reproduces the Pi's failure.
The 84 MB is directly measured by `/usr/bin/time -v` on a real full pass over all
668,961 items, run `31252756280`, which also became the first successful `track`
run in the project's history.

`track` now runs under `time -v` permanently, so **Maximum resident set size** is
in the log of every pass. If that number starts climbing, something has begun
accumulating across pages again.

One more number that matters for the Pi: the pass used **74 s of CPU across 25
minutes of wall clock — 4.9%.** It is almost pure network wait. The Pi's weaker
processor is therefore not a constraint, and a pass there should take roughly as
long as it does on hosted.

## Conditions for retrying

**1. Measure `track.py`'s peak RSS. — DONE.** ~7.3 GB, as above.

**2. Bound it if above ~400 MB. — DONE.** Paged and windowed in `fa70afa`;
84 MB and flat. Worth doing on its own merits regardless of the Pi, and it was:
the hosted runs were headed for the same wall.

**3. Cap the runner service and prove the job under the cap. — DONE.** Note the
correction that drove the sizing: **a memory cap alone would not have saved the
box.** The Pi died of swap thrash, not OOM, and swap here is *zram* — compressed
pages held in the same RAM the kernel is trying to free, so reclaim under pressure
burns CPU and relieves nothing. That is the livelock. The line that converts "the
Pi died" into "the job failed" is `MemorySwapMax=0`.

Applied as a systemd drop-in at
`/etc/systemd/system/actions.runner.Fakhravar1-Loppan.qvitta-pi.service.d/memory.conf`:

```ini
[Service]
MemoryAccounting=yes
MemoryHigh=240M
MemoryMax=300M
MemorySwapMax=0
```

Sized from measurement: ~66 MB runner listener (idle — the earlier ~130 MB figure
was high) + 84 MB job + headroom. `MemoryHigh` throttles and reclaims first,
`MemoryMax` hard-kills, swap is denied so neither can thrash. Ubuntu 24.04 is
cgroup v2 and the kernel confirms all three:

```
systemctl show <service> -p MemoryHigh -p MemoryMax -p MemorySwapMax
cat /sys/fs/cgroup$(systemctl show <service> -p ControlGroup --value)/memory.max
```

**Proved before being trusted.** A 10 MB-at-a-time balloon in a transient scope
carrying the same limits was killed at 306 MB anon-RSS in **one second**, with
`constraint=CONSTRAINT_MEMCG` and `oom_memcg` naming the balloon's own cgroup —
so the kill was scoped, not global. Swap was untouched, load average did not move,
and both runners stayed active. That is the whole point: the box no longer notices.

**4. Add the fallback router — DONE in the workflow, one manual step outstanding.**
The `route` job in `track.yml` probes runner status and picks `runs-on`. It needs a
secret to do so, and **until that secret exists the router warns and routes hosted**
— so the Pi will not actually be used:

1. Create a fine-grained PAT scoped to `Fakhravar1/Loppan` with
   **Repository permissions → Administration: Read-only**. This is the only
   permission it needs. `GITHUB_TOKEN` cannot be granted it, which is why a
   separate token is required at all.
2. `gh secret set RUNNER_STATUS_TOKEN --repo Fakhravar1/Loppan`

Until then every scheduled pass runs hosted, with a warning annotation saying why.
That is the safe direction to fail, but it is not free.

## Proven on the Pi, 2026-08-08

A full pass, dispatched with `runner=qvitta-pi`, run `31254519767`:

| | hosted | qvitta-pi |
|---|---|---|
| wall clock (`track.py`) | 25:06 | **7:46** |
| peak RSS | 84 MB | **57 MB** |
| cgroup OOM kills | — | **0** |
| swap consumed | — | **0** |

The Pi was *faster*, which is not the paradox it looks like: the pass is ~5% CPU
and the rest is waiting on Algolia, where a home connection beats a hosted runner's
path to the CDN. The two runs are not strictly comparable — the hosted one wrote
184,218 changed rows against the Pi's 11,919, having been the first pass in weeks —
but the fetch itself was quicker from home.

The cgroup recorded 355 `high` events (reclaim at the throttle point) and **zero**
`max` or `oom_kill` events. The reclaimed memory is page cache from checkout, which
is exactly what `MemoryHigh` is for: throttle on cheap memory before killing on
expensive memory.

**Still worth doing: cap the sibling's runner too.** Right now dbt has exactly the
same power to take the box down that `track` had, and Loppan is what gets starved
next time.

## Things that will bite you if you don't know them

- **A job sent to an offline self-hosted runner does not fail — it queues**,
  silently, for up to 24 h, and is then cancelled. `timeout-minutes` does not help:
  it counts execution time, and a queued job has none. This is why the `route` job
  exists. If you ever bypass it by hardcoding `runs-on: qvitta-pi`, you are back to
  a dead Pi meaning a silently skipped pass with no alert.
- **`enrol` and `cohort-check` stay hosted, deliberately.** They are ~90 min/month
  between them and moving them adds Sellpy-from-home-IP exposure for little saving.
- **The crawl would originate from a home residential IP** — the same household as
  the owner's Sellpy account, which the README's ground rules care about ("the risk
  that matters is the account, not the scraper"). Accepted as a trade for a
  read-only 1 req/s crawl during a measurement-only phase. A NordVPN tunnel on the
  Pi was considered and rejected: it inserts a new failure domain into the path
  between the sibling project's pipeline and its database, and VPN exit IPs are
  *more* likely to be anti-bot flagged than either a home or Azure IP. If Loppan
  ever becomes a business, the clean answer is a cheap EU VPS with a properly
  separated identity, not a VPN on this box.
- **Monitoring exists and works.** Two Healthchecks.io dead-man checks ping every
  5 min from the Pi's crontab, each gated on one runner service being active, so
  an alert email names which runner died. Verified in the real incident above:
  detected within 15 min, emails delivered. If you get a
  `qvitta-pi-loppan-runner is DOWN` email, the Pi or its Loppan runner is gone.
- **The pass resumes after a kill.** `track.py` checkpoints the last completed page
  to `track_progress`, and that row exists *only* while a pass is in flight — so a
  row left behind is itself the signal that the previous pass died. One caveat is
  logged rather than hidden: disappearances found before an interruption are not
  adjudicated on the resumed run, and stay live until the next pass.

## The honest summary

The Pi was a bad fit for `track` as written, and the reason turned out to be a
Loppan code defect rather than a hardware limit: an unbounded working set that
happened to be over the hosted ceiling too. That is fixed — 84 MB, flat as the
sample grows, and now printed on every run.

The rest was about making sure that when something on this box misbehaves it fails
as a job instead of as a machine. That is now true and demonstrated: the cgroup is
capped, swap is denied to it, an overrun is a one-second scoped OOM kill, a killed
pass resumes from its checkpoint, and a dead Pi routes to hosted instead of queueing
into silence. A full pass has run there end to end — 7:46, 57 MB, no kills.

The saving is smaller than the first draft of this document claimed (~400 min/month,
not ~990) but it is drawn from an account-wide pool that several projects share,
which is what makes it worth claiming.

One manual step stands between this and the Pi actually being used: creating
`RUNNER_STATUS_TOKEN`, without which the router cannot see the Pi and routes hosted
every time. Until that exists, everything here is built and proven but idle.
