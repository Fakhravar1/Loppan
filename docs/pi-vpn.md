# The Loppan VPN namespace on qvitta-pi

Why the crawl leaves the house through a tunnel, how it is scoped so it cannot touch
the Qvitta project, and what to do when it breaks.

Read `pi-runner.md` first — it explains what the Pi is and why it is shared.

---

## 1. What this is for, and what it is not for

`api-notes.md` names the real exposure: nothing here authenticates as a user, so the
worst realistic case is an IP being rate-limited. The residual risk is **correlation**
— crawl traffic leaving the same household address that also carries a logged-in
Sellpy session. This removes that link.

**It is not rate-limit evasion.** There is no IP rotation and there will not be. The
whole crawl is a few thousand Algolia requests, about 73 seconds of traffic; there is
no rate problem to hide from. Rotation would also contradict `api-notes.md`'s standing
"no distributed crawling, one account" rule, and rotating addresses is a detection
signature in its own right — a single, boring, stable origin is the quiet option.

---

## 2. Why a network namespace and not a system-wide VPN

**The Pi is shared production hardware.** It runs the Qvitta project's `dbt build`
every 15 minutes on a second Actions runner, and when this box livelocked on
2026-08-07 that project lost its scheduled runs for ~12 hours. A system-wide tunnel
with a kill switch would take Qvitta offline on every reconnect.

So everything lives in a namespace called `loppan`:

```
host namespace                    loppan namespace
├── eth0/wlan0  → home router     ├── lo
├── Qvitta runner  (untouched)    ├── wg-loppan  → NordVPN Sweden
└── wg-loppan's UDP socket ───────┘   └── Loppan runner, and every job step
```

**The kill switch is structural, not a firewall rule.** The namespace contains exactly
two interfaces: loopback, and the tunnel. There is no second path. If the tunnel is
down, traffic does not fall back to the home IP — it fails, because there is nowhere
for it to go. Nothing has to be remembered or configured for that to hold, which is
the entire reason for choosing this design over `nordvpn set killswitch on`.

**The interface is created in the host namespace and then moved.** A WireGuard
interface keeps its UDP socket in whichever namespace it was created in, so the
encrypted packets still leave over the Pi's ordinary LAN route while the plaintext
side sits inside the namespace. Create it inside and it has no way to reach the
internet to establish the tunnel at all.

**No daemon and no snap in steady state.** WireGuard is in the kernel
(`/lib/modules/6.8.0-1060-raspi/.../wireguard.ko.zst` — already present on Ubuntu
24.04 arm64), so the running cost is one network interface and zero processes. The
NordVPN client is installed only long enough to obtain a key, then removed. That
matters on a 899 MB box with a history of memory exhaustion.

---

## 3. Files

| Path | What it is |
|---|---|
| `deploy/pi-vpn/install` | Copies everything into place. Idempotent. Does **not** bind the runner unless `--bind-runner` |
| `deploy/pi-vpn/set-key` | Obtains the NordLynx key and writes it to `/etc/wireguard/loppan.conf`. Never prints it |
| `deploy/pi-vpn/verify` | Four checks, including a deliberate leak test |
| `deploy/pi-vpn/loppan-netns-up` / `-down` | The namespace itself |
| `deploy/pi-vpn/loppan-netns.service` | systemd unit, enabled at boot |
| `deploy/pi-vpn/runner-netns.conf` | Drop-in that binds the Loppan runner to the namespace |

On the Pi they live at `/usr/local/sbin/` and `/etc/systemd/system/`, with the working
copy of the repo files in `~/loppan-deploy/deploy/pi-vpn/`.

---

## 4. Setting it up

```bash
sudo ~/loppan-deploy/deploy/pi-vpn/install     # plumbing, runner untouched
nordvpn login                                  # YOU do this — see below
sudo ~/loppan-deploy/deploy/pi-vpn/set-key     # fetch key, remove the client
sudo systemctl start loppan-netns
sudo ~/loppan-deploy/deploy/pi-vpn/verify      # must pass before the next line
sudo ~/loppan-deploy/deploy/pi-vpn/install --bind-runner
```

**`nordvpn login` is deliberately not automated.** It prints a URL; open it in a
browser, sign in, and the CLI picks up the result. NordVPN publishes WireGuard keys
nowhere else — not on a web page, not through an API — so letting their client
negotiate one is the only route to a key, and that step needs the account.

`set-key` writes the key straight into a `0600` file. It is never echoed, so it does
not reach a terminal scrollback, the systemd journal, or an agent transcript.

### The order matters

`install` does not bind the runner, and `--bind-runner` is a separate invocation on
purpose. Binding stops the Loppan runner until the tunnel works — `Requires=` on the
namespace unit means a runner that cannot get a tunnel does not start at all, rather
than starting in the host namespace and crawling from the home IP. Run `verify` first.

---

## 5. Verifying

`verify` asks four questions, and the fourth is the one that matters:

1. Does the **host** still reach the internet? (i.e. did we break Qvitta)
2. Does the **namespace** reach the internet?
3. Is the namespace's public IP **different** from the host's?
4. With the tunnel route removed, does the namespace **fail closed**?

Check 4 deletes the default route and confirms nothing escapes. If anything answers,
the namespace is not the kill switch it claims to be and the design is void — do not
bind the runner.

### First run, 2026-08-10 — all four passed

| | Result |
|---|---|
| Host public IP | `2.249.73.218` (home) |
| Namespace public IP | `187.15.109.101` (NordVPN Sweden) |
| Route removed | `Could not resolve host` — nothing escaped |
| Qvitta runner | `active` throughout, never left the host namespace |

Namespace IDs confirm the split: the Loppan runner sits in `net:[4026532549]` while the
Qvitta runner and `init` share `net:[4026531840]`.

Both real endpoints were then exercised **through the tunnel**, because reaching
GitHub proves nothing about whether Sellpy's infrastructure accepts a VPN address:

| Endpoint | Result |
|---|---|
| Algolia `prod_marketItem_se_relevance` | `200`, `nbHits` 11,129,691 |
| Parse `MarketOffer` with the browser SDK keys | `200`, real rows, 0.22 s |
| GitHub API | `200`, 0.38 s — runner logged "Connected to GitHub" |

⚠️ A bare `GET /parse` answers **403** even when everything is fine — Parse requires
the application and JavaScript keys on every request. Do not read that 403 as a
blocked VPN address; test with the keys, as the table above does.

Memory after the client was removed: **392 MB available of 899 MB**, no new daemons.

---

## 6. When it breaks

### The tunnel is monitored now (2026-08-11)

It was not before. Both Healthchecks crons tested only that a **runner service** was
active, which says nothing about the tunnel — so a dead tunnel with a live runner raised
no alert at all, and you found out by reading a failed workflow log. §7 listed that as a
known weakness; this closes it.

`/usr/local/sbin/loppan-tunnel-ok` (source: `deploy/pi-vpn/loppan-tunnel-ok`) exits 0
only if `wg` reports a handshake newer than 300 s, and the Loppan ping is gated on it:

```
runner active  &&  tunnel healthy  →  ping
```

⚠️ **This changes what an existing alert means.** A `qvitta-pi-loppan-runner is DOWN`
email no longer implies the runner is down — it now means *the runner or the tunnel*.
Check both:

```bash
systemctl is-active actions.runner.Fakhravar1-Loppan.qvitta-pi.service
sudo /usr/local/sbin/loppan-tunnel-ok && echo "tunnel ok" || echo "tunnel is the problem"
```

The check lives in **root's** crontab, not `arian`'s, because `ip netns exec` needs
root; the Qvitta ping is untouched in the user crontab. `MAX_AGE`, `IFACE` and `NETNS`
are overridable so the gate can be tested without tearing the tunnel down — verified
2026-08-11 against a healthy tunnel (pass), a stale handshake, a missing interface and a
missing namespace (all three correctly refuse to ping).

**Symptom: the Loppan runner is offline and workflows queue.**

That is the designed failure. `track.yml`'s `route` job probes runner status before
dispatching and falls back to a hosted runner when the Pi is unavailable, so a pass is
not lost — it costs billed minutes instead. Check:

```bash
systemctl status loppan-netns
sudo ip netns exec loppan wg show wg-loppan
```

A missing `latest handshake` means the tunnel never established. Most likely causes,
in order: the NordLynx key expired (re-run `set-key`), the endpoint went away
(restarting the unit picks a fresh server from the recommendation API), or the Pi has
no internet at all (check the host, and Qvitta will be failing too).

**Restarting** is safe and idempotent:

```bash
sudo systemctl restart loppan-netns
sudo systemctl restart actions.runner.Fakhravar1-Loppan.qvitta-pi.service
```

**Removing it entirely**, if it ever becomes more trouble than it is worth:

```bash
sudo rm -rf /etc/systemd/system/actions.runner.Fakhravar1-Loppan.qvitta-pi.service.d
sudo systemctl disable --now loppan-netns
sudo systemctl daemon-reload
sudo systemctl restart actions.runner.Fakhravar1-Loppan.qvitta-pi.service
```

The runner returns to the host namespace and the crawl leaves from home again.

---

## 7. Known weaknesses, recorded rather than hidden

- **The NordLynx key can be rotated by NordVPN.** Nothing *auto-recovers* from it — see
  the monitoring note below for how you now find out. If it happens more than once, the
  fix is a scheduled `set-key`, not a longer troubleshooting session.

- **What recovers by itself, and what does not.** The peer carries
  `persistent keepalive: every 25 seconds`, so a transient blip, a router reboot or a
  NAT timeout re-handshakes on its own with no intervention. What does *not* recover:
  the unit is `Type=oneshot` with `RemainAfterExit=yes` and **no `Restart=`**, so if the
  namespace is torn down systemd still reports it active and never rebuilds it; the
  endpoint is fixed at namespace start, so a retired server is retried forever until
  `systemctl restart loppan-netns` picks a fresh one; and a rotated key fails handshakes
  permanently until `set-key` is re-run.
- **The endpoint is chosen at namespace start, not per request.** A server going away
  mid-pass means a failed pass, recovered on the next start.
- **Traffic still traverses the home internet connection.** The tunnel hides the
  address, not the fact that a connection exists. Only moving the job off the premises
  entirely would change that, and that was considered and rejected in favour of
  keeping the Pi's unbilled minutes.
- **Granting the NordVPN snap `network-control` and `firewall-control`** was necessary
  to obtain the key. The snap is removed afterwards, but note that those permissions
  were held, briefly, on shared production hardware.
