# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-04-27

Phase 2 per [docs/SRD-kasa-cli.md](docs/SRD-kasa-cli.md) §16.2: state control, energy, schedule, named colors, runtime file-logging tee.

### Added
- **Toggle** (FR-13, FR-15): `kasa-cli toggle <target>` flips on/off. Multi-socket strips REQUIRE `--socket N` or `--socket all`; `--socket all` flips each child independently (mixed-state strip ends up inverted per-socket).
- **Set** (FR-16..20): `kasa-cli set <target>` with mutually-exclusive `--brightness`, `--color-temp`, `--hsv`, `--hex`, `--color`. Per-socket support via `--socket N` / `--socket all`. Capability gating via `Module.Light` produces exit 5 on incompatible flags.
- **Named-color table** (FR-19a, FR-19b): 12 built-in names — `warm-white`, `cool-white`, `daylight`, `red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `magenta`, `pink`. Compiled into `colors.py` (not config). HSV-only constraint documented.
- **Energy** (FR-21..23): `kasa-cli energy <target>` emits a Reading per §10.3. `--watch <seconds>` streams JSONL per-tick (flushed); `--cumulative` / `--no-cumulative` controls `today_kwh`/`month_kwh` inclusion. HS300 per-socket via `--socket N`; strip-total fallback sums children when parent has no `Module.Energy`. EP40M raises exit 5 with a model-specific hint.
- **Schedule list** (FR-24, FR-24a, FR-25): `kasa-cli schedule list <target>` reads device-stored rules via `Module.IotSchedule` (legacy IOT only). KLAP/Smart devices exit 5 with the SRD-mandated message verbatim. No add/remove/edit ever (FR-25).
- **Runtime file-logging tee** (§7.3): `[logging] file = "<path>"` in config attaches a JSON-line `FileHandler` to `kasa_cli` logger alongside the stderr handler. Idempotent across repeated invocations; closes prior FileHandlers cleanly.
- **Color-temp plausibility guard** (R3): `--color-temp` outside `[1000, 12000]K` raises exit 64 with a typo hint (e.g. `27000K → did you mean 2700K?`).

### Fixed
- **FR-20 exit code**: `--hsv`/`--hex`/`--color` mutex and `--brightness` range checks now exit 64 (was 2 — Click's default). `_Exit64UsageError` Click subclass routes through the standard structured-stderr path. Tests updated from `!= 0` to `== 64` (the same bridge-fallback false-pass anti-pattern that hit Phase 1).
- **`energy --watch` streaming**: JSONL/TEXT modes now flush per tick instead of buffering until loop exit. Production `--watch` was previously silent until Ctrl-C; tests passed only via the hidden `_max_ticks` hook.
- **HS300 strip-total voltage semantics**: docstring/code reconciled to "last non-zero wins"; tests now assert voltage and summed current with a distinct-voltage fixture.

### Notes
- python-kasa 0.10.2's `consumption_today`/`consumption_this_month` properties used (modern surface; the SRD's mention of `get_daystat`/`get_monthstat` was the deprecated path).
- Schedule module is `Module.IotSchedule` (not `Module.Schedule` — KLAP has no schedule module upstream).
- 256 tests, CI matrix Python 3.11/3.12/3.13 × ubuntu/macos.

## [0.1.0] — 2026-04-27

First usable release: Phase 1 MVP per [docs/SRD-kasa-cli.md](docs/SRD-kasa-cli.md) §16.1.

### Added
- **Discovery** (FR-1..5b): `kasa-cli discover` — UDP broadcast across both legacy (9999) and KLAP/Smart (20002+) ports via `python-kasa.Discover.discover()`. Zero-result responses exit 0 with an INFO log; broadcast-bind failures exit 3. `--target-network <CIDR>` for multi-NIC hosts.
- **Listing** (FR-6..8): `kasa-cli list` (config-resolved, fast), `--probe` for liveness checks, `--online-only` filter, `--groups` for group-membership view.
- **Info** (FR-9..10): `kasa-cli info <target>` — live `device.update()` then full `Device` record per SRD §10.1.
- **Control** (FR-11..15a): `kasa-cli on`, `off` — idempotent. Multi-socket strips (KP303, KP400, EP40, HS300) require explicit `--socket N` or `--socket all` per FR-15a's safety-by-default rationale.
- **Output** (FR-32..35a): text on tty, JSONL on pipe, `--json` / `--jsonl` overrides, `--quiet`. Every JSON byte round-trip-validated before write per FR-35a.
- **Errors** (FR-36..39): exit codes `0/1/2/3/4/5/6/7/64/130/143` per SRD §11.1. Structured-error envelope on stderr (omits null fields per §11.2). `-v`/`-vv` configures a stderr JSON-line `StreamHandler` at INFO/DEBUG.
- **Config** (FR-40..40c): TOML at `~/.config/kasa-cli/config.toml` with `--config <path>` and `KASA_CLI_CONFIG` env precedence. `kasa-cli config show` round-trips through `effective_toml`. `kasa-cli config validate` lints.
- **Credentials** (FR-CRED-1..3, FR-CRED-9): JSON file at `~/.config/kasa-cli/credentials` with versioned schema (top-level `version: 1`, `username`, `password`). Permission check (chmod 0600) and symlink rejection. Per-device `[devices.<alias>] credential_file` override. `KASA_USERNAME` / `KASA_PASSWORD` env vars. `--credential-source env|file|none` filter.
- **KLAP session cache** (FR-CRED-4..8, FR-CRED-10..11): `~/.config/kasa-cli/.tokens/<MAC>.json`, chmod 0600 / parent 0700. Atomic writes (tmpfile + fsync + rename). Per-device `flock`-based advisory lock. **Wall-clock expiry on disk** (translated to/from python-kasa's process-relative `_session_expire_at` at the boundary) — survives process restarts. `kasa-cli auth status` / `auth flush [--target <alias>]`.

### Notes
- Python 3.11+ on macOS 13+ and Linux x86_64/arm64. Windows out of scope.
- `python-kasa>=0.10.2,<0.11`. Wraps but does not reimplement.
- 147 tests, CI matrix is Python 3.11/3.12/3.13 × ubuntu/macos.
- Install: `uv tool install git+ssh://git@github.com/agileguy/kasa-cli@v0.1.0`.
