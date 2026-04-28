"""``kasa-cli batch`` (SRD §5.9, FR-30 / FR-31 / FR-31a / FR-31b / FR-31c).

The ``batch`` verb reads newline-delimited sub-commands from a file
(``--file``) or from stdin (``--stdin``), parses each line into a verb
invocation, and fans the resulting jobs out via :func:`kasa_cli.parallel.run_parallel`.

Phase 1+2 lessons applied:

* Stream-shaped: each completed sub-result flushes immediately (per-record
  ``flush()`` in JSONL/TEXT mode), never buffer-then-emit. ``--json`` is the
  one exception — it must produce one JSON array, so we collect and emit at
  the end (FR-35a).
* Exit-code asserts: every test asserts the EXACT FR-29a / FR-31a code
  (0, 7, or homogeneous-failure-code) — never just ``!= 0``.

Sub-command grammar:

* Blank lines and lines whose first non-whitespace character is ``#`` are
  silently skipped (FR-31b).
* The remaining lines are parsed via :func:`shlex.split` so quoting and
  escaping behave as in the shell (e.g. ``set patio --color "warm white"``).
* The first token names the verb (``info``, ``on``, ``off``, ``toggle``,
  ``set``, ``energy``); subsequent tokens are arguments and options. The
  parser is intentionally minimal — it does NOT re-enter Click. This keeps
  the per-line dispatch deterministic and avoids Click's logging/contextual
  side effects firing once per line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TextIO

from kasa_cli import auth_cache, parallel
from kasa_cli.errors import (
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    KasaCliError,
    StructuredError,
    UsageError,
)
from kasa_cli.output import OutputMode, _safe_dumps
from kasa_cli.parallel import AggregateResult, TaskResult
from kasa_cli.wrapper import CredentialBundle

# FR-31c drain budget: we wait up to this many seconds for in-flight
# sub-operations to finish after the stop event fires.
DRAIN_BUDGET_SECONDS: Final[float] = 2.0


@dataclass(frozen=True, slots=True)
class _ParsedLine:
    """One parsed batch line — verb plus already-tokenized argv."""

    lineno: int
    verb: str
    argv: list[str]
    raw: str


def _iter_batch_lines(source: TextIO) -> list[_ParsedLine]:
    """Read ``source`` and return parsed lines, skipping blanks/comments.

    FR-31b: empty input is empty output (caller emits ``[]`` in JSON mode);
    blank and ``#``-prefixed lines are silently dropped.
    """
    parsed: list[_ParsedLine] = []
    for lineno, raw in enumerate(source, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError as exc:
            # Unbalanced quotes etc. — surface as a usage error per line.
            raise UsageError(
                f"batch line {lineno}: cannot parse: {exc}",
                hint=f"Check quoting on: {stripped!r}",
            ) from exc
        if not tokens:
            continue
        verb, *argv = tokens
        parsed.append(_ParsedLine(lineno=lineno, verb=verb, argv=argv, raw=stripped))
    return parsed


# ---------------------------------------------------------------------------
# Per-line dispatcher
# ---------------------------------------------------------------------------
#
# Each entry returns ``Awaitable[TaskResult]``. We deliberately do NOT
# re-enter Click for each line — the verb modules expose ``run_*`` async
# functions whose signatures are stable across phases. We tokenize per line
# with ``shlex.split`` and then plumb the arguments through a small
# argument parser that mirrors each verb's flag set.


def _make_lookup(
    config_lookup: Callable[[str], tuple[str | None, str | None]] | None,
) -> Callable[[str], tuple[str | None, str | None]]:
    if config_lookup is not None:
        return config_lookup

    def _identity(target: str) -> tuple[str | None, str | None]:
        return target, None

    return _identity


def _parse_kv_flags(
    argv: list[str],
    allowed: set[str],
    *,
    bare_flags: dict[str, str] | None = None,
) -> dict[str, str]:
    """Parse ``--key value`` / ``--key=value`` pairs out of ``argv``.

    Returns a dict of {name → value}. Unknown flags raise UsageError. Bare
    positional tokens are returned via the special key ``"_pos"`` (a
    space-joined list isn't useful here — callers want the first one).

    Args:
        argv: The tokens after the verb name.
        allowed: Flags that take a value (``--key value`` or
            ``--key=value``).
        bare_flags: Optional mapping of bare-flag name -> normalized
            destination spec ``"<dest>=<value>"``. Use this for boolean
            toggles like ``--cumulative`` (sets ``cumulative=true``) and
            ``--no-cumulative`` (sets ``cumulative=false``). When a
            bare-flag name appears with an attached ``=value`` form, that
            wins (e.g. ``--cumulative=false`` overrides the implied
            ``true``); when it appears bare, the spec's value is used.
    """
    bare_flags = bare_flags or {}
    result: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--"):
            name, sep, inline_val = tok[2:].partition("=")
            in_value_flags = name in allowed
            in_bare_flags = name in bare_flags
            if not in_value_flags and not in_bare_flags:
                hint_allowed = sorted(set(allowed) | set(bare_flags))
                raise UsageError(
                    f"unknown flag --{name} in batch line",
                    hint=f"allowed: {hint_allowed}",
                )
            # Bare-flag form (no ``=``).
            if not sep and in_bare_flags:
                # E.g. ``--cumulative`` -> bare_flags["cumulative"] is
                # "cumulative=true"; ``--no-cumulative`` is
                # "cumulative=false". Splitting once on ``=`` gives the
                # destination key + normalized value.
                spec = bare_flags[name]
                dest, _, dest_val = spec.partition("=")
                result[dest] = dest_val
                i += 1
                continue
            # Inline-value form (``--key=value``).
            if sep:
                # Bare-flag aliases never accept inline values
                # (``--no-cumulative=true`` is nonsense — reject).
                if in_bare_flags and not in_value_flags:
                    raise UsageError(
                        f"--{name} does not accept a value",
                        hint=f"use bare --{name} or its inverse",
                    )
                result[name] = inline_val
                i += 1
                continue
            # Space-separated value form (``--key value``).
            if i + 1 >= len(argv):
                raise UsageError(f"--{name} requires a value")
            result[name] = argv[i + 1]
            i += 2
            continue
        positional.append(tok)
        i += 1
    if positional:
        result["_pos"] = positional[0]
    return result


def _kasa_error_to_task_result(target: str, exc: KasaCliError) -> TaskResult:
    return TaskResult(
        target=target,
        success=False,
        exit_code=exc.exit_code,
        output=None,
        error=exc.to_structured(),
    )


def _generic_error_to_task_result(target: str, exc: BaseException) -> TaskResult:
    return TaskResult(
        target=target,
        success=False,
        exit_code=1,
        output=None,
        error=StructuredError(
            error="device_error",
            exit_code=1,
            target=target,
            message=f"Unhandled error: {type(exc).__name__}: {exc}",
        ),
    )


async def _dispatch_line(
    line: _ParsedLine,
    *,
    config_lookup: Callable[[str], tuple[str | None, str | None]] | None,
    credentials: CredentialBundle,
    timeout: float,
) -> TaskResult:
    """Dispatch a single parsed line to the appropriate verb.

    Returns a ``TaskResult`` regardless of success or failure. Exceptions
    raised by verbs are caught here and projected into a structured error.

    Test hatch: when ``KASA_CLI_TEST_FAKE_SLEEP`` is set, every line sleeps
    for that many seconds (default 0.5) and returns a fake-success
    TaskResult. This gives the FR-31c signal-handler test deterministic
    timing without spinning up real Kasa devices. Documented hatch — keep
    in sync with ``tests/test_signal_handler.py``.
    """
    import os as _os

    fake_sleep_raw = _os.environ.get("KASA_CLI_TEST_FAKE_SLEEP")
    if fake_sleep_raw:
        try:
            secs = float(fake_sleep_raw)
        except ValueError:
            secs = 0.5
        # Yield control to the loop so SIGINT can land between sleeps.
        await asyncio.sleep(secs)
        fake_target = line.argv[0] if line.argv else line.verb
        return TaskResult(
            target=fake_target,
            success=True,
            exit_code=EXIT_SUCCESS,
            output={"verb": line.verb, "target": fake_target, "ok": True, "fake": True},
        )

    lookup = _make_lookup(config_lookup)
    verb = line.verb

    try:
        if verb in ("on", "off"):
            from kasa_cli.verbs.onoff import run_onoff

            flags = _parse_kv_flags(line.argv, allowed={"socket"})
            target = flags.get("_pos")
            if not target:
                raise UsageError(f"{verb}: missing target alias on line {line.lineno}")
            code = await run_onoff(
                action=verb,  # type: ignore[arg-type]
                target=target,
                socket_arg=flags.get("socket"),
                config_lookup=lookup,
                credentials=credentials,
                timeout=timeout,
                mode=OutputMode.QUIET,
            )
            return TaskResult(
                target=target,
                success=code == EXIT_SUCCESS,
                exit_code=code,
                output={"verb": verb, "target": target, "ok": code == EXIT_SUCCESS},
            )

        if verb == "toggle":
            from kasa_cli.verbs.toggle_cmd import run_toggle

            flags = _parse_kv_flags(line.argv, allowed={"socket"})
            target = flags.get("_pos")
            if not target:
                raise UsageError(f"toggle: missing target alias on line {line.lineno}")
            code = await run_toggle(
                target=target,
                socket_arg=flags.get("socket"),
                config_lookup=lookup,
                credentials=credentials,
                timeout=timeout,
                mode=OutputMode.QUIET,
            )
            return TaskResult(
                target=target,
                success=code == EXIT_SUCCESS,
                exit_code=code,
                output={"verb": "toggle", "target": target, "ok": code == EXIT_SUCCESS},
            )

        if verb == "info":
            from kasa_cli.verbs.info_cmd import run_info

            target = line.argv[0] if line.argv else None
            if not target:
                raise UsageError(f"info: missing target alias on line {line.lineno}")
            code = await run_info(
                target=target,
                config_lookup=lookup,
                credentials=credentials,
                timeout=timeout,
                mode=OutputMode.QUIET,
            )
            return TaskResult(
                target=target,
                success=code == EXIT_SUCCESS,
                exit_code=code,
                output={"verb": "info", "target": target, "ok": code == EXIT_SUCCESS},
            )

        if verb == "set":
            from kasa_cli.verbs.set_cmd import run_set

            flags = _parse_kv_flags(
                line.argv,
                allowed={"brightness", "color-temp", "hsv", "hex", "color", "socket"},
            )
            target = flags.get("_pos")
            if not target:
                raise UsageError(f"set: missing target alias on line {line.lineno}")
            brightness = int(flags["brightness"]) if "brightness" in flags else None
            color_temp = int(flags["color-temp"]) if "color-temp" in flags else None
            code = await run_set(
                target=target,
                brightness=brightness,
                color_temp=color_temp,
                hsv=flags.get("hsv"),
                hex_color=flags.get("hex"),
                color_name=flags.get("color"),
                socket_arg=flags.get("socket"),
                config_lookup=lookup,
                credentials=credentials,
                timeout=timeout,
                mode=OutputMode.QUIET,
            )
            return TaskResult(
                target=target,
                success=code == EXIT_SUCCESS,
                exit_code=code,
                output={"verb": "set", "target": target, "ok": code == EXIT_SUCCESS},
            )

        if verb == "energy":
            from kasa_cli.verbs.energy_cmd import run_energy

            # ``--cumulative`` and ``--no-cumulative`` are bare boolean
            # toggles; ``--cumulative=true|false`` (with ``=``) is also
            # accepted for explicit value form. Default is True.
            flags = _parse_kv_flags(
                line.argv,
                allowed={"socket", "cumulative"},
                bare_flags={
                    "cumulative": "cumulative=true",
                    "no-cumulative": "cumulative=false",
                },
            )
            target = flags.get("_pos")
            if not target:
                raise UsageError(f"energy: missing target alias on line {line.lineno}")
            code = await run_energy(
                target=target,
                watch_seconds=None,
                cumulative=flags.get("cumulative", "true").lower() != "false",
                socket=int(flags["socket"]) if "socket" in flags else None,
                config_lookup=lookup,
                credentials=credentials,
                timeout=timeout,
                mode=OutputMode.QUIET,
            )
            return TaskResult(
                target=target,
                success=code == EXIT_SUCCESS,
                exit_code=code,
                output={"verb": "energy", "target": target, "ok": code == EXIT_SUCCESS},
            )

        # Unknown verb in batch input.
        raise UsageError(
            f"unknown verb {verb!r} on batch line {line.lineno}",
            hint="Supported: info, on, off, toggle, set, energy",
        )

    except KasaCliError as exc:
        # Operator precedence: prefer the exception's explicit target; fall
        # back to the first positional, then the verb name. Parentheses are
        # load-bearing — without them the expression collapses to
        # ``(exc.target or line.argv[0]) if line.argv else line.verb`` and
        # silently drops ``exc.target`` when ``argv`` is empty.
        target = exc.target or (line.argv[0] if line.argv else line.verb)
        return _kasa_error_to_task_result(target or line.verb, exc)
    except Exception as exc:  # pragma: no cover — last-ditch defensive net
        target = line.argv[0] if line.argv else line.verb
        return _generic_error_to_task_result(target, exc)


# ---------------------------------------------------------------------------
# Streaming emission
# ---------------------------------------------------------------------------


def _result_to_dict(r: TaskResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "target": r.target,
        "success": r.success,
        "exit_code": r.exit_code,
    }
    if r.output is not None:
        payload["output"] = r.output
    if r.error is not None:
        payload["error"] = r.error.to_dict()
    return payload


def _emit_per_result(
    r: TaskResult,
    *,
    mode: OutputMode,
    stream: TextIO,
) -> None:
    """Stream one result per FR-31a / FR-35a contract."""
    if mode is OutputMode.QUIET:
        return
    payload = _result_to_dict(r)
    if mode is OutputMode.JSON:
        # JSON mode collects — see ``_emit_collected`` at end.
        return
    if mode is OutputMode.TEXT:
        ok_marker = "ok" if r.success else f"FAIL[{r.exit_code}]"
        stream.write(f"{ok_marker:<10} {r.target}\n")
        stream.flush()
        return
    # JSONL — flush per record (Phase 2 lesson).
    stream.write(_safe_dumps(payload, pretty=False))
    stream.write("\n")
    stream.flush()


def _emit_collected(
    results: list[TaskResult],
    *,
    mode: OutputMode,
    stream: TextIO,
) -> None:
    """Emit a collected JSON array (only used in --json mode)."""
    if mode is not OutputMode.JSON:
        return
    payload = [_result_to_dict(r) for r in results]
    stream.write(_safe_dumps(payload, pretty=True))
    stream.write("\n")
    stream.flush()


# ---------------------------------------------------------------------------
# FR-31c interrupted summary
# ---------------------------------------------------------------------------


def _emit_interrupted_summary(
    *,
    completed: int,
    pending: int,
    stream: TextIO,
    mode: OutputMode,
) -> None:
    """Emit the FR-31c interrupted summary line.

    Per SRD §5.9 FR-31c, the summary is **always** emitted as a JSON line on
    stdout regardless of mode (it's machine-parseable telemetry, not user
    text). ``--quiet`` does not suppress it — operators still need it.
    """
    del mode  # always emit, even in TEXT/QUIET
    payload = {"event": "interrupted", "completed": completed, "pending": pending}
    stream.write(json.dumps(payload, separators=(",", ":")))
    stream.write("\n")
    stream.flush()


def _flush_token_cache_pending() -> None:
    """FR-31c step 4: flush token cache to disk for any pending sessions.

    This is intentionally a no-op. Phase 2's :func:`auth_cache.save_session`
    is called eagerly at result-receive time inside the wrapper layer (and
    fsyncs before the rename), so by the time SIGINT/SIGTERM lands every
    successful auth has already been persisted to disk. There is no
    in-memory buffer / pending-session registry to flush.

    We keep the hook in place because:

    1. A future phase might add registry-based pending-session tracking;
       if so, this is the single point where the CLI calls into
       ``auth_cache`` for the drain path.
    2. The hook documents the FR-31c step 4 contract at the call site
       inside :func:`run_batch`.

    The "eager save_session, no buffering" invariant is verified by
    ``tests/test_verbs_batch.py::
    test_auth_cache_has_no_in_memory_buffer_state`` — if a future change
    introduces a buffer that needs flushing, that test will fail and force
    this helper to grow a real implementation.
    """
    # No-op. Intentionally swallows any error: token-flush failures MUST
    # NOT prevent the process from exiting cleanly with 130/143.
    try:
        # Keep the import warm so static analysis sees it; if a future phase
        # adds e.g. ``auth_cache.flush_pending()``, call it here.
        _ = auth_cache
    except Exception:  # pragma: no cover — defensive
        return


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_batch(
    *,
    source: TextIO,
    mode: OutputMode,
    config_lookup: Callable[[str], tuple[str | None, str | None]] | None,
    credentials: CredentialBundle,
    timeout: float,
    concurrency: int,
    stop_event: asyncio.Event | None = None,
    stdout: TextIO | None = None,
    drain_budget_s: float = DRAIN_BUDGET_SECONDS,
) -> int:
    """Run ``kasa-cli batch``. Returns the FR-31a aggregate exit code.

    ``stop_event`` is the cooperative cancellation flag wired in by ``cli.py``
    so the FR-31c signal handler can request a graceful drain. When unset,
    no signal handling happens here (the caller is responsible).
    """
    out = stdout if stdout is not None else sys.stdout

    parsed_lines = _iter_batch_lines(source)

    # FR-31b: empty input → exit 0 with [] in JSON mode, no output otherwise.
    if not parsed_lines:
        if mode is OutputMode.JSON:
            out.write("[]\n")
            out.flush()
        return EXIT_SUCCESS

    # Per-line dispatcher closure that the parallel runner calls.
    line_by_target: dict[str, _ParsedLine] = {}

    async def _dispatch_for_target(synthetic_target: str) -> TaskResult:
        line = line_by_target[synthetic_target]
        return await _dispatch_line(
            line,
            config_lookup=config_lookup,
            credentials=credentials,
            timeout=timeout,
        )

    # We key tasks by a synthetic per-line key (line:N) so parallel.run_parallel
    # can address them uniquely even if two lines target the same alias. The
    # per-line lookup map indirects back to the parsed line.
    targets: list[str] = []
    for parsed in parsed_lines:
        synthetic = f"line:{parsed.lineno}"
        line_by_target[synthetic] = parsed
        targets.append(synthetic)

    # Per-result streaming — flush each completed line in JSONL/TEXT mode.
    streamed: list[TaskResult] = []

    def _on_result(r: TaskResult) -> None:
        streamed.append(r)
        _emit_per_result(r, mode=mode, stream=out)

    interrupted = False

    # Bridge B3's external stop_event semantics into A3's on_signal-callback-registration
    # contract. A3's run_parallel calls on_signal(register_stop_fn) once at start; we
    # capture the stop callable, and a watcher task fires it when cli.py's stop_event
    # is set. This keeps batch_cmd.py oblivious to A3's internal stop mechanism.
    stop_fn_holder: list[Callable[[], None] | None] = [None]

    def _register_stop(stop_fn: Callable[[], None]) -> None:
        nonlocal interrupted
        stop_fn_holder[0] = stop_fn
        # Mark interrupted=True only when the actual stop fires; here we just save it.

    async def _watch_external_stop_event() -> None:
        if stop_event is None:
            return
        await stop_event.wait()
        nonlocal interrupted
        interrupted = True
        fn = stop_fn_holder[0]
        if fn is not None:
            fn()

    aggregate: AggregateResult
    watch_task = asyncio.create_task(_watch_external_stop_event())
    try:
        aggregate = await parallel.run_parallel(
            targets,
            _dispatch_for_target,
            concurrency=concurrency,
            on_signal=_register_stop,
            on_each=_on_result,
        )
    except asyncio.CancelledError:
        # We were cancelled mid-flight (FR-31c drain budget exceeded).
        # Whatever we got into ``streamed`` is what we have. The CLI signal
        # handler picked the exit code; we just emit the summary and bail.
        completed = len(streamed)
        pending = max(0, len(parsed_lines) - completed)
        _flush_token_cache_pending()
        _emit_interrupted_summary(
            completed=completed,
            pending=pending,
            stream=out,
            mode=mode,
        )
        # Re-raise so the CLI _run_async sees the cancellation and emits
        # the right exit code (130 or 143).
        raise
    finally:
        watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch_task

    # If the stop_event fired but we drained successfully, emit the summary
    # but still return the aggregate exit code (the CLI overrides with
    # 130/143 once it sees ``stop_event.is_set()``). The FR-31c interrupted
    # summary on stdout is the authoritative end-of-run record on the
    # interrupt path; we deliberately do NOT also emit the FR-35a stderr
    # summary here so operators don't see two competing summaries.
    if interrupted or (stop_event is not None and stop_event.is_set()):
        completed = len(streamed)
        pending = max(0, len(parsed_lines) - completed)
        _flush_token_cache_pending()
        _emit_interrupted_summary(
            completed=completed,
            pending=pending,
            stream=out,
            mode=mode,
        )
        # Note: FR-31c says "exit with 130 or 143" — the cli.py signal
        # handler returns those codes. Return whatever the aggregate would
        # have been; cli.py will override.
        return aggregate.exit_code

    # Normal completion: emit the JSON-array tail in --json mode and return.
    _emit_collected(streamed, mode=mode, stream=out)
    # FR-35a: one structured §11.2 summary on stderr when the aggregate is
    # non-zero, regardless of mode (operators always need to know why).
    parallel.emit_aggregate_summary_to_stderr(aggregate, total_inputs=len(parsed_lines))
    return aggregate.exit_code


# Re-exports for tests and the cli.py wiring.
__all__ = [
    "DRAIN_BUDGET_SECONDS",
    "run_batch",
]


# Belt-and-suspenders: importing ``EXIT_PARTIAL_FAILURE`` / ``EXIT_USAGE_ERROR``
# at module top is intentional even though they're not all referenced in this
# file's runtime code — they document the contract this verb honors and let
# tests import constants from one place. ``ruff`` would otherwise flag them as
# unused.
_ = EXIT_PARTIAL_FAILURE
_ = EXIT_USAGE_ERROR
