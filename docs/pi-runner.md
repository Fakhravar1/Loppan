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

**Eliminated — "it scales with items staged", which is what the table above suggests.**
It does not. Bucket 6 of 24 staged 21,903 items and peaked at **234 MB**; bucket 3 of 12
staged 66,003 and peaked at 264 MB. A third of the items, essentially the same peak. The
linear fit over three points was a coincidence, and the 12 → 24 bucket split — made on
the strength of it — did **not** halve the peak. (It is still worth having for rotation
speed and shorter runs; it just did not buy what it was sold on.)

### Where it actually is: not Python

Settled by tracing the **real** `sweep()` loop off the Pi — `algolia` and `db` stubbed,
`enrol.row_of`, `search.image_paths` and the loop itself untouched — over 80,000 items
including one 20,000-item brand:

```
traced PEAK 16.3 MB     top retained site at end: 0.01 MB
```

**16 MB, retaining nothing, against ~250 MB RSS.** `tracemalloc` only sees Python's
allocator, so that gap is the finding: the memory is C-level and no amount of reading
`sweep_pool.py` would ever have located it. It also explains why every Python-level fix
failed, and why the fragmentation test came back clean — that tested obmalloc, and this
is glibc.

The only C allocator in the hot path is OpenSSL. `algolia._post` calls
`urllib.request.urlopen` per request, so **every Algolia request builds a fresh TLS
connection**, and a bucket makes thousands of them across brands, size shapes and the
price-split recursion. Those buffers come from glibc malloc, which holds freed heap
rather than returning it once fragmented. It tracks *requests*, not items — which is
exactly why the per-item fit kept nearly working and then broke.

Both fixes were tried on 2026-08-11. Results, because neither went as predicted:

| | peak RSS | wall clock |
|---|---|---|
| bucket 6, before | 234 MB (21,903 items) | 7:24 |
| bucket 7, before | 264 MB (32,276 items) | 8:58 |
| **bucket 8, with connection reuse** | **210 MB (25,217 items)** | **4:47** |

`MALLOC_ARENA_MAX=2` did nothing at all, and the test was invalid anyway: this path is
single-threaded, so glibc uses the main arena and an arena cap could never apply. It has
been removed rather than left as a plausible-looking knob.

**Connection reuse bought a large speed win and a small memory one.** ~40% faster is the
TLS handshake per request disappearing, and that is worth having on its own. But peak
only fell to 210 MB on a bucket *larger* than the one that cost 234 MB — call it 10–20%,
and the buckets are not the same size, so even that is soft.

So TLS churn was **a** contributor and not the main one. Roughly 200 MB is still
unaccounted for: not Python objects (16 MB traced, nothing retained), not obmalloc
fragmentation, not per-brand buffering, not items staged.

**Recommendation: stop here.** The term plateaus rather than growing — 765 brands and
1,547 brands both land near 264 MB — so it is not a time bomb that the pool will grow
into. It sits under a 400 MB ceiling that has never once fired across every pass on the
worst day this box has had. Four hypotheses have now been tested and killed at
meaningful cost, including two that took the runner down. The next person should pick
this up only with a reason better than tidiness.

⚠️ **Do not run `tracemalloc` on the Pi to check this.** Its bookkeeping does not fit
beside the job in a 300 MB cgroup: 25 frames gave 114,002 throttle events and load 27,
1 frame still pinned the cgroup at `MemoryHigh` with ~2M throttle events, and both runs
died and left an orphaned process behind. Trace it off the box, as above.
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

**Read the trail first.** It is the only thing on this box that records what happened,
and after three crashes that left nothing it is the reason a fourth should be
diagnosable. The last lines before the gap are the whole point:

```bash
# The run-up to the silence: where did memory go, and which cgroup was holding it?
sudo tail -40 /var/log/loppan-trail.tsv | column -t
# Uptime resets to a small number at the reboot, so the gap is easy to find:
sudo awk -F'\t' 'NR>1{print $1, $2, $3, $4}' /var/log/loppan-trail.tsv | tail -60
```

Then the live picture:

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

## The third crash — 2026-08-12 — and why there is now a trail

The box died again overnight and was power-cycled at 07:33 CEST. **The cause is not
known**, and that is the finding rather than a gap in this document.

> ⚠️ **This paragraph was superseded on 2026-08-13 and then un-superseded the same day.
> The cause of the third crash is still not known.** The retraction said the cause was
> the SD card and that "the SD card is healthy" below was false. Both of those were
> wrong, and the sentence they attacked was right: there is **not one SD I/O error in
> the retained log history before 2026-08-13** — `/var/log/syslog*` and `kern.log*` go
> back to 2026-02-10 and every one of the 350 errors falls on 08-13. The `/var/crash`
> dumps cited as proof are Python *exception* reports, not signal deaths. See "The fifth
> crash" for the evidence and for how the wrong retraction was built.

What was ruled out, all measured rather than assumed: the SD card is healthy and the
filesystem came up clean (no recovery, no orphan inodes), the SoC was at 64.7 °C with no
under-voltage or throttling in `dmesg`, and there was **no OOM kill and no kernel error
of any kind**. The last GitHub run before the silence — a pool sweep of bucket 3 that
started 03:00 UTC — has *no conclusion on its step*, which is the runner dying mid-job
rather than a job failing.

### Why the journal is no help, and will not be

The board has **no RTC**. `fixrtc` sets the clock from filesystem mtime at boot, so
entries written before NTP syncs land under a wildly wrong timestamp and then jump.
`journalctl --list-boots` reports the current boot as starting 2026-06-05, and `-b -1`
returns February. **Do not try to reconstruct a crash window from the journal on this
box** — three attempts across two days produced nothing but confusion.

### The trail

`/usr/local/sbin/loppan-trail` (source: `deploy/pi-trail/loppan-trail`), a systemd timer
writing one TSV line to `/var/log/loppan-trail.tsv` every 30 s:

| | |
|---|---|
| what | load, `MemAvailable`, swap, all three PSI figures, and `memory.current` / `memory.swap.current` / `high` for **both** runner cgroups, plus the largest process |
| why both cgroups | the open question is whether one workload runs away or the two together exceed 899 MB, and only a number for each tells them apart |
| durability | one append and an `fsync` per sample — a trail that buffers loses exactly the lines anyone will want |
| isolation | runs outside both runner cgroups, `Nice=-5`, realtime IO priority, so it keeps writing while they are the thing going wrong |
| cost | ~700 KB/day, truncated at 8 MB, against the 1–3 GB/day this box already writes |

### What it found immediately, and what it did not

Re-running the exact crash-time workload — bucket 3 — **succeeded**: 32,349 staged,
7,002 kept, 4:59. It then overlapped a real dbt build without difficulty. Worst combined
use was **567 MB of 899**, `MemAvailable` never fell below 298 MB, peak memory pressure
1.55 %, peak load 2.21.

So the crash **did not reproduce**, and the tempting theory — that Loppan's 300M and
Qvitta's 450M `MemoryHigh` sum to more than the box has — is *not* supported by that
data. It is recorded here as an untested hypothesis, not a diagnosis. Note also that
Loppan's spike and Qvitta's build never coincided in the observed window; the genuinely
bad case, ~299 MB against a ~495 MB build, has still never been seen.

**Cadence is a red herring.** dbt runs every 15 minutes and takes ~4, so a sweep of any
length overlaps it regardless of how often the sweep runs. Changing pool frequency does
not avoid contention, it only changes how often Loppan is the one contending.

## The fourth crash — 2026-08-13 01:20 UTC — SIGBUS off the SD path

> ⚠️ **This section was written as "the SD card, and it explains the third". It does not
> explain the third, and the media is not failing.** What is measured here — SIGBUS, the
> sector range, the `mmc0` lines — is real and stands. The inferences drawn from it were
> wrong, and are corrected in place below and in "The fifth crash". The original claims
> are kept, struck through in words rather than deleted, because this is the second time
> in three days that a plausible reading of real evidence sent the diagnosis sideways,
> and that pattern is the more useful thing to record.

### What happened

The 09:23 UTC pool sweep — **bucket 3 again** — died 12m13s in. Same signature as
08-12: no conclusion on the step, and `gh api .../jobs/<id>/logs` returns
`BlobNotFound`. GitHub reported the runner `offline` while `systemctl` reported it
`active (running)`.

That contradiction is the whole diagnosis. `runsvc.sh` is the unit's `MainPID` and it
survives; the `Runner.Listener` child was dying and being relaunched every ~75 s:

```
Runner listener exited with error code null
Runner listener exit with undefined return code, re-launch runner in 5 seconds.
```

`error code null` is a process killed by a signal, not one that exited. `apport` named
the signal: it was invoked as `-p<pid> -s7`, and **signal 7 is SIGBUS** — a page fault
on an mmap'd file whose backing blocks cannot be read.

`dmesg` had the media underneath it: **351** `mmc0: Got data interrupt ...` lines and
**109** `I/O error, dev mmcblk0` on a contiguous range, sectors 22902312–22902496 —
about 92 KB, inside `mmcblk0p2` (root), not the boot partition.

### ~~Why this explains 08-12, and 08-11~~ — it explains neither

`/var/crash` was read as the trail the journal could not give (no RTC — see above), and
every dump in it was attributed to the card:

| Crash dump | Local (CEST) | What it *actually* is | |
|---|---|---|---|
| `analytics.py` | Aug 11 07:55 | Python traceback — no `Signal:` field | not the card |
| `pool_refresh.py` | Aug 11 12:45 | `RuntimeError: HTTP 400 — 23502` not-null violation on `shortlist_daily` | not the card |
| `shortlist.py` | Aug 11 18:32 | `RuntimeError: HTTP 400 — 42P10` no unique constraint matching `ON CONFLICT` | not the card |
| `sweep_pool.py` | Aug 12 01:13 | `ssl.SSLEOFError` | not the card |
| `Runner.Listener` + `.Worker` | Aug 13 03:20 | **`Signal: 7` — SIGBUS** | this section |
| `udevadm` | Aug 13 03:24 | `Signal: 6` — SIGABRT | collateral |

⚠️ **The reasoning that produced the wrong answer, since it will be tempting again.** A
directory of crash dumps was found while looking for a hardware fault, the timestamps
spanned the bad days, and the file *names* were the scripts that had been failing — so
the pile was read as one story. Nobody opened them. An apport report for a Python
process carries a `Traceback:` and **no `Signal:` field at all**; only two of the seven
here were killed by a signal. One `grep` would have separated them:

```bash
for f in /var/crash/*.crash; do echo "$f: $(sudo grep -m1 '^Signal:' "$f" || echo 'no signal — exception report')"; done
```

⚠️ **So the earlier diagnoses were right and the retraction was wrong.** `ssl.SSLEOFError`
and the two PostgREST `400`s were correctly diagnosed when they happened, and the 08-13
entry retracted them for no reason. The 08-07 and 08-10 livelocks had genuine measured
memory evidence and were never in doubt. **Nothing before 2026-08-13 is explained by the
SD path** — there is no I/O error in the logs before that date.

⚠️ **`BlobNotFound` on a job's logs means the runner process died — not necessarily
hardware.** A job that fails uploads its log; a job whose *runner* dies cannot. It is
worth checking first, but it narrows the cause to "the runner went away", which includes
SIGBUS, OOM, a livelock and a power cut alike:

```bash
gh api repos/:owner/:repo/actions/jobs/<job-id>/logs
```

### What was done, and what it proves

Stop the unit (which ends the crash loop and stops `apport` writing dumps onto the
failing card), then reboot. Measured either side of it:

| | before | after |
|---|---|---|
| `mmc0` data-interrupt lines | 351 | **2** |
| `I/O error` lines | 109 | **0** |
| Unreadable files in `/opt/actions-runner-loppan` | Listener SIGBUS loop | **0 of ~2,000** |
| Memory pressure `some avg10` | 10.78 | **0.00** |
| Load (5 min) | 13.59 | 1.43 |

Every file in the runner tree was then read end to end — the exact data that was
faulting — provoking **zero** I/O errors. The unit was re-enabled and came back with
`√ Connected to GitHub`; GitHub reports `online`.

⚠️ **This does not prove the card is healthy, and it must not be read that way.** 109
hard read failures on a fixed sector range is not noise. A power cycle plausibly let the
card's controller remap the block, or the fault was controller/timing-level rather than
dying media — `Got data interrupt ... even though no data operation was in progress` is
a known Pi SDHCI quirk, and 2 of them is background where 351 was not. The honest state
is *currently error-free under a read of exactly what was failing*.

> ✅ **That caveat was the one thing in this section that held.** The repair did not last:
> the first job after it failed identically 35 minutes later. And the second guess in it —
> "controller/timing-level rather than dying media" — is what the evidence now supports.
> The "read every file end to end, zero errors" test passes *whenever the box is quiet*
> and proves nothing; it passed again on 08-13 after the fault had already returned. See
> "The fifth crash".

**The load during the incident was not Loppan's.** It was the sibling's `dbt build`, and
the Loppan cgroup sat at 29.5 MB against a 300 MB `MemoryHigh`. Do not reach for the
memory ceiling for this failure mode — it is the wrong instrument and it cost a day here.

### Watching for the return

```bash
sudo dmesg -T | grep -c "I/O error"
journalctl -u 'actions.runner.Fakhravar1-Loppan.qvitta-pi.service' | grep -c 'error code null'
```

⚠️ **The second command is not a health check and nearly hid the fifth crash.** It only
fires when the *Listener* is the process that dies. On 08-13 at 11:14 the Listener was
fine and the **Worker** took the SIGBUS: `error code null` stayed at **0** through a
failed job. Use the first command, and this, which catches either:

```bash
sudo dmesg -T | grep -cE "I/O error|Got data interrupt"
grep -c "exit code 135" /opt/actions-runner-loppan/_diag/Runner_*.log
```

> ⚠️ **Both of these read 0 through the three-day outage of 08-14, correctly.** They
> detect the SD path, and 08-14 was the cgroup. Nothing here — and nothing else on the
> box, including the health-check cron — can tell you the runner has stopped talking to
> GitHub. For that, and it is the one check that has never been wrong:
>
> ```bash
> gh api repos/Fakhravar1/Loppan/actions/runners --jq '.runners[] | "\(.name) \(.status)"'
> ```
>
> See "The sixth outage" below.

## The fifth crash — 2026-08-13 11:14 UTC — the host controller, not the media

The first job after the repair above failed the same way, 35 minutes after the runner
was re-enabled. That forced the diagnosis to be rebuilt from evidence instead of
inherited, and it came out differently. **The card's stored data is intact, the media is
not failing, and the failure is in the SD host-controller path.**

### What happened

Run `31694612725`, `sweep` job on `qvitta-pi`, scheduled 11:14 UTC. All times UTC.

| | |
|---|---|
| 11:14:33 | Listener spawns Worker pid 83065 |
| 11:14:59 | first `I/O error, dev mmcblk0, sector 22903448 op 0x0:(READ) flags 0x80700 phys_seg 9` |
| 11:15–11:17 | six more, sectors 22903208–22903416; `psi_io_some` **96.6 → 98.9 %**, load 8.39 |
| 11:17:53 | `Finished process 83065 with exit code 135` |
| 11:17:54 | job result `Failed` |

`135` is `128 + 7` — SIGBUS, on the **Worker**. The Listener survived, which is why
`error code null` is 0 and why the runbook's second watch command said nothing was wrong.

No `Worker_*.log` was ever created and `_work/Loppan/Loppan` has not been touched since
08-12. **No step ran: no checkout, no Python, no Algolia, no Supabase.** Everything at or
above the workflow is therefore excluded by construction, not by argument.

### What was excluded, and how

Every one of these was checked on the box rather than reasoned about:

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Memory / cgroup livelock | out | Loppan cgroup `high 0 max 0 oom 0 oom_kill 0` for the whole boot; 116–120 MB against a 300 MB `MemoryHigh`; `MemAvailable` 542 MB throughout |
| Under-voltage / PSU | out | `vcgencmd get_throttled` → `0x0`, no under-voltage line in `dmesg` |
| Thermal | out | 63 °C, no throttling |
| Disk full | out | 22 % space, 8 % inodes |
| Filesystem corruption | out | ext4 `clean`, no error count, rw, never remounted ro |
| Runner self-update swapping a mapped binary | out | no `SelfUpdate*` logs, no `_work/_update`, `libcoreclr.so` mtime is the release date |
| Orphaned / duplicate runner | out | the second `Runner.Listener` is the **sibling's**, started at boot |
| VPN netns / tunnel | out | netns active, wg peer up, DNS resolves inside it |
| dbt contention | out | sibling idle (~300 MB) through the window; its build started 11:18, *after* the failure |
| Kernel regression (1047 → 1060) | out | upgraded 2026-08-04, then nine days with zero I/O errors |
| Bus / connector / signal integrity | out | **0** CRC errors, **0** timeouts, **0** tuning failures, **0** retries, **0** controller resets; card at a standard DDR50 50 MHz, not overclocked |
| Loppan application code | out | never executed (above) |

### What killed the media theory

Four measurements, none of which a failing card survives:

- **The "bad" sectors read fine.** `dd iflag=direct` (page cache bypassed) on 22903400
  and 22903416 returns 4096 bytes in milliseconds, no error, and provokes no new dmesg
  line.
- **The data is intact.** `md5sum` of `/opt/actions-runner-loppan/bin/libcoreclr.so` is
  `fc895b57f963800c63ea6a9276f179e7` — **byte-identical** to the sibling runner's
  independent copy at `/opt/actions-runner/bin/libcoreclr.so`.
- **350 of 350 errors are `op 0x0:(READ)`. Zero write errors**, ever.
- **The card has never reported a failure.** Not one `mmc0: req failed`, no CRC
  (`-84`), no timeout (`-110`), no `-5`, in the entire retained history.

Extent, for the record: 20 distinct sectors spanning 22901904–22903456 — 776 KB — and
`debugfs` maps **all twenty** to one inode:

```bash
start=$(cat /sys/class/block/mmcblk0p2/start)          # 1050624
blk=$(( (SECTOR - start) / 8 ))                        # 4K fs blocks
sudo debugfs -R "icheck $blk" /dev/mmcblk0p2           # block -> inode
sudo debugfs -R "ncheck $INODE" /dev/mmcblk0p2         # inode -> path
#   411202  /opt/actions-runner-loppan/bin/libcoreclr.so
```

### The mechanism

```
mmc0: Got data interrupt 0x00000002 even though no data operation was in progress.   03:10:45
  ... repeated, ~8.3 s apart ...
I/O error, dev mmcblk0, sector 22902232 op 0x0:(READ) flags 0x80700 phys_seg 32      03:11:13
```

The host controller takes a spurious DATA interrupt with no data command in flight. The
driver's recovery cycle fires every ~8.3 s and never clears it, so the read never
completes, and after ~25 s the **block layer synthesises `EIO` for a request the card
never failed**. That `EIO` lands on a demand-paged page of an mmap'd executable, which is
SIGBUS by definition, and the .NET process dies with 135.

The errors cluster in one file because the victim is whatever request is in flight when
the controller wedges — and during .NET startup that is always the same large readahead
(`phys_seg 30/32`) of the same file at the same offsets. It is not a bad region; it is a
fixed access pattern meeting an intermittent fault.

### Why Loppan and never Qvitta, on the same card — hypothesis, not measurement

Both runners are the same runner build and mmap byte-identical copies of the same
library off the same card. Only Loppan has ever crashed. The likeliest reason is cache
residency: the sibling runs **every 15 minutes** under a roomier cap, so its copy stays
warm and is rarely read off the card at all; Loppan runs **every 2 hours** under 300 MB,
so its copy is evicted between runs and is re-read cold on every job start. Loppan would
then be the only workload issuing those big cold readaheads, and so the only one exposed.

**This is not measured** — page-cache residency was not sampled per file. It is written
down because it predicts something cheap and testable: keeping the runner tree resident
should make the failure stop without touching the hardware at all.

### What to do, cheapest first

1. **Reseat the card and clean the contacts** — done 2026-08-13, ~14:40 CEST. Costs
   nothing and addresses the contact-level end of a signalling fault. Verify by whether a
   sweep survives, not by reading files while the box is quiet.
2. **Drop the card out of DDR50.** `dtparam=sd_overclock` / forcing SDR50 in
   `/boot/firmware/config.txt` sidesteps the quirk at a modest throughput cost.
3. **Raise Loppan's `MemoryHigh`** so the runner tree stays cached between runs — tests
   the hypothesis above and, if right, removes the trigger.
4. **Move root to a USB SSD.** Still the right answer, but note the reason has changed:
   it is worth doing because it retires the whole SD path — card, socket *and*
   controller — not because the card is worn out. **A fresh SD card may not fix this**,
   which is exactly what the old reasoning would have predicted it would.

⚠️ **Do not spend another day on the memory ceiling for this.** Twice now the cgroup has
been the first place looked and twice it was innocent; on 08-13 it recorded literally
zero throttle events across a boot containing a failed job.

> ⚠️ **This warning is correct for crashes 4 and 5 and wrong as a general rule.** On
> 08-14 the cgroup was the cause, and the trail measured it: 4.5 million `memory.high`
> throttle events and `psi_mem_full` above 90 % for a day. Kept because the reasoning
> still holds where it was aimed — *the SD-path crashes were not the ceiling* — but read
> it as "not this failure", not "never the ceiling". See "The sixth outage" below.

## The sixth outage — 2026-08-14 09:32 UTC — the second livelock, in the band between High and Max

**The first outage that cost real money, the first that no check on the box detected,
and a recurrence of 08-10 rather than anything new.** It is also the first one where
the SD path was measurably innocent.

### What it cost

The runner went quiet at 09:32 UTC on 08-14 and was not noticed until 08-17. `route`
did its job and fell back to hosted every two hours for three days — but Loppan is a
**private** repo, so hosted minutes bill. `pool sweep` spent **522 of the 834 hosted
minutes** used 08-04..17, running ~150 min/day (142 on the 15th, 159 on the 16th)
against a 2,000 min/month pool shared with every repo on the account.

### What happened, from the trail

| time (UTC) | what the trail shows |
|---|---|
| 09:30:34 | sibling `dbt` spikes, `qvi_mb` 272 → **449**; `MemAvailable` 547 → 362 |
| 09:31:08 | runner logs `Running job: sweep` — **the last line it ever wrote** |
| 09:32:03 | `lop_mb` 290 and climbing to the cap; swap 306 MB |
| 09:32 → 10:20 | `lop_mb` pinned **300–302**, `psi_io_some` **55–85 %**, `lop_high` climbing ~5,000 every 30 s |
| 10:25 → | `psi_mem_full` jumps to **70–98 %** and stays there; `load1` ~9 |
| 08-15 10:58 | last sample with `psi_mem_full > 50` — **~25 hours** of it |
| 08-15 11:14:30 | GitHub cancels the sweep queued at 08-14T11:12:28Z, 24 h 2 min after queueing |

`lop_high` went from 39,779,082 to 44,312,977 — **4.5 million throttle events**. The
cgroup peaked around **333 MB**: above `MemoryHigh=300M`, below `MemoryMax=400M`.

**That band is the whole failure.** Under `MemoryHigh` the cgroup runs. Over
`MemoryMax` it gets OOM-killed, the job fails, and `route` sends the next sweep
elsewhere — loud, fast, cheap. *Between* them it is throttled and reclaimed forever
without ever dying, which is a livelock with no error, no exit code and no corpse. The
sweep sat in that band for a day. `memory.conf` already says "a cgroup parked at
`MemoryHigh` reclaims continuously; headroom under it is the property being bought" —
on 08-14 it got parked there anyway, because the sibling took the headroom first.

### Why nothing on the box caught it

Every detector built after crashes 3–5 was looking somewhere else. All three were clean
**and all three were right to be** — this was not their failure mode:

| check | reading | why it missed |
|---|---|---|
| `dmesg \| grep -cE "I/O error\|Got data interrupt"` | **0** | no SD fault occurred |
| `grep -c "exit code 135" _diag/Runner_*.log` | **0** in every log but 08-13's | nothing took a SIGBUS |
| health-check cron → hc-ping.com | **green throughout** | see below |

The health check is the one worth fixing:

```
systemctl is-active --quiet actions.runner.Fakhravar1-Loppan.qvitta-pi.service \
  && /usr/local/sbin/loppan-tunnel-ok && curl -fsS ... hc-ping.com/f04a571b-...
```

⚠️ **`systemctl is-active` cannot detect this and never could.** `runsvc.sh` is the
unit's `MainPID`, nothing killed it, so the unit stayed `active (running)` for three
days while the Listener underneath it was too starved to hold its connection to GitHub.
The same `MainPID`-survives property was already the whole diagnosis of crash 4 — there
it hid a dying Listener, here it hid a throttled one. **A green tunnel and a green unit
together still mean nothing about whether GitHub can see the runner.** The only check
that would have caught this is the one the `route` job already makes:

```bash
gh api repos/Fakhravar1/Loppan/actions/runners --jq '.runners[] | "\(.name) \(.status)"'
```

### What fixed it

`sudo systemctl restart` — nothing more. Online in seconds, no reboot, no card touched.
Uptime shows the box itself never went down.

⚠️ **`journalctl --list-boots` reports this boot starting 07-28. It did not** — that is
the no-RTC artifact described above, `fixrtc` stamping pre-NTP entries with a stale
clock. `/proc/uptime` is monotonic and is the only boot clock on this box worth
believing.

### What to do about it

1. **Watch runner reachability from GitHub's side, not the box's.** Every on-box signal
   was green through a three-day outage. This is the gap.
2. **Consider narrowing the High↔Max band**, or dropping `MemoryHigh` toward `MemoryMax`
   for the sweep. A killed job is recoverable in one cron cycle; a throttled one is
   invisible for three days and bills for all of them. The band exists to buy headroom,
   and this outage is the bill for that choice.
3. **The `route` cost gate (added 08-17) is the backstop that makes any recurrence
   cheap** — four 6-hourly hosted slots instead of twelve, ~40 min/day instead of ~150.
   It does not prevent the outage; it caps what an unnoticed one costs.
4. **Revisit the oversubscription below.** The trigger was the sibling's `dbt` taking
   the headroom at the moment a sweep started. That is the exact collision "The two
   ceilings deliberately oversubscribe the box" accepts as a known risk.

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

⚠️ **And Loppan does not sit below its throttle point. Measured 2026-08-12, it sits
exactly on it.** Every sweep spike reaches 291–299 MB against a 300 MB `MemoryHigh`,
throttles, and pushes ~80 MB into swap — `high` events climbed 821 → 1434 in three
minutes — *while the box had 300–480 MB free*. Its page cache is squeezed to 30 MB in
the process, against Qvitta's 237 MB. So the sentence above describes an intention, not
the machine: Loppan is being constrained against a limit the box is not short of. That
is worth fixing on its own merits; it is **not** established that it causes the crashes.

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
  between them and moving them adds the marketplace-from-home-IP exposure for little saving.
- **The crawl would originate from a home residential IP** — the same household as
  the owner's the marketplace account, which the README's ground rules care about ("the risk
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

⚠️ **The paragraph that stood here — "one manual step stands between this and the Pi
actually being used: creating `RUNNER_STATUS_TOKEN`" — has been stale since 2026-08-10.**
The token was created that day, which is precisely what started sending real jobs to the
box and set off the second livelock. It is recorded in "What set it off" above; the
summary was never updated to match. Corrected 2026-08-13.

The last lesson is about diagnosis rather than memory. Four of the six failures were
first blamed on the wrong thing, and on 08-13 a *correction* was itself wrong in both
directions at once: it retracted three accurate diagnoses (`SSLEOFError`, two PostgREST
`400`s) and it declared a failing card that measurement does not support. Both mistakes
came from the same move — reading a pile of evidence as one story without opening the
individual pieces. The habit worth keeping is the one the memory work already taught:
**open the artifact and read the number before building an argument on it.** A crash
dump has a `Signal:` field or it does not; a card that is failing reports errors, and
this one never has.

08-14 added the counterpart to that habit. Every on-box check was green — `dmesg` clean,
`exit code 135` at zero, the health-check cron pinging happily — and every one of them
was *correct*, because none of them was watching the thing that failed. Three days and
522 billed minutes went by on that. **A green check is only evidence about what it
measures**, and after five SD-shaped crashes every check on this box was SD-shaped. The
one signal that was never wrong all week came from outside: whether GitHub itself could
see the runner. Prefer the check that sits where the consequence lands.
