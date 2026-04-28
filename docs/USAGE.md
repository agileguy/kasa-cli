# kasa-cli — Usage Guide

Every verb, every flag, with worked examples. Skim the table of contents, jump to what you need.

> **Conventions:** `<target>` is an alias from your config, an IP, a MAC, or `@group-name`. Lines starting with `$` are shell commands; lines starting with `>` are program output; trailing `# comment` calls out a thing to notice.

## Contents

1. [Global flags](#global-flags)
2. [Discovery](#discovery)
3. [Listing](#listing)
4. [Info](#info)
5. [Control: on / off / toggle](#control-on--off--toggle)
6. [Set: brightness / color / color-temperature](#set-brightness--color--color-temperature)
7. [Energy](#energy)
8. [Schedule (read-only)](#schedule-read-only)
9. [Groups](#groups)
10. [Batch](#batch)
11. [Auth](#auth)
12. [Config](#config)
13. [Output formats](#output-formats)
14. [Exit codes](#exit-codes)

---

## Global flags

These apply to every verb. Always passed BEFORE the verb name.

| Flag | Purpose |
|---|---|
| `--json` | Pretty JSON output (one document per invocation) |
| `--jsonl` | One JSON object per line (default on pipes) |
| `--quiet` | Suppress stdout entirely; only the exit code communicates result |
| `--timeout SECONDS` | Per-operation timeout (default 5; sub-second values like `0.5` accepted) |
| `--config PATH` | Use a non-default config file (overrides `KASA_CLI_CONFIG` env var) |
| `--credential-source env\|file\|none` | Constrain credential resolution. `none` skips KLAP-protocol devices entirely |
| `--concurrency N` | Override `[defaults] concurrency` for `@group` and `batch` operations |
| `-v` / `-vv` | Stderr verbosity. `-v` enables INFO; `-vv` enables DEBUG (raw protocol frames, credentials redacted) |

**Precedence:** `--config PATH` > `KASA_CLI_CONFIG` env var > `~/.config/kasa-cli/config.toml`. If `--config` or the env var points at a missing file, kasa-cli exits **6** (config error). If only the default location is consulted and absent, kasa-cli runs with built-in defaults and prints one INFO line on stderr.

---

## Discovery

```
kasa-cli discover [--target-network CIDR]
```

Broadcasts on UDP/9999 (legacy) and UDP/20002 (KLAP/Smart). Streams responding devices.

```bash
$ kasa-cli discover
> kitchen-lamp     192.168.1.42   AA:BB:CC:DD:EE:01  KL130    legacy   on
> office-strip     192.168.1.51   AA:BB:CC:DD:EE:02  HS300    legacy   on
> patio-plug       192.168.1.78   AA:BB:CC:DD:EE:03  EP25     klap     off

$ kasa-cli --json discover
> [
>   {"alias": "kitchen-lamp", "ip": "192.168.1.42", "mac": "...", "model": "KL130", "protocol": "legacy", "state": "on"},
>   ...
> ]
```

**Multi-NIC hosts (Wi-Fi + Tailscale + Docker bridges)**: macOS lacks `SO_BINDTODEVICE`, so the OS picks the broadcast interface — possibly the wrong one. Use `--target-network`:

```bash
$ kasa-cli discover --target-network 192.168.1.0/24
```

**Zero responders within the timeout** is a successful state — exit **0** with empty output and one INFO line on stderr. Exit **3** is reserved for cases where the broadcast itself failed (no usable interface, socket bind error). See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#discovery-finds-zero-devices) if discovery silently misses devices you can ping.

---

## Listing

```
kasa-cli list [--probe] [--online-only] [--groups]
```

Prints every alias defined in your config. By default, **does not** issue any per-device probe — output reflects config-resolved data only (instant return).

```bash
$ kasa-cli list
> kitchen-lamp     192.168.1.42   AA:BB:CC:DD:EE:01
> office-strip     192.168.1.51   AA:BB:CC:DD:EE:02
> patio-plug       192.168.1.78   AA:BB:CC:DD:EE:03

$ kasa-cli list --probe
# Each device gets a quick liveness check (concurrency-bounded)
> kitchen-lamp     192.168.1.42   ...   online=true
> office-strip     192.168.1.51   ...   online=true
> patio-plug       192.168.1.78   ...   online=false

$ kasa-cli list --online-only
# Implies --probe, filters to live devices only

$ kasa-cli list --groups
# Print group definitions instead of devices
> bedroom-lights: ["bedroom-lamp", "hallway-strip"]
> night-off:      ["kitchen-lamp", "office-strip", "patio-plug"]
```

`list` does **not** scan the LAN. To find devices not yet in your config, use `discover`.

---

## Info

```
kasa-cli info <target>
```

Issues a live `update()` against the device and prints its full state.

```bash
$ kasa-cli --json info kitchen-lamp
> {
>   "alias": "kitchen-lamp",
>   "ip": "192.168.1.42",
>   "mac": "AA:BB:CC:DD:EE:01",
>   "model": "KL130",
>   "hardware_version": "1.0",
>   "firmware_version": "1.8.11",
>   "protocol": "legacy",
>   "features": ["brightness", "color", "color-temp"],
>   "state": "on",
>   "sockets": null,
>   "last_seen": "2026-04-27T22:04:31Z"
> }
```

For multi-socket strips (HS300, KP303, KP400, EP40), `sockets` is populated:

```bash
$ kasa-cli --json info office-strip
> {
>   ...
>   "model": "HS300",
>   "state": "mixed",
>   "sockets": [
>     {"index": 1, "alias": "monitor",     "state": "on"},
>     {"index": 2, "alias": "printer",     "state": "off"},
>     {"index": 3, "alias": "lamp",        "state": "on"},
>     {"index": 4, "alias": "speakers",    "state": "off"},
>     {"index": 5, "alias": "router",      "state": "on"}
>   ]
> }
```

Exits **4** if the alias is unknown or the IP is unreachable.

---

## Control: on / off / toggle

```
kasa-cli on <target> [--socket N|all]
kasa-cli off <target> [--socket N|all]
kasa-cli toggle <target> [--socket N|all]
```

`on` and `off` are **idempotent** — `on` against an already-on device exits 0 silently. `toggle` flips the current state.

```bash
$ kasa-cli on kitchen-lamp
$ kasa-cli off kitchen-lamp
$ kasa-cli toggle kitchen-lamp
```

### Multi-socket strips: `--socket` is required

KP303, KP400, EP40, and HS300 are multi-socket strips. **You MUST specify `--socket N` or `--socket all`** — there is no implicit "all sockets" default. This prevents the unrecoverable foot-gun of accidentally turning off a strip with a router or always-on appliance plugged in.

```bash
$ kasa-cli off office-strip
> error: office-strip is a multi-socket strip; specify --socket N (1-5) or --socket all
> available sockets:
>   1 monitor    (on)
>   2 printer    (off)
>   3 lamp       (on)
>   4 speakers   (off)
>   5 router     (on)
# exit code: 64

$ kasa-cli off office-strip --socket 2
# Turns off socket 2 only (the printer)

$ kasa-cli off office-strip --socket all
# Turns off every socket — explicit fan-out

$ kasa-cli toggle office-strip --socket all
# Each socket flips INDEPENDENTLY based on its current state
# A [on, off, on, off, on] strip becomes [off, on, off, on, off]
```

For single-socket devices, `--socket 1` and no `--socket` are equivalent; any other value exits 64.

---

## Set: brightness / color / color-temperature

```
kasa-cli set <target>
    [--brightness 0-100]
    [--color-temp KELVIN]
    [--hsv H,S,V | --hex #rrggbb | --color NAME]
    [--socket N|all]
```

`--hsv`, `--hex`, and `--color` are **mutually exclusive**. `--brightness` and `--color-temp` may be combined with each other or with one color flag.

```bash
$ kasa-cli set bedroom-lamp --brightness 30
$ kasa-cli set bedroom-lamp --color-temp 2700
$ kasa-cli set bedroom-lamp --color blue
$ kasa-cli set bedroom-lamp --color "warm-white" --brightness 50
$ kasa-cli set bedroom-lamp --hsv 240,100,100
$ kasa-cli set bedroom-lamp --hex "#ff8000"
```

**Built-in named colors:** `warm-white`, `cool-white`, `daylight`, `red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `magenta`, `pink`. The three white names map to neutral HSV `(0, 0, 100)` because pure HSV cannot distinguish color temperatures — for tunable-white targeting use `--color-temp 2700` (warm) / `5000` (cool) / `6500` (daylight) instead.

**Capability gating:**

| Flag | Required device feature | Exit code on mismatch |
|---|---|---|
| `--brightness` | dimmable | 5 (unsupported feature) |
| `--color-temp` | tunable-white | 5 |
| `--hsv` / `--hex` / `--color` | color-capable | 5 |

**Color-temp clamping (FR-17):** `--color-temp` is clamped to the device's reported `(min, max)` range. Values outside `[1000, 12000]` Kelvin are rejected as a typo (e.g., `--color-temp 27000` → exit 64 with hint *"did you mean 2700?"*).

**Multi-socket strips:** as with on/off/toggle, you must pass `--socket N` or `--socket all` on multi-socket devices. `--socket all` applies the same set operation to every child.

---

## Energy

```
kasa-cli energy <target> [--socket N] [--watch SECONDS] [--cumulative|--no-cumulative]
```

Reads instantaneous power, voltage, current, and (optionally) cumulative kWh from energy-monitoring devices.

```bash
$ kasa-cli --json energy office-strip --socket 2
> {
>   "ts":              "2026-04-27T22:04:31Z",
>   "alias":           "office-strip",
>   "socket":          2,
>   "current_power_w": 42.1,
>   "voltage_v":       120.2,
>   "current_a":       0.35,
>   "today_kwh":       1.23,
>   "month_kwh":       18.7
> }
```

`current_power_w` / `voltage_v` / `current_a` come from one live `update()` call. `today_kwh` / `month_kwh` add an extra `get_daystat`/`get_monthstat` round-trip (~200 ms).

### `--watch SECONDS` — JSONL streaming

```bash
$ kasa-cli --jsonl energy office-strip --socket 2 --watch 5
> {"ts":"...","alias":"office-strip","socket":2,"current_power_w":42.1,"voltage_v":120.2,"current_a":0.35,"today_kwh":null,"month_kwh":null}
> {"ts":"...","alias":"office-strip","socket":2,"current_power_w":41.8,"voltage_v":120.1,"current_a":0.35,"today_kwh":null,"month_kwh":null}
> ...
```

Each tick is flushed immediately to stdout — pipe to `jq`, `tail -f` redirected to a file, etc.

**`--cumulative` defaults**: under `--watch`, cumulative kWh is **omitted** by default to keep each tick fast (so `today_kwh` / `month_kwh` are `null` in the watch stream). Pass `--cumulative` to include them; this adds ~200 ms per tick. For single-shot reads (no `--watch`), cumulative is included by default — pass `--no-cumulative` to skip.

Sub-second intervals (`--watch 0.5`) are honored.

### Energy-supported devices (v1)

HS110, **HS300 (per-socket)**, KP115, KP125, KP125M, EP10, EP25, EP40. EP40M is supported as a device but **lacks a hardware emeter** — `energy ep40m-target` exits **5** (unsupported feature). Non-energy devices (HS200 switches, KL110 bulbs, etc.) also exit 5.

For **HS300 strip totals** (no `--socket`), kasa-cli sums per-child emeter readings when the parent doesn't expose an Energy module (firmware-dependent). Voltage is the last non-zero child reading (children share AC line); current is summed.

---

## Schedule (read-only)

```
kasa-cli schedule list <target>
```

Reads device-stored schedule rules from **legacy IOT devices** (via python-kasa's `Module.IotSchedule`).

```bash
$ kasa-cli --json schedule list kitchen-lamp
> [
>   {"id": 0, "enabled": true,  "time_spec": "daily 22:00",                 "action": "off"},
>   {"id": 1, "enabled": true,  "time_spec": "weekly mon,wed,fri 06:30",    "action": "on"},
>   {"id": 2, "enabled": false, "time_spec": "daily 18:00",                 "action": "set_brightness:50"}
> ]
```

KLAP/Smart-protocol devices exit **5** with the message: *"python-kasa 0.10.2 does not expose schedule listing for KLAP/Smart-protocol devices; revisit when upstream adds a Schedule module to kasa/smart/modules/."*

**FR-25:** v1 has **no** add/remove/edit sub-verbs. Schedules belong in cron, systemd timers, or launchd in this tool's worldview. This is a permanent v1 stance, not a deferred feature.

---

## Groups

```
kasa-cli groups list
```

Prints every group from your config:

```bash
$ kasa-cli --json groups list
> [
>   {"name": "bedroom-lights", "members": ["bedroom-lamp", "hallway-strip"]},
>   {"name": "night-off",      "members": ["kitchen-lamp", "office-strip", "patio-plug"]}
> ]
```

**There is no `groups add` / `groups remove` / `groups edit`.** Hand-edit `~/.config/kasa-cli/config.toml` to manage groups. The reasoning: comment-preserving TOML round-trip is non-trivial and most operators have a small, stable group set.

### `@group-name` target syntax

Most per-target verbs accept `@group-name` to fan out across the group's members:

```bash
$ kasa-cli on @bedroom-lights
# Turns on every member in parallel; emits one TaskResult per member as JSONL

$ kasa-cli --json off @bedroom-lights
> {"target":"bedroom-lamp",   "success":true, "exit_code":0, "output":null, "error":null}
> {"target":"hallway-strip",  "success":true, "exit_code":0, "output":null, "error":null}
# Aggregate exit code: 0 (all success)

$ kasa-cli off @bedroom-lights
# One device unreachable: exit 7 (partial failure)
# All devices unreachable: exit 3 (homogeneous network failure)
# Mix of unreachable + auth-failed: exit 7 (mixed all-failure)
```

Verbs that work with `@group-name`: `info`, `on`, `off`, `toggle`, `set`, `energy`, `schedule list`.

**Concurrency** is bounded by `[defaults] concurrency` (default 10) or the global `--concurrency N` flag.

**Mixed-strip groups + `--socket`**: combining `@group` with `--socket N` is rejected with exit **64** because the same socket index applied across mixed strip and non-strip devices is operationally surprising. Use a shell loop instead:

```bash
$ for a in $(kasa-cli list --groups | jq -r '.[]|select(.name=="strips").members[]'); do
    kasa-cli off "$a" --socket 2
  done
```

---

## Batch

```
kasa-cli batch (--file PATH | --stdin) [--concurrency N]
```

Reads newline-delimited sub-commands and dispatches them in parallel. The leaf use case for cron / systemd timers.

```bash
# A batch file
$ cat night.batch
# Comments and blank lines are skipped silently
off @living-room
off @kitchen

# Multiple verbs allowed; each runs independently
set bedroom-lamp --brightness 5 --color-temp 2200
off office-strip --socket 1
off office-strip --socket 2

$ kasa-cli batch --file night.batch
# JSONL stream — one line per sub-command result
> {"target":"living-room",   "success":true, "exit_code":0, ...}
> {"target":"kitchen",       "success":true, "exit_code":0, ...}
> {"target":"bedroom-lamp",  "success":true, "exit_code":0, ...}
> {"target":"office-strip:1","success":true, "exit_code":0, ...}
> {"target":"office-strip:2","success":true, "exit_code":0, ...}

# Same content via stdin pipe
$ printf "off @living-room\noff @kitchen\n" | kasa-cli batch --stdin
```

### Aggregate exit codes (FR-31a)

| Outcome | Exit code |
|---|---|
| All sub-commands succeed | **0** |
| At least one success AND at least one failure (any reason) | **7** (partial failure) |
| All fail with the same reason (e.g., all unreachable) | That reason's code (e.g., **3**) |
| All fail with different reasons | **7** (mixed all-failure) |

For partial / all-failure runs, kasa-cli also writes one structured summary line to stderr per [SRD §11.2](SRD-kasa-cli.md).

### Graceful Ctrl-C (FR-31c)

On SIGINT or SIGTERM during a batch, kasa-cli:

1. Stops dispatching new sub-commands
2. Waits up to 2 seconds for in-flight commands to complete
3. Emits a final JSONL summary line on stdout: `{"event":"interrupted","completed":N,"pending":M}`
4. Flushes any pending KLAP session state to disk
5. Exits **130** (SIGINT) or **143** (SIGTERM)

Already-emitted result lines stay valid — `tail -f`/`jq` consumers see clean newline-terminated JSON throughout.

### Line grammar

- Blank lines: skipped
- Lines starting with `#` (after whitespace): comments, skipped
- Empty input file or empty stdin: exit 0 with `[]` in `--json` mode
- `shlex.split` parses each line, so quoting works: `set patio --color "warm white"`

`--cumulative` and `--no-cumulative` are accepted as bare flags inside batch lines (no `=value` required); see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) if you hit parse oddities.

---

## Auth

```
kasa-cli auth status [--target ALIAS]
kasa-cli auth flush  [--target ALIAS]
```

Inspect or clear the per-device KLAP session cache at `~/.config/kasa-cli/.tokens/<MAC>.json`.

```bash
$ kasa-cli --json auth status
> [
>   {"alias":"patio-plug",  "mac":"AA:BB:CC:DD:EE:03", "mtime":"2026-04-27T18:02:14Z", "bytes":312, "expires_at":"2026-04-28T17:42:14Z"},
>   {"alias":"<unmapped>",  "mac":"AA:BB:CC:DD:EE:11", "mtime":"2026-04-26T11:30:01Z", "bytes":312, "expires_at":"2026-04-27T11:10:01Z"}
> ]

$ kasa-cli auth flush
# Deletes every cached session

$ kasa-cli auth flush --target patio-plug
# Deletes only that device's session
```

Cache files are stored with mode `0600` inside a `0700` directory. Expiration is computed from python-kasa's `_session_expire_at` (TIMEOUT cookie from KLAP handshake1 minus a 20-minute safety buffer). On disk, the expiry is stored as a wall-clock timestamp so it survives process restarts.

When a cached session is rejected by the device (firmware reboot, password rotation), kasa-cli auto-retries once with a fresh handshake.

---

## Config

```
kasa-cli config show
kasa-cli config validate [PATH]
```

```bash
$ kasa-cli config show
# Prints the effective resolved config in TOML — useful to verify
# precedence (--config flag > KASA_CLI_CONFIG > default)
> [defaults]
> timeout_seconds = 5
> concurrency = 10
> output_format = "auto"
> ...

$ kasa-cli config validate
# Lints the active config (default location); exit 0 on success, 6 on error

$ kasa-cli config validate /tmp/new-config.toml
# Lints a candidate config without making it active
```

See [docs/CONFIG.md](CONFIG.md) for the full schema.

---

## Output formats

| Mode | Default for | Trigger |
|---|---|---|
| `text` | tty stdout | (default) |
| `jsonl` | piped stdout | (default when stdout is not a tty) |
| `json` | — | `--json` (force pretty array) |
| `jsonl` | — | `--jsonl` (force one-per-line) |
| `quiet` | — | `--quiet` (no stdout; exit code only) |

**FR-35a guarantee:** in `--json` and `--jsonl` modes, every byte written to stdout is round-trip-validated through `json.loads` before being flushed. The CLI never emits malformed JSON. On non-zero exit in `--json` mode, stdout is either valid JSON or empty (never half-written).

For batch / group operations with mixed results, **stdout JSONL contains one result object per attempted sub-operation including failures** (each with its own `error` field per SRD §11.2). Stderr emits one structured summary error per run.

---

## Exit codes

Per [SRD §11.1](SRD-kasa-cli.md#11-error-model-and-exit-codes):

| Code | Meaning | When |
|---|---|---|
| 0 | Success | Operation completed; for batch/group, **every** sub-op succeeded |
| 1 | Device error | Device returned an error response (non-auth, non-network) |
| 2 | Authentication error | KLAP auth failed; no credentials; credentials file mode too permissive |
| 3 | Network error | Timeout, connection refused, no route, broadcast bind failure, KLAP device unreachable |
| 4 | Device not found | Alias unknown, IP unreachable, MAC not on LAN |
| 5 | Unsupported feature | Verb/flag combo not supported by target device family or firmware |
| 6 | Config error | Config file missing when `--config` set; invalid TOML; unknown keys; unresolvable references |
| 7 | Partial / mixed-result failure | Batch/group: ≥1 sub-op succeeded AND ≥1 failed (any reason); OR all-failed with mixed reasons |
| 64 | Usage error | Invalid CLI invocation: missing arg, mutex flag conflict, unknown named color |
| 130 | SIGINT | Ctrl-C during batch/group; partial JSONL stream + interrupted summary |
| 143 | SIGTERM | Process terminated; same partial-result + interrupted-line behavior |

**For batch / group operations**, when every sub-op fails with the **same** reason (all unreachable → 3, all auth-failed → 2), kasa-cli exits with that reason's code instead of 7. This lets shell scripts `set -e` gracefully:

```bash
if ! kasa-cli on @bedroom-lights; then
    case $? in
        2) echo "Auth issue — check credentials file" ;;
        3) echo "Network issue — devices unreachable" ;;
        7) echo "Mixed result — some on, some failed" ;;
        *) echo "Unexpected exit code $?" ;;
    esac
fi
```

---

## See also

- [docs/CONFIG.md](CONFIG.md) — TOML config schema reference
- [docs/EXAMPLES.md](EXAMPLES.md) — cron / systemd / shell-pipeline patterns
- [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) — KLAP auth, multi-NIC, EP40M, common errors
- [docs/SRD-kasa-cli.md](SRD-kasa-cli.md) — the full specification
