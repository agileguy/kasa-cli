# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-04-27

Phase 3 per [docs/SRD-kasa-cli.md](docs/SRD-kasa-cli.md) §16.3 — final implementation phase. v0.3.0 ships the full SRD-compliant feature set; Phase 4 is a no-commitment placeholder per the SRD.

### Added
- **Groups list** (FR-26, FR-27, FR-29b): `kasa-cli groups list` reads `[groups]` from config and emits each group's `{name, members: [aliases]}`. v1 has no `add`/`remove` mutations — by hand-editing the config per FR-29b.
- **`@group-name` target syntax** (FR-27): `kasa-cli on @bedroom-lights`, `info @rack`, `set @desk --brightness 30`, etc. fan out across the group's members. Supported on `info`, `on`, `off`, `toggle`, `set`, `energy`, `schedule list`. Per-target output is suppressed in fanout mode in favor of one `TaskResult` line per member.
- **`--concurrency N` global flag** (FR-28): overrides `[defaults] concurrency` from config. Applies to `@group` fanout and batch.
- **Parallel-execution engine** (`src/kasa_cli/parallel.py`): `run_parallel(targets, fn, *, concurrency, on_each, on_signal, drain_timeout)` returns an `AggregateResult` with `TaskResult[]` and an exit code per FR-29a. Concurrency-bounded by `asyncio.Semaphore`; per-task results stream via `on_each` callback (each line flushed); cooperative cancellation via `on_signal` callback registration; 2-second drain budget on stop.
- **Aggregate exit code** (FR-29a, FR-31a): empty / all-success → 0, mixed → 7, all-fail-homogeneous → that reason's code, **all-fail-mixed-reasons → 7** (per SRD §11.1).
- **Batch verb** (FR-30, FR-31, FR-31a, FR-31b, FR-31c): `kasa-cli batch --file <path>` and `--stdin` read newline-delimited sub-commands. FR-31b: blank lines and `#` comments skipped; empty input exits 0 with `[]` in JSON mode. FR-31a: aggregate exit code per FR-29a. Per-line dispatch uses `shlex.split` and a verb-specific argument parser (no Click re-entry).
- **Graceful SIGINT/SIGTERM handler** (FR-31c): on signal, (1) cease dispatching new sub-operations, (2) wait up to 2 seconds for in-flight to complete, (3) emit `{"event":"interrupted","completed":N,"pending":M}` summary line on stdout, (4) flush token cache (Phase 2 already eager-saves; verified by structural test), (5) exit with code 130 (SIGINT) or 143 (SIGTERM). `_run_async_graceful` uses `loop.add_signal_handler` for POSIX-friendly delivery.
- **FR-35a stderr summary**: on non-zero aggregate exit (groups fanout or batch), one structured `StructuredError` envelope is emitted to stderr per §11.2 — `error="partial_failure"` for exit 7, or the homogeneous-failure name for unanimous-reason failures.
- **`--cumulative` / `--no-cumulative` bare flags in batch lines**: `energy patio --cumulative` (no value) is now valid; the inline-value form (`--cumulative=true`) still works.

### Fixed
- **FR-29a all-fail-mixed-reasons**: `aggregate_exit_code` now correctly returns `EXIT_PARTIAL_FAILURE` (7) for all-failure cases with mixed reasons. Was returning the first failure's code, contradicting SRD §11.1's "Mixed-failure-reasons SHALL still exit 7" — the third Phase-N anti-pattern (test asserting implementation behavior instead of SRD).
- **`_dispatch_line` operator-precedence bug**: target attribution in error envelopes was wrong when a `KasaCliError` with a populated `target` was raised from an empty-argv line. Fixed via parentheses.
- **Misleading test** `test_signal_during_already_complete_batch_does_not_corrupt_exit` renamed to `test_tiny_batch_exits_zero` — the original docstring claimed it exercised a signal-after-completion edge case, but the test never sent a signal. The original edge case is fundamentally racy in subprocess testing.

### Notes
- `parallel.py` introduces a small new public surface (`TaskResult`, `AggregateResult`, `run_parallel`, `aggregate_exit_code`, `build_aggregate_summary_error`, `emit_aggregate_summary_to_stderr`); these are intended for verb modules and shouldn't be considered stable for external callers.
- 353 tests, CI matrix Python 3.11/3.12/3.13 × ubuntu/macos.
- v0.3.0 ships the full SRD §16 phase plan. The SRD's Phase 4 is explicitly a no-commitment placeholder (Tapo support, hub child enumeration, etc.) — any future work will get a new SRD.

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
