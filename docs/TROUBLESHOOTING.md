# kasa-cli — Troubleshooting

The most common gotchas, with their root causes and fixes. Each section is named for a symptom you might Google.

## Contents

1. [Discovery finds zero devices](#discovery-finds-zero-devices)
2. [`KLAP authentication failed` / exit 2](#klap-authentication-failed--exit-2)
3. [`credentials file mode is too permissive` / exit 2](#credentials-file-mode-is-too-permissive--exit-2)
4. [`config file not found` / exit 6](#config-file-not-found--exit-6)
5. [Multi-socket strip turning off the wrong outlet](#multi-socket-strip-turning-off-the-wrong-outlet)
6. [`unsupported feature` / exit 5](#unsupported-feature--exit-5)
7. [EP40M energy reading fails](#ep40m-energy-reading-fails)
8. [Schedule listing returns exit 5 on a Kasa plug](#schedule-listing-returns-exit-5-on-a-kasa-plug)
9. [`--watch` produces no output](#--watch-produces-no-output)
10. [Mixed-protocol fanout: some devices auth, some don't](#mixed-protocol-fanout-some-devices-auth-some-dont)
11. [Cron job exits 0 but nothing happened](#cron-job-exits-0-but-nothing-happened)
12. [`--config` PATH is silently ignored](#--config-path-is-silently-ignored)
13. [`--quiet` still produces output on Ctrl-C](#--quiet-still-produces-output-on-ctrl-c)
14. [Sub-second `--watch` interval rounds up](#sub-second---watch-interval-rounds-up)

---

## Discovery finds zero devices

**Symptoms:** `kasa-cli discover` exits 0 (success) with empty output and a stderr line saying *"timeout reached, 0 devices found"* — but you can `ping` the device.

**Cause:** Multi-NIC routing. macOS does not support `socket.SO_BINDTODEVICE`, so the OS picks which interface to broadcast on. With Wi-Fi + Tailscale + Docker bridges + VPNs all present, the broadcast may go out the wrong interface.

**Fix:** Use `--target-network` with the directed-broadcast address (the `.255` of your LAN's `/24`):

```bash
$ kasa-cli discover --target-network 192.168.1.0/24

# Or specify the exact directed-broadcast address
$ kasa-cli discover --target-network 192.168.1.255
```

If you don't know your LAN's subnet:

```bash
# macOS / Linux
$ ifconfig | grep -A 2 "broadcast"

# Linux only
$ ip -4 addr show
```

Add this to your shell rc as a habit if you have a multi-NIC machine:

```bash
alias kasa-discover="kasa-cli discover --target-network 192.168.1.0/24"
```

---

## `KLAP authentication failed` / exit 2

**Symptoms:** Stderr shows `{"error":"auth_failed","exit_code":2,...}`. Devices known to be working in the Kasa app fail from kasa-cli.

**Cause #1:** No credentials configured. KLAP-protocol devices (post-2022 firmware on most plugs/strips/bulbs) require your TP-Link cloud account email + password for the local handshake.

**Fix #1:**

```bash
$ install -m 0600 /dev/null ~/.config/kasa-cli/credentials
$ cat > ~/.config/kasa-cli/credentials <<'EOF'
{"version": 1, "username": "you@example.com", "password": "..."}
EOF
$ kasa-cli on patio-plug
```

**Cause #2:** Wrong credentials. kasa-cli authenticates against the device on the LAN, not against TP-Link servers, so you won't get a "service-side" error to lean on. The error surfaces only when the KLAP handshake fails locally.

**Fix #2:** Verify in the Kasa mobile app that your credentials work, then:

```bash
$ kasa-cli auth flush                     # drop any stale cached sessions
$ kasa-cli --jsonl info patio-plug -vv    # raw protocol frames on stderr
```

`-vv` will dump the KLAP handshake; the `_AUTH_FAILED` response is unmistakable.

**Cause #3:** Different account per device (e.g., a guest device shared from a friend's account). 

**Fix #3:** Per-device credential override:

```bash
$ install -m 0600 /dev/null ~/.config/kasa-cli/credentials.guest
$ cat > ~/.config/kasa-cli/credentials.guest <<'EOF'
{"version": 1, "username": "guest@example.com", "password": "..."}
EOF
```

Then in `~/.config/kasa-cli/config.toml`:

```toml
[devices.guest-light]
ip = "192.168.1.99"
mac = "AA:BB:CC:DD:EE:99"
credential_file = "~/.config/kasa-cli/credentials.guest"
```

**Cause #4:** Two consecutive auth failures on a single command. kasa-cli auto-retries once with a fresh handshake on the first auth failure — if the second handshake also fails, it gives up and exits 2.

**Fix #4:** Clear the cache and verify the credentials file by hand:

```bash
$ kasa-cli auth flush --target patio-plug
$ jq . ~/.config/kasa-cli/credentials   # validates JSON + dumps for sanity
$ kasa-cli on patio-plug
```

---

## `credentials file mode is too permissive` / exit 2

**Symptoms:** Stderr says something like *"credentials file ~/.config/kasa-cli/credentials has insecure permissions: 0o644"* with hint *"Run: chmod 600 <path>"*.

**Cause:** kasa-cli refuses to read credential files with any group or world bits set (FR-CRED-2). This is non-negotiable security hygiene.

**Fix:**

```bash
$ chmod 600 ~/.config/kasa-cli/credentials
$ ls -la ~/.config/kasa-cli/credentials
> -rw------- 1 you staff 75 Apr 27 22:00 ~/.config/kasa-cli/credentials
```

Also: kasa-cli refuses to follow symlinks for credentials files. If you're managing credentials via a vault wrapper script, materialize the actual file at the configured path; don't symlink to a vault directory.

---

## `config file not found` / exit 6

**Symptoms:** `kasa-cli on kitchen-lamp` exits 6 with *"config file not found"* despite the file existing.

**Cause #1:** `--config` flag or `KASA_CLI_CONFIG` env var pointing somewhere that doesn't exist. kasa-cli is **strict** when an explicit path is set — unlike the implicit default-location lookup, missing explicit paths are an error.

**Fix #1:** Verify:

```bash
$ ls -la "$KASA_CLI_CONFIG"
$ kasa-cli --config /tmp/nonexistent.toml list   # → exit 6
$ unset KASA_CLI_CONFIG && kasa-cli list          # back to default lookup
```

**Cause #2:** Tilde expansion not happening. Cron / systemd / launchd run with restricted environment; `~` may not expand.

**Fix #2:** Use absolute paths in scheduled jobs:

```bash
# Wrong:
0 22 * * * kasa-cli --config ~/.config/kasa-cli/config.toml list

# Right:
0 22 * * * kasa-cli --config /home/you/.config/kasa-cli/config.toml list
```

**Cause #3:** Default config file truly absent. This is **not** an error by itself — kasa-cli uses built-in defaults and prints one INFO line on stderr. If you see exit 6 on the default path, the file probably has invalid TOML, dangling group references, or unknown keys.

**Fix #3:** Lint:

```bash
$ kasa-cli config validate
> error: dangling group reference: groups.bedroom-lights includes "bedrom-lamp" (typo for "bedroom-lamp"?)
> exit code: 6
```

---

## Multi-socket strip turning off the wrong outlet

**Symptoms:** None. kasa-cli specifically prevents this.

**Cause / FR:** Multi-socket strips (KP303, KP400, EP40, HS300) **require** an explicit `--socket N` or `--socket all` flag. There is no implicit "all sockets" default. This was a deliberate design choice (FR-15a) to prevent the unrecoverable foot-gun of accidentally turning off a strip with a router or always-on appliance plugged in.

**What you'll see if you forget:**

```bash
$ kasa-cli off office-strip
> error: office-strip is a multi-socket strip; specify --socket N (1-5) or --socket all
> available sockets:
>   1 monitor    (on)
>   2 printer    (off)
>   3 lamp       (on)
>   4 router     (on)         <-- you really don't want to off this
>   5 speakers   (off)
# exit code: 64
```

**Fix:** Use `--socket N` for individual sockets, `--socket all` when you really mean everything:

```bash
$ kasa-cli off office-strip --socket 2
$ kasa-cli off office-strip --socket all
```

---

## `unsupported feature` / exit 5

**Symptoms:** Stderr says *"Device does not support color-temperature control"* (or similar feature).

**Cause:** Capability mismatch — the verb/flag combo isn't supported by that device family.

| Flag | Required device feature | Devices that don't have it |
|---|---|---|
| `--brightness` | dimmable | most plugs (HS100, KP100, etc.); some switches |
| `--color-temp` | tunable-white | non-color bulbs (older KL110); plain white bulbs |
| `--hsv` / `--hex` / `--color` | color-capable | plugs, switches, tunable-only bulbs |
| `energy` | hardware emeter | most switches; bulbs without metering; **EP40M specifically** |
| `schedule list` (KLAP devices) | python-kasa Schedule module | every KLAP device — not implemented upstream |

**Fix:** Check what the device actually supports:

```bash
$ kasa-cli --json info kitchen-lamp | jq .features
> [
>   "brightness",
>   "color-temp"
> ]
# This bulb is dimmable + tunable-white; --color would exit 5.
```

---

## EP40M energy reading fails

**Symptoms:** `kasa-cli energy ep40m-target` exits 5 with *"EP40M (EP40M(US)) is supported as a device but lacks a hardware emeter."*

**Cause:** TP-Link's EP40M is the "M" (mini) variant of the EP40 outdoor strip and **does not have hardware power monitoring**, despite being supported by python-kasa as a control device. This is a documented limitation — the per-device feature matrix in the upstream library reports no Energy module for EP40M.

**Fix:** None. If you need outdoor energy monitoring, use the EP25 (single outlet) or stay with indoor HS300 / KP125. The error is operationally correct: you should not silently return zeros for a device that has no emeter.

---

## Schedule listing returns exit 5 on a Kasa plug

**Symptoms:** `kasa-cli schedule list patio-plug` exits 5 with *"python-kasa 0.10.2 does not expose schedule listing for KLAP/Smart-protocol devices; revisit when upstream adds a Schedule module to kasa/smart/modules/."*

**Cause:** python-kasa's Schedule module is implemented for **legacy IOT** (XOR-protocol) devices only — typically pre-2022 firmware. KLAP (post-2022) firmware uses a different protocol family that python-kasa hasn't yet wired schedule access for.

**Fix:** Two options:

1. **Wait for upstream**: track [python-kasa #1648](https://github.com/python-kasa/python-kasa/issues/1648) and similar for KLAP schedule support.
2. **Don't store schedules on the device**: use cron / systemd / launchd to drive `kasa-cli on/off` at the desired times. Per FR-25, this is the recommended pattern anyway — kasa-cli is permanently read-only on schedules and won't ever add `schedule add` / `schedule edit`.

To check whether a device is legacy IOT or KLAP:

```bash
$ kasa-cli --json info patio-plug | jq .protocol
> "klap"
```

---

## `--watch` produces no output

**Symptoms:** `kasa-cli energy office-strip --socket 2 --watch 5` shows nothing on stdout. Sometimes for minutes. Then Ctrl-C → still nothing.

**Cause (pre-v0.3.0):** A bug fixed in v0.3.0. Earlier versions buffered the entire watch stream and only emitted on loop termination — production loops are unbounded so stdout stayed silent until Ctrl-C, at which point the buffer was discarded.

**Fix:** Update:

```bash
$ uv tool upgrade kasa-cli
$ kasa-cli --version
> 0.3.0   # or later
```

If you're on v0.3.0+ and still seeing silence, check:

- Is stdout being captured by something? `kasa-cli ... --watch 5 2>&1 | grep something` may suppress the live output if `grep` doesn't see matches; use `grep --line-buffered`.
- Is your terminal multiplexer buffering? `tmux` and `screen` sometimes buffer non-tty output; the `unbuffer` command (`expect-tools` package) forces line buffering.

---

## Mixed-protocol fanout: some devices auth, some don't

**Symptoms:** `kasa-cli on @everything` returns exit 7 (partial failure). The stderr summary says "1 success, 3 auth failures" but the JSONL stream shows the auth-failed devices have wildly different `target` values.

**Cause:** Some devices in your group are KLAP-protocol (need credentials) and some are legacy IOT (no auth). If your credentials are configured and correct for KLAP devices, the legacy ones should still work. If credentials are missing, **only legacy** devices succeed.

**Fix:** Verify credentials are loaded:

```bash
$ kasa-cli auth status   # any cached sessions?
$ ls -la ~/.config/kasa-cli/credentials
$ kasa-cli --jsonl info <one-of-the-failing-aliases> 2>&1 | tail
```

If you have a deliberately-no-credential setup (e.g., shared LAN with someone else's KLAP devices you can't authenticate to), separate them into different groups so the protocol families don't mix:

```toml
[groups]
my-stuff       = ["kitchen-lamp", "office-strip"]    # legacy + my KLAP
guest-stuff    = ["guest-bulb"]                       # someone else's KLAP
```

---

## Cron job exits 0 but nothing happened

**Symptoms:** Crontab entry shows successful exit; no errors in mail; but lights didn't turn off.

**Cause:** Cron environment differs from your interactive shell. The most common culprits:

1. `$PATH` — `/usr/local/bin` and `~/.local/bin` may not be on cron's PATH
2. `$HOME` — set, but tilde expansion in `--config ~/...` may not happen
3. The default config-file location lookup falls back to built-in defaults silently when the cron user's `~/.config/kasa-cli/` is empty

**Fix:**

```cron
# At the top of your crontab, set explicit env
PATH=/home/you/.local/bin:/usr/local/bin:/usr/bin:/bin
HOME=/home/you

# Use absolute paths in commands
0 22 * * * kasa-cli --config /home/you/.config/kasa-cli/config.toml batch --file /home/you/.config/kasa-cli/routines/night.batch >> /home/you/.local/state/kasa-cli/cron.log 2>&1
```

Verify what cron sees with a test entry:

```cron
# Once, to debug
* * * * * env > /tmp/cron-env.txt; kasa-cli --config /home/you/.config/kasa-cli/config.toml list >> /tmp/cron-test.log 2>&1
```

---

## `--config` PATH is silently ignored

**Symptoms:** You pass `--config /path/to/alt.toml` and kasa-cli still loads the default file.

**Cause:** **Global flags must come BEFORE the verb.** Click parses options in two phases — the top-level group's options first, then the sub-verb's. Putting a global flag after the verb name routes it to the verb's parser, which doesn't know about `--config` and may silently ignore it.

```bash
# Wrong — --config goes to the `on` sub-verb (which doesn't have it)
$ kasa-cli on kitchen-lamp --config /tmp/alt.toml

# Right — --config goes to the top-level group
$ kasa-cli --config /tmp/alt.toml on kitchen-lamp
```

`--json`, `--jsonl`, `--quiet`, `--timeout`, `--credential-source`, `--concurrency`, `-v`, `-vv` are all in the same boat — pre-verb only.

---

## `--quiet` still produces output on Ctrl-C

**Symptoms:** A long batch run with `--quiet`. You hit Ctrl-C. A `{"event":"interrupted","completed":N,"pending":M}` line appears on stdout despite `--quiet`.

**Cause:** Documented behavior, not a bug. The FR-31c interrupted summary line is intentionally always emitted — operators usually want to know how many sub-ops completed before the signal landed, and the exit code (130/143) alone doesn't carry that information. The SRD does not formally resolve the FR-35 (`--quiet`) vs FR-31c (interrupted summary) collision; the implementation chose to honor FR-31c.

**Fix:** If you really want zero output even on Ctrl-C, redirect:

```bash
$ kasa-cli --quiet batch --file routine.batch >/dev/null
```

The exit code (130 SIGINT, 143 SIGTERM) still tells you what happened.

---

## Sub-second `--watch` interval rounds up

**Symptoms:** `kasa-cli energy patio-plug --watch 0.5` works. `kasa-cli energy patio-plug --watch 0` exits weirdly.

**Cause:** `--watch SECONDS` accepts floats — `0.5` is honored. `0` is not (would be a busy loop); negative values are rejected at parse time.

**Fix:** Use `--watch 0.1` for ~10 Hz updates, or remove `--watch` for a single-shot read:

```bash
$ kasa-cli energy patio-plug                # one read, then exit
$ kasa-cli energy patio-plug --watch 1.0    # 1 Hz stream
$ kasa-cli energy patio-plug --watch 0.5    # 2 Hz stream
$ kasa-cli energy patio-plug --watch 0.1    # 10 Hz stream
```

Note: device-side rate limiting on KLAP firmware sometimes rejects very fast polling. If you see exit 3 errors after a few hundred milliseconds, slow down to 1 Hz or longer.

---

## See also

- [docs/USAGE.md](USAGE.md) — every verb's flags
- [docs/CONFIG.md](CONFIG.md) — config file schema
- [docs/EXAMPLES.md](EXAMPLES.md) — common usage patterns
- [docs/SRD-kasa-cli.md](SRD-kasa-cli.md) §11 — Error model and exit codes (canonical reference)

If you hit something not listed here, it's worth checking the [v0.x.0 release notes](../CHANGELOG.md) for known issues.
