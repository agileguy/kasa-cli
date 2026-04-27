# Software Requirements Document: kasa-cli

**Document ID:** SRD-KASA-CLI-001
**Version:** 1.0.0
**Date:** 2026-04-27
**Status:** Draft — Ready for Review
**Author:** Dan Elliott
**Source:** Derived from user requirements + verified python-kasa 0.10.2 metadata (PyPI, released 2025-02-12; no follow-up tagged release as of 2026-04-27)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Background and Prior Art](#3-background-and-prior-art)
4. [Architecture Decision: Wrap vs Reimplement](#4-architecture-decision-wrap-vs-reimplement)
5. [Functional Requirements](#5-functional-requirements)
6. [Authentication and Credentials](#6-authentication-and-credentials)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [CLI Surface](#8-cli-surface)
9. [Configuration File](#9-configuration-file)
10. [Data Model](#10-data-model)
11. [Error Model and Exit Codes](#11-error-model-and-exit-codes)
12. [Testing Strategy](#12-testing-strategy)
13. [Distribution and Install](#13-distribution-and-install)
14. [Out of Scope](#14-out-of-scope)
15. [Resolved Decisions](#15-resolved-decisions)
16. [Phase Plan](#16-phase-plan)

---

## 1. Overview

`kasa-cli` is a deterministic, scriptable command-line tool for discovering, querying, and controlling TP-Link Kasa smart devices on the local LAN. It is not a HomeKit bridge, not a cloud daemon, not an MQTT broker, not a rules engine, and not a GUI dashboard. It is a single binary that takes a verb, a target, and flags, performs one operation against one or more devices over the local network, prints a result on stdout, and exits with a meaningful status code. Its job is to be the leaf node in a shell pipeline or cron job — nothing more.

---

## 2. Goals and Non-Goals

### 2.1 Goals

- **Discover** Kasa devices on the local network across both legacy and KLAP-era firmware
- **Query** device state (alias, model, firmware, on/off, brightness, color, energy)
- **Control** devices: on, off, toggle, set brightness, set color, set color-temperature
- **Export energy data** in machine-parseable formats from energy-monitoring devices (HS110, **HS300 with per-socket data**, KP115, KP125, KP125M, EP10, EP25, EP40). EP40M is supported as a device but lacks hardware emeter — `energy` against EP40M SHALL exit code 5 (unsupported feature).
- **Be scriptable**: deterministic exit codes, JSON/JSONL output, no interactive prompts in non-tty mode
- **Group devices logically** via local config (alias-to-IP map and group-to-alias-list map)
- **Run batch operations** across multiple devices in parallel
- **Cache authentication tokens** for KLAP-era devices to avoid per-command re-auth latency

### 2.2 Non-Goals

- **No GUI.** This is a CLI. Visual dashboards belong elsewhere.
- **No scheduling daemon.** Cron, systemd timers, and launchd handle scheduling. The CLI exposes a verb; the scheduler invokes it.
- **No cloud relay.** Local LAN only. No port forwarding. No TP-Link cloud control plane.
- **No automation rules engine.** "If door opens, turn on light" is Home Assistant or Node-RED territory, not this tool.
- **No Matter or Thread support.** Different protocol stack entirely.
- **No Tapo-line support in v1.** Brand-adjacent but protocol-divergent. Reserved for Phase 4.
- **No device-side schedule editing.** Read-only listing of device-stored schedules is the v1 ceiling.

---

## 3. Background and Prior Art

### 3.1 python-kasa

The canonical open-source library for Kasa devices is **python-kasa** (https://github.com/python-kasa/python-kasa), latest stable **0.10.2** (released **2025-02-12** per PyPI; master HEAD as of 2026-04-27 still at 0.10.2 — no tagged release in 14 months). It supports the legacy TP-Link Smart Home Protocol (XOR-obfuscated payloads on TCP/UDP) and the newer Smart/KLAP authenticated protocol introduced in post-2022 firmware, and ships its own command-line tool named `kasa`. Device family coverage verified against the upstream README and `SUPPORTED.md`:

| Family | Examples | Type |
|--------|----------|------|
| HS-series | HS100, HS103, HS105, HS107, HS110, HS200, HS210, HS220, **HS300** | Plugs, switches, dimmers, multi-socket strips |
| KP-series | KP100, KP105, KP115, KP125, **KP125M**, KP200, KP303, KP400, KP401, KP405 | Plugs, power strips |
| KL-series | KL50, KL60, KL110, KL110B, KL120, KL125, KL130, KL135, KL400L5, KL400L10, KL420L5, KL430 | Bulbs, light strips |
| EP-series | EP10, EP25, EP40, EP40M | Outdoor plugs, strips (note: EP40M lacks hardware emeter despite being supported) |
| KS-series | KS200, KS200M, KS205, KS220, KS220M, KS225, KS230, KS240 | Wall switches |
| ES-series | ES20M | Wall switches |
| KH-series | KH100 hub + KE100 hub-attached thermostat valve | Hubs and hub-attached |

The library also supports a substantial subset of TP-Link Tapo devices (P-series plugs, L-series bulbs and light strips, S-series switches, H-series hubs, plus Tapo cameras / doorbells / vacuums) but the Tapo line is explicitly out of scope for v1 of this CLI.

**Upstream cadence note.** python-kasa has not had a tagged release in 14 months as of this SRD. If a new device family or KLAP variant ships and 0.10.2 stops classifying it correctly (see open issues #1648, #1691 for KLAP login_version=2 misclassification on multi-socket strips), the wrapper SHOULD pin to a git SHA on master rather than wait for a tagged release. Section 4.2's "upstream patches firmware churn for free" argument is contingent on this.

### 3.2 The shipped `kasa` CLI

`python-kasa` already provides a working command-line tool (`kasa`) with subcommands for discover, on, off, brightness, energy, etc. It is functional but has three properties that motivate a thin wrapper rather than direct use:

1. **Output formatting is human-oriented.** JSON output exists but is not the default; flag conventions are inconsistent across subcommands; jsonl streaming is not native.
2. **No alias/group resolution layer.** Targets are IPs or MACs. There is no "turn off the bedroom-lights group" idiom.
3. **No first-class credential resolver.** Credentials come from `--username`/`--password` flags or `KASA_USERNAME`/`KASA_PASSWORD` env vars. No persistent credential file with sane permissions, no per-device credential override, no cached auth tokens persisted between invocations.

### 3.3 Home Assistant integration

Home Assistant ships a Kasa integration that is the gold standard for in-home automation but is the wrong tool for shell scripting. It requires a running HA instance, configuration via UI, and exposes devices through HA's entity model rather than directly. It is mentioned here only to delineate scope: `kasa-cli` is for users who want shell-native control without standing up a home-automation platform.

### 3.4 Why a thin custom CLI is justified

The combination of (a) alias/group resolution, (b) per-device credential override and a chmod-0600 credential file, (c) consistent JSON/JSONL output across all verbs, (d) parallelized batch operations with structured failure reporting, and (e) persistent KLAP token caching makes a wrapper materially more useful for shell scripting than direct `kasa` invocation. The wrapper does not duplicate protocol work; it adds a config-and-output layer on top of a maintained protocol library.

---

## 4. Architecture Decision: Wrap vs Reimplement

### 4.1 Decision

**Wrap python-kasa 0.10.2.** Do not reimplement the protocol layer.

### 4.2 Rationale

| Factor | Wrap python-kasa | Reimplement protocol |
|--------|------------------|----------------------|
| Legacy XOR protocol | Free | ~200 lines + edge cases |
| KLAP handshake | Free, maintained | ~600 lines crypto + ongoing churn |
| Smart protocol JSON envelope | Free | ~400 lines + per-device variance |
| Device family coverage | 50+ models verified | Ship for 5, hope for the rest |
| Firmware churn response time | Upstream patches | We patch every regression |
| Auth token format changes | Absorbed upstream | We chase breakage |
| v1 ship time | Days | Weeks-to-months |
| Long-term maintenance burden | Track minor version bumps | Own a protocol stack forever |

KLAP authentication in particular involves a specific handshake sequence with HKDF-derived session keys and per-message AEAD encryption; reimplementing it correctly across the device family matrix is months of work that adds zero user value when a maintained reference implementation exists.

### 4.3 Implementation language

**Recommendation: Python with `uv` for dependency and tool management.** Reasoning:

- `python-kasa` is a Python library; using it from Python is idiomatic and avoids a process-boundary tax on every command
- Dan's stack guidance permits Python where the upstream ecosystem dominates — Kasa is exactly that case
- `uv tool install kasa-cli` gives single-command global install with isolated venv, matching Dan's existing tool-install patterns
- Per-command latency is dominated by network discovery and device round-trips, not language startup; the Python startup cost (≈80ms cold) is acceptable

### 4.4 Considered alternative: Bun-TS shell-out wrapper

A Bun TypeScript wrapper that shells out to `python-kasa` via `uv run` was considered for stack consistency. Rejected because:

- Each invocation pays both Python startup AND a process-spawn round-trip — roughly 2x the latency floor
- Token caching across invocations becomes harder (where does the TS process stash KLAP tokens? in a file the Python helper also reads?)
- Two languages to maintain for a single tool

If stack-uniformity becomes a requirement later, the same CLI surface can be re-fronted in Bun-TS shelling out to a Python `kasa-cli-rpc` daemon. That is a Phase 5+ concern, not v1.

---

## 5. Functional Requirements

Each FR is atomic and independently testable.

### 5.1 Discovery

- **FR-1:** `kasa-cli discover` SHALL broadcast on UDP port 9999 (legacy Smart Home Protocol) and emit any responding devices.
- **FR-2:** `kasa-cli discover` SHALL broadcast on UDP ports 20002 AND 20004 (KLAP/Smart discovery; both are used by python-kasa's `Discover.discover()`) and emit any responding devices.
- **FR-3:** Discovery SHALL complete within `--timeout` seconds (default 3s) and SHALL aggregate responses across all three ports into a single result set.
- **FR-4:** Discovery output SHALL include device alias, IP, MAC, model, hardware version, firmware version, and protocol family (`iot` or `smart`/`klap`).
- **FR-5:** Discovery SHALL be invokable with `--target-network <CIDR>` to constrain broadcast to a specific subnet. The CIDR's directed-broadcast address SHALL be passed to python-kasa's `Discover.discover(target=...)` parameter.
- **FR-5a:** Discovery completing within timeout with **zero responding devices** SHALL exit 0 with empty output (`[]` in `--json`/`--jsonl` modes; empty stdout in text mode) and emit a single INFO log line to stderr stating "timeout reached, 0 devices found." Exit code 3 (network error) SHALL be reserved for cases where the broadcast itself failed (no usable interface, socket bind error, permission denied).
- **FR-5b:** On macOS, `socket.SO_BINDTODEVICE` is not available, so without `--target-network` the OS chooses the broadcast interface. On hosts with multiple interfaces (Wi-Fi + Tailscale + Docker bridges), discovery MAY send on the wrong interface and miss devices. The CLI SHALL document this in `--help` for `discover` and recommend `--target-network <CIDR>` on multi-NIC hosts.

### 5.2 Listing

- **FR-6:** `kasa-cli list` SHALL print every alias defined in the local config file with its resolved IP/MAC. By default, list does **not** issue a per-device probe — output reflects config-resolved data only.
- **FR-6a:** `kasa-cli list --probe` SHALL additionally probe each device for liveness within `--timeout` and include an `online: bool` field.
- **FR-6b:** List output in `--json`/`--jsonl` mode SHALL be a JSON array of list-view objects: `{alias, ip, mac, online: bool|null}` where `online` is `null` if `--probe` was not specified.
- **FR-7:** `kasa-cli list --groups` SHALL print every group defined in config with its member alias list.
- **FR-8:** `kasa-cli list --online-only` SHALL imply `--probe` and filter the output to devices that responded.

### 5.3 Info

- **FR-9:** `kasa-cli info <target>` SHALL issue a live `update()` against the device and print full device state including alias, model, firmware, on/off state, child sockets (for strips), and feature flags (dimmable, color, energy-monitor).
- **FR-10:** Info output in `--json` mode SHALL be a single JSON object matching the full Device record per §10.1, with stable key names across firmware versions.

### 5.4 Control

- **FR-11:** `kasa-cli on <target>` SHALL turn the device on and exit 0 on confirmed success.
- **FR-12:** `kasa-cli off <target>` SHALL turn the device off and exit 0 on confirmed success.
- **FR-13:** `kasa-cli toggle <target>` SHALL flip the current on/off state and exit 0 on confirmed success.
- **FR-14:** Control verbs SHALL be idempotent — calling `on` on an already-on device SHALL exit 0 without error.
- **FR-15:** For multi-socket strips (KP303, KP400, EP40, **HS300** — the v1 multi-socket set), control verbs SHALL **require** an explicit `--socket <n>` (1-indexed) OR `--socket all` flag. Invocation against a multi-socket strip without either SHALL exit code 64 with an error listing the available sockets and their aliases. Single-socket devices SHALL accept `--socket 1` or no `--socket` flag interchangeably; any other value SHALL exit code 64.
- **FR-15a:** Rationale (informative): defaulting to "all sockets" was rejected because turning off a router or always-on appliance plugged into one socket of a strip is an unrecoverable operator error. Explicit-target is the safer default.

### 5.5 Brightness, Color, Color-Temperature

- **FR-16:** `kasa-cli set <target> --brightness <0-100>` SHALL set brightness on dimmable devices.
- **FR-17:** `kasa-cli set <target> --color-temp <kelvin>` SHALL set color temperature on tunable-white devices, clamped to device-supported range.
- **FR-18:** `kasa-cli set <target> --hsv <h,s,v>` SHALL set hue/saturation/value on color-capable bulbs.
- **FR-19:** `kasa-cli set <target> --hex <#rrggbb>` SHALL accept hex color and convert to HSV before sending.
- **FR-19a:** `kasa-cli set <target> --color <name>` SHALL accept a named color drawn from a built-in name→HSV table. The table SHALL include at minimum: `warm-white`, `cool-white`, `daylight`, `red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `magenta`, `pink`. Unknown names SHALL exit with code 64 (usage error) and list the supported names.
- **FR-19b:** The named-color table SHALL be defined in code (not config) in v1 to keep behavior identical across machines. User-defined color aliases are deferred to a future phase.
- **FR-20:** `set` SHALL reject flags incompatible with target device features and exit with code 5 (unsupported-feature). `--hsv`, `--hex`, and `--color` are mutually exclusive — supplying more than one SHALL exit with code 64.

### 5.6 Energy

- **FR-21:** `kasa-cli energy <target>` SHALL emit a single Reading object (per §10.3) for energy-monitoring devices: `current_power_w` (float, instantaneous watts from python-kasa's `current_consumption`), `voltage_v` (float, volts), `current_a` (float, amps), `today_kwh` (float, kWh — may be `null` if device requires a separate `get_daystat` call that fails or times out), `month_kwh` (float, kWh — same nullable contract).
- **FR-21a:** `current_power_w`, `voltage_v`, and `current_a` are populated from python-kasa's normalized `Energy` module live values. `today_kwh`/`month_kwh` are populated via `get_daystat` / `get_monthstat`; these add ~200ms to the call. Use `--no-cumulative` to skip them when in `--watch` mode.
- **FR-22:** `kasa-cli energy <target> --watch <seconds>` SHALL emit a JSONL stream of Reading objects at the specified interval. By default `--watch` SHALL omit `today_kwh`/`month_kwh` (pass `--cumulative` to include them; this lengthens each tick by ~200ms).
- **FR-23:** Energy on a non-energy-monitoring device (including EP40M, which is supported but lacks hardware emeter) SHALL exit with code 5.

### 5.7 Schedule (Read-Only)

- **FR-24:** `kasa-cli schedule list <target>` SHALL print device-side schedule entries on **legacy IOT devices** (via python-kasa's `Schedule` / `RuleModule.rules`). Output is a JSON array per `--json`/`--jsonl` of rule objects (id, enabled, time spec, action).
- **FR-24a:** For Smart/KLAP devices, `schedule list` SHALL exit with code 5 (unsupported feature) and an error stating "python-kasa 0.10.2 does not expose schedule listing for KLAP/Smart-protocol devices; revisit when upstream adds a `Schedule` module to `kasa/smart/modules/`."
- **FR-25:** v1 SHALL NOT support creating, editing, or deleting device-side schedules on **any** protocol family. Read-only forever per Decision 3.

### 5.8 Groups

- **FR-26:** Groups SHALL be defined locally in the CLI config file's `[groups]` table, NOT on the devices themselves.
- **FR-27:** A group target (`@group-name` or `--group group-name`) SHALL resolve to its member aliases at command execution time.
- **FR-28:** Group operations SHALL execute device commands in parallel up to a configurable concurrency limit (default 10; per-command override via `--concurrency N`).
- **FR-29:** Group operations SHALL report per-device success/failure individually; a single device failure SHALL NOT abort the group operation.
- **FR-29a:** Group exit code SHALL be:
  - **0** if every sub-operation succeeded
  - **7** (partial failure) if at least one sub-operation succeeded AND at least one failed
  - The exit code of the first sub-operation failure if every sub-operation failed (e.g., all devices unreachable → 3; all unauthorized → 2)
- **FR-29b:** v1 SHALL NOT support `groups add` / `groups remove` sub-verbs that mutate the config file. Comment-preserving TOML round-trip is non-trivial and out of scope. `kasa-cli groups list` is the only group sub-verb in v1; mutations are by hand-editing the config.

### 5.9 Batch

- **FR-30:** `kasa-cli batch --file <path>` SHALL read newline-delimited commands from a file and execute them, emitting one JSONL result per line on stdout.
- **FR-31:** `kasa-cli batch --stdin` SHALL accept the same format from stdin for shell-pipe composability.
- **FR-31a:** Batch exit code semantics SHALL match FR-29a (0 / 7 / first-failure-code).
- **FR-31b:** Empty-input batch (`batch --stdin < /dev/null`, or `--file` against an empty file) SHALL exit 0 with no stdout output (`[]` in `--json` mode). Blank lines in the input SHALL be skipped silently. Lines beginning with `#` SHALL be treated as comments and skipped.
- **FR-31c:** On SIGINT or SIGTERM during batch or group execution, the CLI SHALL: (1) cease dispatching new sub-operations, (2) wait up to 2 seconds for in-flight sub-operations to complete and have their results emitted, (3) emit a final JSONL summary line `{"event":"interrupted","completed":N,"pending":M}` to stdout, (4) flush the token cache to disk for any successfully-authenticated device, (5) exit with code **130** (SIGINT) or **143** (SIGTERM).

### 5.10 Output Formats

- **FR-32:** Default output SHALL be human-readable text on a tty, JSONL when stdout is a pipe.
- **FR-33:** `--json` SHALL force pretty JSON output regardless of tty detection.
- **FR-34:** `--jsonl` SHALL force one-JSON-per-line output regardless of tty detection.
- **FR-35:** `--quiet` SHALL suppress all stdout output; only the exit code communicates result.
- **FR-35a:** In `--json` and `--jsonl` modes, on **any** non-zero exit, stdout SHALL be valid parseable JSON or empty. The CLI SHALL never emit malformed JSON. For batch and group operations with mixed results, stdout JSONL SHALL contain one result object per attempted operation including those that failed (each with its own `error` field per §11.2). Stderr SHALL emit the structured summary error per §11.2 once.

### 5.11 Error Handling

- **FR-36:** Network errors SHALL exit with code 3 and emit a structured error object to stderr.
- **FR-37:** Authentication failures SHALL exit with code 2 and a credential-source hint.
- **FR-38:** Unknown alias or unreachable IP SHALL exit with code 4.
- **FR-39:** Verbose mode (`-v`, `-vv`) SHALL emit progressively detailed JSON-structured logs to stderr; stdout SHALL remain clean.

### 5.12 Configuration Resolution

- **FR-40:** Config file resolution order: (1) `--config <path>` flag if present, (2) `KASA_CLI_CONFIG` env var if set and non-empty, (3) `~/.config/kasa-cli/config.toml` if it exists.
- **FR-40a:** If `--config` or `KASA_CLI_CONFIG` is set and the referenced file does not exist or cannot be read, the CLI SHALL exit code 6 (config error). Silent fallback is forbidden — explicit selection means strict.
- **FR-40b:** If only the default location is consulted and it does not exist, the CLI SHALL operate with built-in defaults and emit a single INFO log line on stderr ("no config file found, using defaults"). This SHALL NOT be an error.
- **FR-40c:** `kasa-cli config show` SHALL print the effective resolved config (after all overrides) in TOML format. `kasa-cli config validate [<path>]` SHALL load and validate a config file and exit 0 / 6.

---

## 6. Authentication and Credentials

### 6.1 The KLAP wrinkle

Post-2022 Kasa firmware introduced KLAP authentication: even on the local LAN, the device requires the user's TP-Link cloud account email and password to derive a session key for control. This is a real, non-removable property of the device firmware. Older firmware (pre-KLAP, typically pre-2022 production runs) is unauthenticated on LAN. The CLI MUST handle both cases without forcing users to think about which is which.

### 6.2 Credential sources (in resolution order)

1. **Per-target override:** Target-specific credential entry in config (`config.devices.<alias>.credential_file`) pointing at an alternate credentials file
2. **Environment variables:** `KASA_USERNAME` and `KASA_PASSWORD`
3. **Default credentials file:** `~/.config/kasa-cli/credentials` (chmod 0600 enforced, JSON `{"username": "...", "password": "..."}`)
4. **No credentials:** legacy-protocol path only — fails with exit code 2 if device requires KLAP

External credential managers (1Password, `pass`, `bw`) are explicitly **not** in scope for v1. Users who want their TP-Link cloud password kept in a vault can wire a wrapper script that materializes the credentials file before invoking `kasa-cli` (e.g., `op read ... > ~/.config/kasa-cli/credentials && kasa-cli ...`).

### 6.3 Credentials file format

- **FR-CRED-1:** The default credentials file SHALL be JSON with a top-level `version` integer (currently `1`) plus the keys appropriate for that version. v1 keys: `username` (TP-Link cloud email), `password`. Unknown additional keys SHALL cause a config-validation error and exit code 6. A missing `version` field SHALL be treated as v1 with a single deprecation warning on stderr; subsequent versions SHALL ship with a `kasa-cli auth migrate` sub-verb that rewrites older files in place.
- **FR-CRED-2:** The CLI SHALL refuse to load a credentials file whose mode is more permissive than 0600 (group or world readable/writable) and SHALL exit with exit code 2 and an actionable error showing the current mode.
- **FR-CRED-3:** A missing credentials file SHALL fall through to the next source (env vars) without warning. Verbose mode (`-v`) SHALL log the fall-through with the path that was tried.

### 6.4 KLAP session caching

python-kasa already implements KLAP session-key derivation, expiration via the device's `TIMEOUT` HTTP cookie from the handshake1 response (24-hour fallback if the cookie is absent), and a 20-minute safety buffer (`SESSION_EXPIRE_BUFFER_SECONDS`) subtracted from the cookie value. The CLI's job is to **persist enough state across invocations to honor python-kasa's expiration**, not to re-derive TTL math.

- **FR-CRED-4:** Successful KLAP auth SHALL persist the session state (local seed, remote seed, auth_hash, computed `_session_expire_at`) to `~/.config/kasa-cli/.tokens/<device-mac>.json` with chmod 0600. The `.tokens/` directory SHALL be created with chmod 0700 on first use.
- **FR-CRED-5:** Subsequent commands against the same device SHALL deserialize the cached state into python-kasa's `KlapTransport`, which will reuse the session if `time.monotonic() < _session_expire_at` and trigger a fresh handshake otherwise.
- **FR-CRED-6:** Cache expiration MUST honor python-kasa's `_session_expire_at` value directly; the CLI SHALL NOT introduce a separate TTL policy. If a stored expiration is in the past at deserialization time, the cache entry SHALL be treated as a miss.
- **FR-CRED-7:** A 401-equivalent or KLAP `_AUTH_FAILED` response from the device during a command SHALL invalidate the cached state and trigger a single retry with a fresh handshake. Two consecutive auth failures SHALL exit code 2 with a hint to verify the credentials file.
- **FR-CRED-8:** `kasa-cli auth flush` SHALL delete all cached state files. `kasa-cli auth flush --target <alias>` SHALL delete only that device's state file.
- **FR-CRED-9:** Config schema SHALL allow `[devices.<alias>] credential_file = "<path>"` to use a different credentials file for one device (e.g., a guest device on a separate TP-Link account). The override file SHALL be subject to the same chmod-0600 check as the default file.
- **FR-CRED-10:** Concurrent `kasa-cli` invocations targeting the same KLAP device SHALL serialize on a per-device advisory lock (`flock` on the token state file) to preserve KLAP sequence-counter integrity. Cache writes SHALL use atomic file replacement (`write tmpfile + fsync + rename`). A second concurrent invocation that fails to acquire the lock within `--timeout` SHALL exit code 3 with a hint to retry. Token reads do not require the lock — only writes and the auth-renew path do.

### 6.5 `auth status`

- **FR-CRED-11:** `kasa-cli auth status` SHALL emit, per cached state file: device alias (resolved from config; `<unmapped>` if no alias matches the MAC), MAC, cache file mtime, file size in bytes, and the cached `_session_expire_at` translated to wall-clock UTC. `--json` mode emits a JSON array of these objects. The CLI SHALL NOT issue liveness probes against cached devices in `auth status` — that is `list --probe`'s job.

---

## 7. Non-Functional Requirements

### 7.1 Performance

Targets assume a wired LAN or 5GHz Wi-Fi with <50ms RTT to the device. Networks with >100ms p95 device RTT (mesh Wi-Fi with retransmits, distant access points) SHALL NOT be measured against these targets — they are not contractual on degraded networks.

| Metric | Target |
|--------|--------|
| Discovery (broadcast complete) | < 3 seconds with default timeout |
| Single-device command (cached KLAP session) | < 500ms p95 |
| Single-device command (cold KLAP — handshake1 + handshake2 + command, three RTTs) | < 2500ms p95 |
| Single-device command (legacy IOT, no auth) | < 500ms p95 |
| Batch of 10 devices, parallel, all cached or legacy | < 2 seconds p95 |
| Cold CLI startup (no command, just `--help`) | < 200ms |

### 7.2 Determinism

- Commands SHALL be idempotent where physically possible (on/on/on is the same as on)
- Identical input SHALL produce identical output structure (JSON key set is stable)
- No interactive prompts when stdin/stdout are not ttys

### 7.3 Observability

- `-v` enables INFO-level structured logs to stderr
- `-vv` enables DEBUG-level logs including raw protocol frames (with credentials redacted)
- All log lines in verbose mode are single-line JSON
- Optional file logging: when `[logging] file = "<path>"` is set in config, the same JSON log lines SHALL be tee'd to that file (append mode, line-buffered). stderr emission SHALL continue regardless. Useful for capturing cron output without redirect plumbing in every cron line.
- File logging SHALL NOT rotate in v1 — operators are expected to use `logrotate` or a `[logging] max_bytes` cap (deferred to v1.1 if it becomes a problem).

### 7.4 Portability

- Supported platforms: macOS 13+ (Apple Silicon and Intel), Linux x86_64 and arm64
- Python 3.11+ required (matches python-kasa minimum)
- No Windows support in v1

### 7.5 Network model

- All `kasa-cli` operations SHALL be fully local-network. The CLI SHALL NOT make outbound connections to TP-Link servers under any code path. KLAP authentication uses cloud-account *credentials* but performs the handshake against the device on the LAN — no internet egress is required.
- DNS unreachable SHALL NOT block any operation (the CLI uses raw IPs from config or discovery; alias resolution does not require DNS).
- A KLAP device unreachable on the LAN SHALL exit code 3 (network error) with the device address in the error message — not code 2 (auth) — even if the failure mode is a TCP reset during handshake.

---

## 8. CLI Surface

### 8.1 Verb summary

| Verb | Purpose |
|------|---------|
| `discover` | Broadcast probe both protocol families |
| `list` | Print configured aliases and groups |
| `info` | Show full state of one target |
| `on` | Power on |
| `off` | Power off |
| `toggle` | Flip on/off state |
| `set` | Brightness, color, color-temp |
| `energy` | Power consumption readings |
| `schedule` | Read-only schedule listing (sub-verb: `list`; legacy IOT only — KLAP/Smart returns code 5) |
| `groups` | List local group definitions (sub-verb: `list` only in v1; mutations by editing the config) |
| `batch` | Execute commands from file or stdin |
| `config` | `show` (effective config) and `validate` (lint a config file) |
| `auth` | Session-cache management (sub-verbs: `flush`, `status`, `migrate`) |

### 8.2 Target syntax

A target is one of:
- An **alias** defined in config (e.g., `kitchen-lamp`)
- An **IP address** (e.g., `192.168.1.42`)
- A **MAC address** (e.g., `AA:BB:CC:DD:EE:FF`)
- A **group name** prefixed with `@` (e.g., `@bedroom-lights`)
- The literal `all` to target every alias in config

### 8.3 Common flags

| Flag | Meaning |
|------|---------|
| `--json` | Pretty JSON output |
| `--jsonl` | Newline-delimited JSON output |
| `--quiet` | Suppress stdout |
| `--timeout <seconds>` | Per-operation timeout, default 5 |
| `--credential-source env\|file\|none` | Override config credential resolver (no `1pw` in v1 — see Decision 1). `none` skips KLAP-protocol devices. |
| `--config <path>` | Use a non-default config file (precedence rules per FR-40) |
| `--concurrency N` | Override `[defaults] concurrency` for this invocation only |
| `--cumulative` / `--no-cumulative` | Include / skip `today_kwh` and `month_kwh` on `energy --watch` (default: skip in `--watch`, include otherwise) |
| `--probe` | On `list`, additionally probe each device for liveness |
| `-v`, `-vv` | Verbose / very verbose stderr logging |

### 8.4 Worked examples

```text
# Discover everything on the LAN
$ kasa-cli discover
kitchen-lamp     192.168.1.42   AA:BB:CC:DD:EE:01  KL130    legacy   on
office-strip     192.168.1.51   AA:BB:CC:DD:EE:02  HS300    legacy   on
patio-plug       192.168.1.78   AA:BB:CC:DD:EE:03  EP25     klap     off

# JSON form
$ kasa-cli discover --json
[
  { "alias": "kitchen-lamp", "ip": "192.168.1.42", "mac": "...", "model": "KL130", "protocol": "legacy", "state": "on" },
  ...
]

# Turn off all configured plugs
$ kasa-cli off all

# Turn off a logical group
$ kasa-cli off @bedroom-lights

# Set bulb to warm white at 30% brightness
$ kasa-cli set kitchen-lamp --brightness 30 --color-temp 2700

# Stream live power consumption from an energy-monitoring strip (per-socket on HS300)
$ kasa-cli energy office-strip --socket 2 --watch 5
{"ts":"2026-04-27T20:11:00Z","alias":"office-strip","socket":2,"current_power_w":42.1,"voltage_v":120.2,"current_a":0.35}
{"ts":"2026-04-27T20:11:05Z","alias":"office-strip","socket":2,"current_power_w":41.8,"voltage_v":120.1,"current_a":0.35}

# Same stream, including cumulative (slower per tick — adds ~200ms)
$ kasa-cli energy office-strip --socket 2 --watch 5 --cumulative

# Run a list of commands from a file
$ cat night.batch
off @living-room
off @kitchen
set bedroom-lamp --brightness 5 --color-temp 2200
$ kasa-cli batch --file night.batch --jsonl

# Inspect cached KLAP sessions
$ kasa-cli auth status --json
[
  {"alias":"patio-plug","mac":"AA:BB:CC:DD:EE:03","mtime":"2026-04-27T18:02:14Z","bytes":312,"expires_at":"2026-04-28T17:42:14Z"},
  {"alias":"<unmapped>","mac":"AA:BB:CC:DD:EE:11","mtime":"2026-04-26T11:30:01Z","bytes":312,"expires_at":"2026-04-27T11:10:01Z"}
]

# Flush all sessions
$ kasa-cli auth flush

# Show effective config (after --config / KASA_CLI_CONFIG / default precedence)
$ kasa-cli config show

# Lint a candidate config without loading it as the active one
$ kasa-cli config validate /tmp/new-config.toml
```

---

## 9. Configuration File

### 9.1 Location and format

Default path: `~/.config/kasa-cli/config.toml` (override via `--config` or `KASA_CLI_CONFIG` env var).

Format: TOML. Chosen over YAML for unambiguous parsing, no significant-whitespace footguns, and consistent comment syntax.

### 9.2 Schema

| Section | Field | Type | Default | Purpose |
|---------|-------|------|---------|---------|
| `[defaults]` | `timeout_seconds` | int | 5 | Per-operation timeout |
| `[defaults]` | `concurrency` | int | 10 | Max parallel device ops in groups/batch |
| `[defaults]` | `output_format` | string | `auto` | `auto`/`text`/`json`/`jsonl` |
| `[credentials]` | `file_path` | string | `~/.config/kasa-cli/credentials` | Default credentials file (chmod 0600) |
| `[logging]` | `file` | string | — | Optional path; when set, JSON log lines are tee'd here |
| `[devices.<alias>]` | `ip` | string | — | Static IP (skips discovery) |
| `[devices.<alias>]` | `mac` | string | — | MAC for stable identification |
| `[devices.<alias>]` | `credential_file` | string | — | Per-device credentials file override |
| `[groups]` | `<name>` | string[] | — | Array of alias names |

### 9.3 Complete example

```toml
# ~/.config/kasa-cli/config.toml

[defaults]
timeout_seconds = 5
concurrency = 10
output_format = "auto"

[credentials]
file_path = "~/.config/kasa-cli/credentials"

[logging]
# Optional. Comment out to disable file logging.
# file = "~/.local/state/kasa-cli/log"

[devices.kitchen-lamp]
ip = "192.168.1.42"
mac = "AA:BB:CC:DD:EE:01"

[devices.office-strip]
ip = "192.168.1.51"
mac = "AA:BB:CC:DD:EE:02"

[devices.patio-plug]
ip = "192.168.1.78"
mac = "AA:BB:CC:DD:EE:03"
credential_file = "~/.config/kasa-cli/credentials.guest"

[devices.bedroom-lamp]
ip = "192.168.1.91"
mac = "AA:BB:CC:DD:EE:04"

[devices.hallway-strip]
ip = "192.168.1.92"
mac = "AA:BB:CC:DD:EE:05"

[groups]
bedroom-lights = ["bedroom-lamp", "hallway-strip"]
outdoor        = ["patio-plug"]
night-off      = ["kitchen-lamp", "office-strip", "patio-plug"]
```

### 9.4 Config validation

`kasa-cli config validate` SHALL parse the file, resolve every alias-to-device reference, resolve every group-to-alias reference, and exit 0 only if all references resolve.

---

## 10. Data Model

### 10.1 Device

```text
Device {
  alias              : string         # human-friendly name (from config or device-stored alias)
  ip                 : string         # IPv4 dotted quad
  mac                : string         # uppercase colon-separated
  model              : string         # e.g., "HS110", "KL130"
  hardware_version   : string         # device-reported
  firmware_version   : string         # device-reported
  protocol           : "legacy" | "klap"
  features           : string[]       # e.g., ["dimmable", "color", "color-temp", "energy-monitor"]
  state              : "on" | "off" | "mixed"   # mixed only for multi-socket strips
  sockets            : Socket[]?      # populated for multi-socket strips
  last_seen          : ISO8601 string
}

Socket {
  index   : int           # 1-based
  alias   : string
  state   : "on" | "off"
}
```

### 10.2 Group

```text
Group {
  name    : string
  members : string[]      # alias names; resolved at runtime
}
```

### 10.3 Reading (energy)

Field names and units match python-kasa's `Energy` module API directly to minimize translation. The library normalizes raw-protocol millivolts/milliamps to floating-point volts/amps internally.

```text
Reading {
  ts                : ISO8601 string
  alias             : string
  socket            : int?           # 1-indexed socket number for HS300; null/omitted for single-socket devices
  current_power_w   : number         # instantaneous watts (float; from python-kasa Energy.current_consumption)
  voltage_v         : number         # volts (float; library-normalized)
  current_a         : number         # amps (float; library-normalized)
  today_kwh         : number?        # kWh, cumulative for current local day; nullable when --no-cumulative or fetch fails
  month_kwh         : number?        # kWh, cumulative for current local month; nullable when --no-cumulative or fetch fails
}
```

`current_power_w`, `voltage_v`, `current_a` come from a single live `update()` call (cheap — one round-trip). `today_kwh` and `month_kwh` require additional `get_daystat` / `get_monthstat` calls (~200ms extra) and are omitted by default in `energy --watch` to keep the loop tight; pass `--cumulative` to include them.

---

## 11. Error Model and Exit Codes

### 11.1 Exit code table

| Code | Meaning | When |
|------|---------|------|
| 0 | Success | Operation completed; for batch/group, **every** sub-op succeeded |
| 1 | Device error | Device returned an error response (non-auth, non-network) |
| 2 | Authentication error | KLAP auth failed, no credentials, credentials file mode too permissive |
| 3 | Network error | Timeout, connection refused, no route, broadcast bind failure, KLAP device unreachable on LAN, concurrent-lock acquisition timeout |
| 4 | Device not found | Alias unknown in config, IP unreachable, MAC not on LAN |
| 5 | Unsupported feature | Verb/flag combo not supported by target device family or firmware (e.g., `schedule list` on KLAP, `energy` on EP40M, `--color` on a non-color device) |
| 6 | Config error | Config file missing when `--config`/`KASA_CLI_CONFIG` was set, invalid TOML, unresolvable references, unknown keys in credentials file |
| 7 | Partial batch/group failure | ≥1 sub-op succeeded AND ≥1 sub-op failed (mixed result) |
| 64 | Usage error | Invalid CLI invocation: missing required arg (e.g., `--socket` on a multi-socket strip), mutually-exclusive flags (`--hsv` + `--hex`), unknown named color |
| 130 | SIGINT | Ctrl-C during execution; partial JSONL stream emitted with trailing `{"event":"interrupted",...}` line |
| 143 | SIGTERM | Process terminated; same partial-result + interrupted-line behavior as 130 |

When every sub-op of a batch/group fails for the **same** reason, the exit code SHALL be that reason's code (e.g., all devices unreachable → 3). Mixed-failure-reasons SHALL still exit 7 — the structured stderr error names the dominant failure.

### 11.2 Structured error object (stderr)

When stdout is JSON/JSONL or `--quiet` is set, errors are emitted to stderr as:

```json
{
  "error": "auth_failed",
  "exit_code": 2,
  "target": "patio-plug",
  "message": "KLAP handshake rejected; check credentials file",
  "hint": "Verify ~/.config/kasa-cli/credentials has correct username/password"
}
```

The `error` enum is closed and stable. Tooling MAY pattern-match on it.

---

## 12. Testing Strategy

### 12.1 Unit tests

- Mock device implementations covering one of each: HS-series single-socket plug, HS300 multi-socket strip (per-socket emeter), KP-series strip, KL-series color bulb, EP-series outdoor plug, EP40M (supported but no emeter — unsupported-feature path), KS-series switch
- Mock both legacy-protocol and KLAP-protocol response paths, including the KLAP `TIMEOUT` cookie path and the `_AUTH_FAILED` retry-once path (FR-CRED-7)
- Config parser tests with valid configs, invalid TOML, dangling alias refs, dangling group refs, missing `version` in credentials file (FR-CRED-1 deprecation warning), unknown keys (exit code 6)
- Output formatter tests asserting JSON key stability across mock devices, including: list-view subset (FR-6b), full Device record (FR-10), Reading with nullable cumulative fields (§10.3), structured error (§11.2)
- Exit-code matrix tests: every exit code 0/1/2/3/4/5/6/7/64/130/143 SHALL be reachable by at least one test; mixed-result group/batch SHALL produce 7 with the expected stdout/stderr shape
- Concurrency lock test (FR-CRED-10): two concurrent `kasa-cli` invocations against the same KLAP device — second SHALL block on `flock` and the cache SHALL remain valid afterward
- Credential resolver tests covering the three sources (per-target `credential_file`, env vars, default file) and the no-credentials fall-through path. No `op` binary involvement — Decision 1 excluded vault integration from v1.
- Signal handling test: SIGINT during a 10-element batch SHALL produce ≤10 result lines plus the `{"event":"interrupted",...}` line and exit 130

### 12.2 Integration tests

Gated on environment variable `KASA_TEST_DEVICE_IP`. When unset, integration tests are skipped (CI default). When set, the test suite runs against a real device on the operator's LAN. CI never sets this variable.

A second variable `KASA_TEST_KLAP_DEVICE_IP` enables KLAP-specific integration tests for users with both legacy and modern devices.

### 12.3 Fixture corpus

Capture real device responses (with MACs and credentials redacted) in `tests/fixtures/` to reproduce protocol-level edge cases without requiring hardware.

---

## 13. Distribution and Install

### 13.1 Recommended

```text
uv tool install git+ssh://git@github.com/agileguy/kasa-cli
```

Rationale: this is a personal tool. Installing directly from the git repo via `uv tool` keeps the install pattern Dan already uses for other Python CLIs (isolated venv, entry point on PATH, no system-Python conflicts) without the overhead of PyPI release management. Updates are `uv tool upgrade kasa-cli`.

### 13.2 Alternatives considered

- **Publish to PyPI** — rejected for v1. Personal scope; no consumers other than Dan; no value in claiming a public name.
- `pipx install git+...` — works identically but Dan's stack guidance prefers `uv`.
- `brew install kasa-cli` — would require maintaining a Homebrew tap; not warranted for a single-user tool.
- Single-file binary via PyInstaller — increases binary size 10x for marginal install simplification; not recommended.

### 13.3 Versioning

Tag releases as `vX.Y.Z` in git. `uv tool install git+ssh://...@vX.Y.Z` pins to a tag. No PyPI registry, no semver contract beyond what tags promise. If the tool ever needs to be shared more broadly, PyPI publication can be added later — the package metadata in `pyproject.toml` SHOULD be PyPI-ready (LICENSE, README, classifiers, project URLs) so the migration is free.

---

## 14. Out of Scope

The following are **explicitly excluded** from v1 to keep the scope honest:

- **Matter and Thread devices.** Different protocol stack. Different tool.
- **TP-Link Tapo line.** No Tapo support at any phase in this SRD. If demand emerges, a follow-on SRD will define Tapo scope; this document makes no Tapo commitment beyond the initial Phase 4 sketch in §16.4 (which is itself optional and explicitly scope-permitting).
- **Tapo cameras, doorbells, vacuums.** Out of scope at all phases — these are media/control surfaces, not switches.
- **Automation rules engine.** "If sunset, then dim lights" belongs in cron, systemd timers, or Home Assistant.
- **GUI dashboard.** This is a CLI. Visualizations consume the JSON output if needed.
- **Cloud relay control.** No remote-network control. Local LAN only. No outbound connections to TP-Link servers from any code path.
- **Device-side schedule editing.** Read-only listing only, legacy IOT only. Forever.
- **Energy historical aggregation.** v1 emits current and current-period readings. Time-series storage is the consumer's job.
- **Firmware updates.** Use the Kasa app.
- **Wi-Fi provisioning.** Use the Kasa app.
- **Multi-network discovery via mDNS reflectors.** Single-LAN broadcast only.
- **Group config mutation.** v1 `groups` sub-verb is `list` only. Add/remove are deferred — see §5.8 FR-29b.
- **Comment-preserving TOML round-trip on config writes.** Out of scope; v1 does not write user config files.

---

## 15. Resolved Decisions

The 10 open questions originally surfaced for sign-off were resolved on 2026-04-27 and are recorded here for traceability. A subsequent fact-check + architecture review pass on the same day produced additional spec clarifications now folded into §3.1, §5.1–§5.12, §6.4, §7.1, §7.5, §10.3, §11.1, §12.1, §14, and §16 — those clarifications do not change the 10 decisions below; they correct factual claims (e.g., python-kasa release date, KLAP TTL mechanism, energy field names, device matrix) and tighten testability gaps (group/batch exit codes, signal handling, concurrency lock, config precedence, --socket safety).

| # | Decision area | Outcome |
|---|--------------|---------|
| 1 | **Credential source** | Plain file only (`~/.config/kasa-cli/credentials`, JSON, chmod 0600). No 1Password / pass / bw integration in v1. Users who want vault-backed creds wrap the CLI with a script that materializes the file. |
| 2 | **Tapo support** | Out of scope at all phases of this SRD. A new SRD will define Tapo if/when wanted. v1 covers HS / KP / KL / EP / KS / ES / KH families per §3.1. |
| 3 | **Device-side schedule editing** | Read-only **forever**. Schedules belong in cron / systemd / launchd. No revisit planned. |
| 4 | **Token cache location** | `~/.config/kasa-cli/.tokens/` (co-located with config). NOT `~/.cache/`. |
| 5 | **Concurrency default** | 10 parallel ops with `--concurrency N` per-command override and `[defaults] concurrency` floor in config. |
| 6 | **Logging destination** | stderr by default; optional file logging via `[logging] file = "<path>"` in config. No rotation in v1. |
| 7 | **Discovery cache** | Always probe live. Users who want speed pin IPs in config aliases. No persistent discovery cache. |
| 8 | **Color naming** | Named colors **included in v1** alongside HSV and hex. Built-in name→HSV table in code (not config); minimum names: `warm-white`, `cool-white`, `daylight`, `red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `magenta`, `pink`. |
| 9 | **Distribution** | **Not published to PyPI.** Install from git: `uv tool install git+ssh://git@github.com/agileguy/kasa-cli`. Versioning via git tags. |
| 10 | **Windows support** | Out of scope. macOS 13+ and Linux x86_64/arm64 only. WSL is the answer for Windows users. |

---

## 16. Phase Plan

### 16.1 Phase 1 — MVP (1-2 weeks)

**Deliverable:** Discover, list, info, on, off for both legacy and KLAP devices.

- Project skeleton, `uv` packaging, entry point
- Config loader (TOML), `config show`, `config validate` (FR-40, FR-40a/b/c)
- python-kasa wrapper layer with alias-to-device resolution
- Verbs: `discover`, `list` (with `--probe`), `info`, `on`, `off` (with mandatory `--socket` on multi-socket strips per FR-15)
- Credential sources: env var and file with versioned-format support (FR-CRED-1..3)
- KLAP session caching to `~/.config/kasa-cli/.tokens/` honoring python-kasa's `_session_expire_at` (FR-CRED-4..8)
- Per-device session-cache locking (FR-CRED-10)
- Output: text (default), `--json`, structured-error contract (FR-35a, §11.2)
- Exit codes 0, 1, 2, 3, 4, 6, 64, 130, 143
- Discovery zero-result handling (FR-5a) and multi-NIC `--target-network` (FR-5, FR-5b)
- KH-series hubs and KE100 hub-attached devices: discoverable in Phase 1 but `info` returns parent-only data; child enumeration deferred to Phase 2 or later
- Unit tests with mock devices (per §12.1)

### 16.2 Phase 2 — State Control, Energy, and Schedule (1 week)

**Deliverable:** Toggle, set, energy (single + per-socket), named colors, schedule (legacy IOT), per-device credential override.

- Verbs: `toggle`, `set` (brightness, color-temp, hsv, hex, named-color)
- Built-in named-color table (FR-19a, FR-19b)
- Verb: `energy` with `--watch` JSONL streaming, per-socket support on HS300, `--cumulative`/`--no-cumulative` (FR-21..23)
- Per-device credential file override (FR-CRED-9)
- `auth status`, `auth flush`, `auth migrate` sub-verbs (FR-CRED-8, FR-CRED-11)
- Exit code 5 for unsupported features (e.g., `energy` on EP40M, `--color` on non-color device)
- `schedule list` for legacy IOT devices only — Smart/KLAP returns code 5 (FR-24, FR-24a)
- Optional file logging via `[logging] file` (§7.3)

### 16.3 Phase 3 — Groups and Batch (1 week)

**Deliverable:** Read-only group resolution, parallel batch operations, full output formats, signal handling.

- `groups list` sub-verb (mutations remain manual TOML edits — FR-29b)
- `@group` and `--group` target syntax resolution
- Parallel execution with concurrency cap (FR-28) + per-command `--concurrency`
- `batch` verb reading from `--file` and `--stdin`; comments and blank lines (FR-31b)
- `--jsonl` output format finalized; mixed-result JSON-validity contract (FR-35a)
- Exit code 7 for mixed-result batch/group failures (FR-29a, FR-31a)
- SIGINT/SIGTERM handling with `{"event":"interrupted",...}` summary line (FR-31c)
- Per-device-result reporting in JSON

### 16.4 Phase 4 — (Reserved, no commitment)

There is **no Phase 4 deliverable** in this SRD. If TP-Link Tapo, child-device enumeration on KH100 hubs, or any other major scope expansion is wanted post-v1, a new SRD will define it. This section exists as a placeholder so phase numbering is stable; it does not promise work.

---

**End of document.**
