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

> ⚠️ **That last sentence was wrong, and it cost the box a second livelock on
> 2026-08-10.** `MemorySwapMax=0` does not convert a runaway into a clean kill. It
> removes the only *cheap* thing reclaim can do, so all the pressure lands on page
> cache instead — which is evicted and immediately faulted back from the SD card,
> forever, without ever reaching `MemoryMax`. No OOM kill, no failed job, just a
> machine that stops making progress. **See "The second livelock" below**, which
> also carries the corrected sizing. The values in the block that follows are
> superseded.

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

## The second livelock — 2026-08-10/11

The box went down again on 2026-08-10, was power-cycled the next morning, and was
livelocking again by 08:16. Different cause from 2026-08-07, and the more instructive
one, because everything above was built specifically to prevent it and did not.

### What set it off

Two things landed on 2026-08-10 that, together, put a workload on the Pi that had
never run there:

1. `RUNNER_STATUS_TOKEN` was created, so the `route` job stopped defaulting to hosted
   and actually began sending jobs to `qvitta-pi` — the "one manual step outstanding"
   above.
2. `pool.yml` / `sweep_pool.py` was added. It is a **different and much larger
   workload than `track.py`**, and the cgroup cap it inherited had been sized against
   `track.py` alone.

Every job that routed to the Pi from that point failed. Everything still hosted —
`enrol`, `cohort check` — kept succeeding, which is the signature worth remembering:
**if the hosted jobs are green and only the self-hosted ones fail, suspect the box,
not the code.**

### Why the cap was too small

It was sized as "~66 MB runner listener (idle) + 84 MB job + headroom". Both terms
were wrong:

| In the cgroup while a job runs | RSS |
|---|---|
| `Runner.Listener` | 71 MB |
| `Runner.Worker` — **exists only while a job runs, and was never counted** | 57 MB |
| `python3 sweep_pool.py` | 120 MB and climbing |
| bash + node | ~9 MB |
| **total** | **~257 MB against a 240 MB `MemoryHigh`** |

The listener was measured *idle*, which is precisely when the worker does not exist.
Any sizing that counts only the idle listener is short by ~57 MB on every real run.

### The failure mode: refault livelock, not OOM

Measured on the live cgroup, 2026-08-11:

| | Loppan runner | Qvitta runner (healthy, for contrast) |
|---|---|---|
| `memory.events` `high` | **8,270,730** | 10,426 |
| `memory.pressure` full avg10 | **95.6 %** | 0.00 % |
| `workingset_refault_file` | **38,106,577** | 887,583 |
| `pgmajfault` | **1,225,952** | 103,804 |
| `oom_kill` | **0** | 0 |

Box-wide: load average **60**, `/proc/pressure/io some` 98 %, and `Dirty` at 36 kB —
near-zero dirty pages with saturated I/O is the fingerprint. Nothing was being
*written*; the same pages were being read back over and over.

The mechanism, and the thing the earlier reasoning missed:

- `MemoryHigh` (240 M) throttles and reclaims **before** `MemoryMax` (300 M) is ever
  reached, so the hard limit never fires and **nothing is ever killed**.
- `MemorySwapMax=0` means anonymous pages cannot be reclaimed *at all*.
- So every byte of reclaim must come from file-backed pages — the executables, the
  shared libraries, the Python stdlib — which are needed again immediately and fault
  straight back in off the SD card.
- Reclaim therefore always "succeeds", the cgroup sits permanently at its throttle
  point, and the job makes no progress. The runner took **8–9 minutes to write a
  single constant log line**; a job GitHub had already failed at 11 minutes was still
  burning the box three hours later.

**`MemorySwapMax=0` is not a safety belt. It is the thing that removed the cheap
reclaim option and forced the expensive one.** The sibling unit, with a *bounded*
192 M, never had this problem.

Note what did work: the blast radius stayed inside the cgroup. Qvitta held at 0.00 %
memory pressure and its runner never missed a beat. The isolation design is sound —
only the sizing and the swap setting were wrong.

### The fix

**Bound the job, then size the cap to it** — the same order `fa70afa` used for
`track.py`:

- `sweep_pool.py` no longer materialises a brand's hits. `_fetch_shape` returned every
  raw Algolia hit for a brand in one list — ~10.7 KB a hit, and Zara alone has ~21,000
  target-size items, so ~225 MB before a single row was written. It is now
  `_walk_shape`, which hands each *complete* price slice to a callback that projects it
  into a ~1 KB staging row and drops the hit. The invariant that a **capped** slice is
  discarded rather than emitted is preserved — that is what keeps a biased 2,000 out of
  the peer groups.

  **Measured on the first full pass after the fix (run `31482344537`, bucket 3, 1,547
  brands, 67,872 items staged): peak RSS 270,236 KB — 264 MB — in 16:25.** An earlier
  draft of this section guessed ~66 MB from the steady state and was wrong by 4x, which
  is precisely why `time -v` is now mandatory on this job rather than an estimate in a
  document.

### The remaining 264 MB is NOT per-brand, and is still unexplained

Worth writing down because two plausible explanations have already been tested and
killed, and the next person will otherwise reach for the same two.

Peak RSS scales with the **total items staged across the whole pass**, not with the
largest brand in it:

| bucket | items staged | peak RSS | KB per staged item |
|---|---|---|---|
| 5 | 40,139 | 145 MB | 3.7 |
| 4 | 59,774 | 253 MB | 4.3 |
| 3 | 66,003 | 264 MB | 4.1 |

**Eliminated — chunking the per-brand upsert.** The obvious read of the sawtooth was
that a brand's projected rows were held until its upsert, so they were changed to flush
every `db.BATCH` rows (free: `db.upsert` already splits at that size, so the same 42 HTTP
requests go out either way). Bucket 3 re-run under identical conditions: **270,236 KB
before, 270,708 KB after — 0.2%.** The bound is real and worth keeping, because a single
21,000-item brand genuinely would have held 21,000 rows, but it is not the dominant term.

**Eliminated — allocator fragmentation.** Churning 66,000 transient hit-sized dicts
500 at a time on this box, retaining only the ids, peaks at **21 MB**. CPython returns
the arenas. This is not obmalloc ratcheting.

**Also checked and clear:** `algolia.search` memoises nothing, and `enrol`'s module-level
`_lookup` / `_brands` / `_masks` caches are bounded by distinct *values*, not item count.

So something retains ~4 KB per staged item for the length of a pass and has not been
found yet. **The next step is measurement, not another guess** — run the pass with
`tracemalloc` snapshotting the top allocations, rather than reasoning about the code.
Until then the number is known, bounded by the cgroup, and survivable; it is simply not
understood.
- `pool.yml` runs under `/usr/bin/time -v` permanently, as `track.yml` already did.
  This job was unmeasured, which is the only reason it grew past the cap unnoticed.

Corrected drop-in at
`/etc/systemd/system/actions.runner.Fakhravar1-Loppan.qvitta-pi.service.d/memory.conf`:

```ini
[Service]
MemoryAccounting=yes
MemoryHigh=300M
MemoryMax=400M
MemorySwapMax=128M
```

- **300 M `MemoryHigh`** — comfortably above the ~200 MB steady state (71 MB listener +
  57 MB worker + ~68 MB job), so the cgroup does not live at its throttle point.
  Sitting *at* `MemoryHigh` is the failure, not a safe steady state.
- **400 M `MemoryMax`** — the runaway stopper, unchanged in purpose. Note it is *not*
  comfortably above the worst case: 264 MB of job on top of 128 MB of runner is ~392 MB,
  inside 400 M but barely. Four consecutive full passes rode it out — `high` climbing
  only 536 → 1,801 across all of them, `max` 0, `oom_kill` 0 — with reclaim and ~52–72 MB
  of zram absorbing the peak, which is exactly the job those two settings exist to do.

  **This is the thinnest margin on the box, and it is load-bearing.** Peak RSS grows
  with the number of items a bucket stages (see below), so a bucket larger than 3's
  66,000 will push it. The honest position: the ceiling is holding, the growth term is
  not yet understood, and the answer if it bites is to find that term — not to raise
  the ceiling into the space Qvitta needs.
- **128 M `MemorySwapMax`, not 0** — the correction. zram compresses ~4.5:1 here, so
  this costs ~28 MB of real RAM and gives reclaim somewhere cheap to go. Bounded, not
  unbounded: unbounded zram reclaim is what caused the *first* livelock.

`MemoryHigh` totals 300 + 450 = 750 MB of an 899 MB box, leaving ~150 MB for the OS,
and both workloads sit well under their throttle points in normal use.

### If you are reading this because the box is down again

```bash
# Is it thrashing rather than busy? Near-100% "full" with no dirty pages is the tell.
cat /proc/pressure/memory /proc/pressure/io; grep Dirty /proc/meminfo
cg=/sys/fs/cgroup$(systemctl show actions.runner.Fakhravar1-Loppan.qvitta-pi.service -p ControlGroup --value)
cat $cg/memory.events; grep -E "workingset_refault_file|pgmajfault" $cg/memory.stat
```

A large and *growing* `high` count with `oom_kill 0` means a refault livelock, not a
runaway. Stopping the runner service releases it — and because a stopped runner routes
the next pass hosted, that is a safe thing to do while you work out why.

⚠️ **Do not "clean up" with `systemctl revert`.** It removes *every* drop-in for the
unit, including this persistent `memory.conf`, leaving the runner uncapped — which is
the one state guaranteed to take the whole box down. Use
`systemctl set-property --runtime` for a temporary change and delete the runtime file
to undo it.

## The sibling's runner is capped too (2026-08-08)

Symmetry, not paranoia: `track` proved an unbounded job on this box takes the whole
machine with it, and Loppan now *depends* on the box. Nothing stopped dbt doing the
same thing in the other direction.

Measured before sizing, because dbt is a much larger workload than `track` and a
copied config would have killed healthy builds:

| | idle | during a build |
|---|---|---|
| anon | 20 MB | **206–221 MB** |
| memory.current | 334 MB | 449–495 MB |
| peak current since boot | | 629 MB (~11 builds) |

**The cap is sized against anon, not `memory.current`.** At idle the cgroup was
20 MB of anon against 295 MB of page cache — 88% reclaimable. Sizing to the 629 MB
figure would have set a ceiling three times larger than the workload needs, which
protects nothing.

`/etc/systemd/system/actions.runner.Fakhravar1-claim-my-train.qvitta-pi.service.d/memory.conf`:

```ini
[Service]
MemoryAccounting=yes
MemoryHigh=450M
MemoryMax=600M
MemorySwapMax=192M
```

Note `MemorySwapMax` is **bounded here, not 0 as on the Loppan unit**. The livelock
came from unbounded zram reclaim, not from swap existing; zram compresses ~4.6:1 on
this box, and letting the kernel page out genuinely cold pages is worth having when
RAM is this tight. Capping it stops a thrash spiral without forbidding normal
behaviour. (Observed swap during a build after the restart: 0 MB. The allowance is
headroom, not a requirement.)

Verified against a real build rather than assumed: peak anon 206 MB, `high` 104
(cache reclaim at the throttle point, which is the intent), **`max` 0 and
`oom_kill` 0**, service still active afterwards.

### The two ceilings deliberately oversubscribe the box

Loppan's 400M plus dbt's 600M is 1,000 MB on an 899 MB machine. That is intentional.
`MemoryMax` is a runaway-stopper, not an operating point. What governs steady state
is the `MemoryHigh` pair — 300M + 450M = 750 MB, leaving ~150 MB for the OS, which
does fit, and both workloads sit well below their throttle points in normal use.

⚠️ **"Below their throttle points" is load-bearing, not a nicety.** A cgroup parked
*at* `MemoryHigh` does not degrade gently — it reclaims continuously, and if it has no
swap it reclaims page cache it needs back immediately. That is the 2026-08-10 livelock.
Headroom under `MemoryHigh` is the thing being bought here; `MemoryMax` only catches
what headroom fails to.

If both ever hit their hard ceiling simultaneously, the global OOM killer takes a
process and a job dies. That is the outcome we want, and it is precisely what was
*not* possible before: with swap unbounded, the box livelocked instead of killing
anything.

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
as a job instead of as a machine. That is *mostly* true: the cgroup is capped, a
killed pass resumes from its checkpoint, and a dead Pi routes to hosted instead of
queueing into silence. A full pass has run there end to end — 7:46, 57 MB, no kills.

But it was not as true as this document claimed, and 2026-08-10 proved it. "Swap is
denied to it" was listed here as a safety property when it was the opposite: denying
swap is what turned an over-cap job into an unkillable refault livelock instead of a
clean kill. A cap protects the box only when the job is bounded *and* reclaim has
somewhere cheap to go *and* the steady state sits below the throttle point. The
standing lesson is the same one `track.py` taught and this job had to learn again:
**measure the job before you size a cap around it, and keep measuring it in the log
of every run.**

The saving is smaller than the first draft of this document claimed (~400 min/month,
not ~990) but it is drawn from an account-wide pool that several projects share,
which is what makes it worth claiming.

One manual step stands between this and the Pi actually being used: creating
`RUNNER_STATUS_TOKEN`, without which the router cannot see the Pi and routes hosted
every time. Until that exists, everything here is built and proven but idle.
