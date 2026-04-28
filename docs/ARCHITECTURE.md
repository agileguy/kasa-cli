# kasa-cli — Architecture

A high-level guide to the codebase. For canonical functional requirements, see [SRD-kasa-cli.md](SRD-kasa-cli.md). This doc focuses on **how** the code is laid out, **why** it's that way, and **where** to make changes for common tasks.

## The 30-second tour

```
kasa-cli <verb> <target> [flags]
              │
              ▼
        ┌──────────┐
        │  cli.py  │   ◄── Click group, signal handler, config + creds resolution
        └────┬─────┘
             │
             ▼
   ┌──────────────────┐
   │   verbs/*.py     │   ◄── one file per verb; pure async logic
   └────────┬─────────┘
            │
   ┌────────┴─────────┐
   ▼                  ▼
┌──────────┐    ┌──────────────┐
│wrapper.py│    │ parallel.py  │   ◄── @group + batch fanout engine
└────┬─────┘    └──────────────┘
     │
     ▼
┌──────────┐
│python-kasa│   ◄── ONE module imports kasa.* (the wrapper); nothing else
└──────────┘
```

**Key invariants** (enforced by code review):

1. **Only `wrapper.py` imports `kasa.*`.** Verbs call wrapper helpers; the wrapper translates python-kasa exceptions to `KasaCliError` subclasses.
2. **Output formatting is mode-aware** (`output.py`). Stream-shaped commands (`--watch`, batch) flush per-record to stdout — never buffer-then-emit.
3. **Exit codes are SRD-mandated** ([§11.1](SRD-kasa-cli.md#11-error-model-and-exit-codes)). Every failure path in the codebase raises a `KasaCliError` subclass that maps to a specific exit code via `_run_async` / `_run_async_graceful`.
4. **No bridge layers.** Each module imports its dependencies directly — there is no "fall back to a stub if the module is missing" pattern. (Phase 1 had one; it hid wiring bugs and was removed.)

---

## Package layout

```
src/kasa_cli/
├── __init__.py             # __version__
├── __main__.py             # python -m kasa_cli entrypoint; SystemExit translation
├── cli.py                  # Click group, signal handler, dispatch
├── errors.py               # exit codes, KasaCliError hierarchy, StructuredError
├── types.py                # Device, Socket, Group, Reading dataclasses
├── output.py               # OutputMode, emit / emit_one / emit_stream / emit_error
├── config.py               # TOML loader, validate, effective_toml
├── credentials.py          # versioned-JSON credentials file, env var fallbacks
├── auth_cache.py           # KLAP session persistence; per-device flock
├── colors.py               # named-color → HSV table (12 names)
├── parallel.py             # run_parallel, AggregateResult, FR-29a exit-code logic
├── wrapper.py              # the ONLY kasa.* importer; resolve_target, discover, etc.
├── py.typed                # PEP-561 marker
└── verbs/
    ├── __init__.py
    ├── discover_cmd.py
    ├── list_cmd.py
    ├── info_cmd.py
    ├── onoff.py            # on, off (toggle is in toggle_cmd.py)
    ├── toggle_cmd.py
    ├── set_cmd.py          # brightness, color, color-temp
    ├── energy_cmd.py
    ├── schedule_cmd.py     # legacy IOT only
    ├── groups_cmd.py       # `groups list` only in v1
    └── batch_cmd.py        # FR-30/31/31a/31b/31c
```

`tests/` mirrors the source layout (`tests/test_<module>.py`) plus integration files (`test_cli.py`, `test_cli_phase2.py`, `test_group_target.py`, `test_signal_handler.py`).

---

## Per-module responsibilities

### `cli.py` — the Click surface

The single biggest module (~1900 lines as of v0.3.0). Owns:

- The top-level `@click.group()` and every sub-verb registration
- `--json` / `--jsonl` / `--quiet` / `--timeout` / `--config` / `--credential-source` / `-v` / `--concurrency` global flags
- `_run_async` (single-target verbs) and `_run_async_graceful` (batch / @group with FR-31c drain)
- Signal installation (`loop.add_signal_handler` on POSIX; falls through cleanly on Windows / sandboxed environments)
- `_resolve_credentials` — bridges Click flags to A's `credentials.resolve_credentials(config, alias=)` resolver
- `_load_config` / `_resolve_concurrency` — config loading + concurrency fallback chain
- `_attach_file_logging` — runtime tee for `[logging] file`
- `@group-name` target syntax dispatch via `_dispatch_target_or_group`
- `_Exit64UsageError(click.UsageError)` — Click subclass with `exit_code = 64` so callbacks raise SRD-correct errors

**Why it's so big**: Click's decorator pattern means each verb's CLI surface lives here, plus the shared dispatcher and signal handler. A future v0.3.1+ refactor could split `cli/dispatch.py` and have each verb register itself there. For v0.3.0 the size is acknowledged technical debt.

### `wrapper.py` — the python-kasa boundary

Every `kasa.*` symbol in the codebase is imported here. Verbs call wrapper helpers; the wrapper translates python-kasa exceptions:

- `kasa.exceptions.AuthenticationError` → `AuthError` (exit 2)
- `kasa.exceptions.KasaTimeoutError` / `OSError` → `NetworkError` (exit 3)
- `kasa.exceptions.UnsupportedDeviceError` / capability-gating → `UnsupportedFeatureError` (exit 5)
- generic `kasa.exceptions.KasaException` → `DeviceError` (exit 1)

Public surface used by verbs:

- `resolve_target(target, *, config_lookup, credentials, timeout) -> kasa.Device`
- `discover(*, timeout, target_network, credentials) -> list[Device]`
- `probe_alive(device, *, timeout) -> bool`
- `to_device_record(kdev) -> Device` (translation to SRD §10.1 dataclass)
- `set_brightness / set_color_temp / set_hsv` (via `Module.Light`)
- `read_energy(kdev, *, socket, cumulative) -> Reading`
- `read_schedule(kdev) -> list[dict]` (legacy IOT only; KLAP raises Unsupported)

The wrapper deliberately does NOT import from `config.py`, `credentials.py`, or `auth_cache.py` — callers resolve credentials and the Config first, then pass plain values down. This keeps the wrapper trivially testable without a config layer.

### `parallel.py` — the fanout engine

Used by both `@group` dispatch (in `cli.py`) and the `batch` verb (`verbs/batch_cmd.py`). Public surface:

```python
@dataclass(frozen=True) class TaskResult:    # one per sub-op
@dataclass(frozen=True) class AggregateResult: # the run as a whole

async def run_parallel(
    targets: list[str],
    fn: Callable[[str], Awaitable[TaskResult]],
    *,
    concurrency: int,
    on_each: Callable[[TaskResult], None] | None = None,
    on_signal: Callable[[Callable[[], None]], None] | None = None,
    drain_timeout: float = 2.0,
) -> AggregateResult: ...

def aggregate_exit_code(results) -> int: ...
def build_aggregate_summary_error(agg) -> StructuredError: ...
def emit_aggregate_summary_to_stderr(agg, mode) -> None: ...
```

**Aggregate exit-code rules** (FR-29a / FR-31a, codified in `aggregate_exit_code`):

- Empty results → 0
- All success → 0
- Mixed success+failure → 7 (partial failure)
- All failure, same reason → that reason's code (e.g., 3 if all unreachable)
- All failure, mixed reasons → 7 (per [SRD §11.1](SRD-kasa-cli.md))

**Cancellation** (FR-31c): `on_signal` is called with a stop-callable that the caller saves; firing it stops new dispatch immediately and triggers a 2-second drain for in-flight tasks. Stragglers are cancelled cleanly via `gather(return_exceptions=True)`.

### `output.py` — mode-aware emission

| Function | Use |
|---|---|
| `detect_mode(json, jsonl, quiet)` | Resolves `--json`/`--jsonl`/`--quiet`/auto to `OutputMode` |
| `emit_one(item, mode, *, formatter, stream)` | Single item, flushed |
| `emit_stream(items, mode, *, formatter)` | Batch emit (used in `--json` array mode) |
| `emit_error(err, mode, *, stream)` | StructuredError to stderr; uses `to_json()` to omit null fields |
| `_safe_dumps(obj, pretty)` | Round-trip-validates JSON before write (FR-35a) |

The streaming pattern that EVERY watch / batch / group operation MUST follow (Phase 2 lesson):

```python
async for tick in source:
    emit_one(tick, mode, formatter=fmt)   # writes-then-flushes
```

Buffering + late emit is forbidden for stream-shaped commands — the production loop is unbounded so the operator would never see output.

### `errors.py` — exit codes + structured errors

Closed enum of error names; one Python exception class per exit code:

| Class | exit_code | error_name |
|---|---|---|
| `DeviceError` | 1 | `device_error` |
| `AuthError` | 2 | `auth_failed` |
| `NetworkError` | 3 | `network_error` |
| `NotFoundError` | 4 | `not_found` |
| `UnsupportedFeatureError` | 5 | `unsupported_feature` |
| `ConfigError` | 6 | `config_error` |
| `PartialFailureError` | 7 | `partial_failure` |
| `UsageError` | 64 | `usage_error` |
| `KasaInterruptError` | 130 | `interrupted` |

`StructuredError` dataclass (SRD §11.2) is the wire shape on stderr:

```python
@dataclass
class StructuredError:
    error: str        # closed enum from above
    exit_code: int
    target: str | None
    message: str
    hint: str | None = None
    extra: dict | None = None

    def to_json(self) -> str: ...   # omits null optionals; FR-35a
```

### `auth_cache.py` — KLAP session persistence

Per-device session cache at `~/.config/kasa-cli/.tokens/<MAC>.json`, chmod 0600 inside chmod 0700 directory. Atomic writes (tmpfile + fsync + rename). Per-device `flock`-based advisory lock prevents concurrent KLAP-counter corruption.

**Cross-process expiry**: python-kasa's internal `_session_expire_at` is monotonic-clock-based (process-relative), which doesn't survive process restarts. The cache module translates to wall-clock on save and back to monotonic-relative on load. (This was a Phase 1 review-caught bug.)

### `verbs/*.py` — one verb, one module

Each verb module exports an `async def run_<verb>(...)` function. The CLI layer wraps it in Click registrations + the dispatcher.

Verbs are NOT allowed to:

- Import `kasa.*` directly (boundary owned by `wrapper.py`)
- Write files (auth_cache and config own their respective surfaces)
- Configure logging (cli.py's `_configure_logging` owns this)
- Re-enter Click for sub-dispatch (batch parses lines with `shlex.split` + a custom mini-parser, not Click)

---

## Cross-cutting concerns

### Logging

Every module declares `logger = logging.getLogger("kasa_cli")` at module scope. `cli.py:_configure_logging(verbose)` attaches a `StreamHandler(sys.stderr)` with a JSON-line formatter at WARNING (`-v` → INFO; `-vv` → DEBUG, includes raw KLAP frames with credentials redacted).

`[logging] file = "<path>"` in config attaches a `FileHandler` (append mode) alongside stderr — both handlers receive the same lines.

### Type checking

`mypy --strict` is gated in CI. Every module has `from __future__ import annotations` for forward-reference flexibility. `types.py` and `errors.py` use `dataclass(slots=True)` for performance + attribute-typo safety.

### Testing

345+ tests, no real network access. Patterns:

- **Unit tests** mock `kasa.Device` with `MockKasaDevice` (in `tests/conftest.py`) + per-module module mocks (`MockLightModule`, `MockEnergyModule`, etc.)
- **CLI tests** use Click's `CliRunner` — every exit-code assert is `== <exact code>`, never `!= 0` (the Phase 1+2 lesson)
- **Subprocess tests** for SIGINT/SIGTERM (`tests/test_signal_handler.py`) use the `KASA_CLI_TEST_FAKE_SLEEP` env hatch for deterministic timing
- **Cross-process tests** for the auth_cache wall-clock expiry (`test_session_expiry_survives_process_restart`)

### CI matrix

GitHub Actions: Python 3.11 / 3.12 / 3.13 × ubuntu-latest / macos-latest. Each job runs `ruff check`, `ruff format --check`, `mypy --strict src`, `pytest --cov=kasa_cli`.

---

## Adding a new verb

The pattern, walked through:

1. **Create `src/kasa_cli/verbs/<verb>_cmd.py`**:

    ```python
    from __future__ import annotations
    from kasa_cli import wrapper
    from kasa_cli.errors import EXIT_SUCCESS
    from kasa_cli.output import OutputMode, emit_one
    from kasa_cli.types import ...

    async def run_<verb>(target: str, *, config_lookup, credentials, timeout, mode) -> int:
        kdev = await wrapper.resolve_target(target, config_lookup=config_lookup, credentials=credentials, timeout=timeout)
        # ... do the verb ...
        emit_one(result, mode, formatter=...)
        return EXIT_SUCCESS
    ```

2. **Add wrapper helpers** if the verb needs new python-kasa access. Put them in `wrapper.py` under a delimited section so the diff is reviewable.

3. **Register in `cli.py`**:

    ```python
    @main.command("<verb>")
    @click.argument("target")
    @click.option("--whatever", ...)
    @click.pass_context
    def <verb>(ctx, target: str, *, whatever: str) -> None:
        state = ctx.obj
        cfg = _load_config(state["config_path"])
        creds = _resolve_credentials(state["credential_source"], config=cfg, alias=target)

        async def _go() -> int:
            return await run_<verb>(target, config_lookup=..., credentials=creds, timeout=state["timeout"], mode=state["mode"], whatever=whatever)

        sys.exit(_run_async(_go, mode=state["mode"]))
    ```

4. **Add tests** in `tests/test_verbs_<verb>.py`. Use `tests/conftest.py` fixtures. Every exit-code assertion uses an exact integer.

5. **Update**:

    - `docs/USAGE.md` (a verb section)
    - `docs/CONFIG.md` (if the verb needs new config fields)
    - `CHANGELOG.md` under `[Unreleased]`
    - `README.md` highlights table if it's a major feature

---

## Phase plan history

The project shipped in 3 phases over a single day:

| Phase | Tag | Focus | Test count | Review iterations |
|---|---|---|---|---|
| Phase 1 | v0.1.0 | discover, list, info, on, off + config + credentials + KLAP cache | 147 | 1 |
| Phase 2 | v0.2.0 | toggle, set (brightness/color/color-temp), energy, schedule, file logging, named colors | 256 | 1 |
| Phase 3 | v0.3.0 | groups, batch, parallel engine, FR-31c graceful drain | 353 | 1 |

Each phase used a **two-engineer-in-parallel-worktrees** development model with:
- A shared SRD as the spec
- Multi-perspective code review (Gemini + Claude + MCS fanout) on every PR
- A single fix-loop iteration per PR
- Bumping the patch version + GitHub release per phase

The same pattern (tests asserting implementation behavior instead of SRD behavior) recurred in all three phases in different shapes; each was caught by reviewers and fixed before merge. See [CHANGELOG.md](../CHANGELOG.md) for per-phase deltas.

The SRD's Phase 4 is explicitly a no-commitment placeholder — any future scope expansion (Tapo support, hub child enumeration, etc.) gets a new SRD.

---

## See also

- [docs/SRD-kasa-cli.md](SRD-kasa-cli.md) — the canonical specification (FRs, error model, decisions)
- [docs/USAGE.md](USAGE.md) — operator-facing reference
- [docs/CONFIG.md](CONFIG.md) — config schema
- [CHANGELOG.md](../CHANGELOG.md) — per-version delta
