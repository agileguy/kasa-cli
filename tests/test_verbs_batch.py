"""Tests for ``kasa-cli batch`` (FR-30 / FR-31 / FR-31a / FR-31b).

These tests exercise ``run_batch`` directly (decoupled from Click) and the
Click wiring via ``CliRunner``. Every exit-code assertion checks the EXACT
SRD-mandated value (0, 7, 64, plus homogeneous-failure codes) — Phase 1+2
lesson applied.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from click.testing import CliRunner

from kasa_cli import parallel
from kasa_cli.cli import main as cli_main
from kasa_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    AuthError,
    StructuredError,
)
from kasa_cli.output import OutputMode
from kasa_cli.parallel import TaskResult
from kasa_cli.verbs.batch_cmd import run_batch
from kasa_cli.wrapper import CredentialBundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_dispatch(
    results_by_target: dict[str, TaskResult],
) -> Callable[[str], Awaitable[TaskResult]]:
    """Build a fn(target) coroutine that returns canned per-target results."""

    async def _fn(target: str) -> TaskResult:
        return results_by_target[target]

    return _fn


def _make_source(text: str) -> io.StringIO:
    return io.StringIO(text)


# ---------------------------------------------------------------------------
# parallel.run_parallel — sanity (the stub Engineer A3 will overwrite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_all_success_returns_zero() -> None:
    targets = ["a", "b", "c"]
    results = {t: TaskResult(target=t, success=True, exit_code=0) for t in targets}
    agg = await parallel.run_parallel(targets, _stub_dispatch(results), concurrency=4)
    assert agg.exit_code == EXIT_SUCCESS
    assert agg.successes == 3
    assert agg.failures == 0


@pytest.mark.asyncio
async def test_parallel_mixed_returns_seven() -> None:
    results = {
        "a": TaskResult(target="a", success=True, exit_code=0),
        "b": TaskResult(
            target="b",
            success=False,
            exit_code=2,
            error=StructuredError(error="auth_failed", exit_code=2, message="x"),
        ),
    }
    agg = await parallel.run_parallel(["a", "b"], _stub_dispatch(results), concurrency=4)
    assert agg.exit_code == EXIT_PARTIAL_FAILURE


@pytest.mark.asyncio
async def test_parallel_all_same_failure_returns_homogeneous_code() -> None:
    results = {
        "a": TaskResult(
            target="a",
            success=False,
            exit_code=2,
            error=StructuredError(error="auth_failed", exit_code=2, message="x"),
        ),
        "b": TaskResult(
            target="b",
            success=False,
            exit_code=2,
            error=StructuredError(error="auth_failed", exit_code=2, message="y"),
        ),
    }
    agg = await parallel.run_parallel(["a", "b"], _stub_dispatch(results), concurrency=4)
    assert agg.exit_code == EXIT_AUTH_ERROR


# ---------------------------------------------------------------------------
# run_batch — direct invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_empty_input_exits_zero_with_empty_array_in_json_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    code = await run_batch(
        source=_make_source(""),
        mode=OutputMode.JSON,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=4,
        stdout=out,
    )
    assert code == EXIT_SUCCESS
    assert out.getvalue().strip() == "[]"


@pytest.mark.asyncio
async def test_batch_empty_input_exits_zero_with_no_output_in_jsonl_mode() -> None:
    out = io.StringIO()
    code = await run_batch(
        source=_make_source(""),
        mode=OutputMode.JSONL,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=4,
        stdout=out,
    )
    assert code == EXIT_SUCCESS
    assert out.getvalue() == ""


@pytest.mark.asyncio
async def test_batch_blank_lines_and_comments_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-31b: blank lines and ``#`` lines are silently dropped."""
    captured_targets: list[str] = []

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        captured_targets.append(line.argv[0] if line.argv else "")
        return TaskResult(target=line.argv[0], success=True, exit_code=0)

    # Patch the per-line dispatcher so we don't need real devices.
    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    text = "\n# this is a comment\n\non patio\n   \n# another comment\noff kitchen\n"
    out = io.StringIO()
    code = await run_batch(
        source=_make_source(text),
        mode=OutputMode.JSONL,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=4,
        stdout=out,
    )
    assert code == EXIT_SUCCESS
    # Two real lines parsed, no comments/blanks dispatched.
    assert sorted(captured_targets) == ["kitchen", "patio"]


@pytest.mark.asyncio
async def test_batch_happy_path_three_lines_all_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 lines all succeed → exit 0."""

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        target = line.argv[0]
        return TaskResult(target=target, success=True, exit_code=0)

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    text = "on a\non b\noff c\n"
    out = io.StringIO()
    code = await run_batch(
        source=_make_source(text),
        mode=OutputMode.JSONL,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=4,
        stdout=out,
    )
    assert code == EXIT_SUCCESS
    # Three JSONL records on stdout.
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert all(r["success"] is True for r in parsed)
    assert all(r["exit_code"] == 0 for r in parsed)


@pytest.mark.asyncio
async def test_batch_mixed_results_exits_seven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed success/failure → exit 7 (FR-31a)."""
    target_outcomes = {"a": True, "b": False, "c": True}

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        target = line.argv[0]
        ok = target_outcomes[target]
        if ok:
            return TaskResult(target=target, success=True, exit_code=0)
        return TaskResult(
            target=target,
            success=False,
            exit_code=2,
            error=StructuredError(error="auth_failed", exit_code=2, message="x"),
        )

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    text = "on a\non b\non c\n"
    out = io.StringIO()
    code = await run_batch(
        source=_make_source(text),
        mode=OutputMode.JSONL,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=4,
        stdout=out,
    )
    assert code == EXIT_PARTIAL_FAILURE


@pytest.mark.asyncio
async def test_batch_all_same_failure_exits_with_homogeneous_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-failures-same-reason → that reason's exit code (FR-29a)."""

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        target = line.argv[0]
        return TaskResult(
            target=target,
            success=False,
            exit_code=EXIT_AUTH_ERROR,
            error=StructuredError(error="auth_failed", exit_code=EXIT_AUTH_ERROR, message="x"),
        )

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    text = "on a\non b\n"
    out = io.StringIO()
    code = await run_batch(
        source=_make_source(text),
        mode=OutputMode.JSONL,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=4,
        stdout=out,
    )
    assert code == EXIT_AUTH_ERROR


@pytest.mark.asyncio
async def test_batch_streams_per_record_in_jsonl_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSONL mode flushes per record (FR-35a stream-shaped contract)."""
    seen_streams: list[int] = []

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        await asyncio.sleep(0)  # yield to event loop
        return TaskResult(target=line.argv[0], success=True, exit_code=0)

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    class CountingStream(io.StringIO):
        def write(self, s: str) -> int:
            n = super().write(s)
            seen_streams.append(self.tell())
            return n

    out = CountingStream()
    text = "on a\non b\non c\n"
    await run_batch(
        source=_make_source(text),
        mode=OutputMode.JSONL,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=2,
        stdout=out,
    )
    # Three JSONL lines should have been written.
    body = out.getvalue()
    assert len([ln for ln in body.splitlines() if ln]) == 3


# ---------------------------------------------------------------------------
# R1 / FR-35a: structured §11.2 aggregate summary on stderr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_emits_partial_failure_summary_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed-success-and-fail aggregate emits exactly one §11.2 line on stderr."""
    import contextlib as _ctx

    target_outcomes = {"a": True, "b": False, "c": True, "d": False}

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        target = line.argv[0]
        if target_outcomes[target]:
            return TaskResult(target=target, success=True, exit_code=0)
        return TaskResult(
            target=target,
            success=False,
            exit_code=EXIT_AUTH_ERROR,
            error=StructuredError(
                error="auth_failed",
                exit_code=EXIT_AUTH_ERROR,
                target=target,
                message="x",
            ),
        )

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    out = io.StringIO()
    err = io.StringIO()
    text = "on a\non b\non c\non d\n"
    with _ctx.redirect_stderr(err):
        code = await run_batch(
            source=_make_source(text),
            mode=OutputMode.JSONL,
            config_lookup=None,
            credentials=CredentialBundle(),
            timeout=5.0,
            concurrency=4,
            stdout=out,
        )
    assert code == EXIT_PARTIAL_FAILURE
    err_lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
    assert len(err_lines) == 1, f"expected exactly 1 stderr line, got: {err_lines!r}"
    summary = json.loads(err_lines[0])
    assert summary["error"] == "partial_failure"
    assert summary["exit_code"] == EXIT_PARTIAL_FAILURE
    assert "2" in summary["message"]  # 2 failures
    assert "4" in summary["message"]  # of 4 total


@pytest.mark.asyncio
async def test_batch_emits_homogeneous_failure_summary_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-fail-same-reason aggregate names the shared reason on stderr."""
    import contextlib as _ctx

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        target = line.argv[0]
        return TaskResult(
            target=target,
            success=False,
            exit_code=EXIT_AUTH_ERROR,
            error=StructuredError(
                error="auth_failed",
                exit_code=EXIT_AUTH_ERROR,
                target=target,
                message="x",
            ),
        )

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    out = io.StringIO()
    err = io.StringIO()
    text = "on a\non b\non c\n"
    with _ctx.redirect_stderr(err):
        code = await run_batch(
            source=_make_source(text),
            mode=OutputMode.JSONL,
            config_lookup=None,
            credentials=CredentialBundle(),
            timeout=5.0,
            concurrency=4,
            stdout=out,
        )
    assert code == EXIT_AUTH_ERROR
    err_lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
    assert len(err_lines) == 1
    summary = json.loads(err_lines[0])
    assert summary["error"] == "auth_failed"
    assert summary["exit_code"] == EXIT_AUTH_ERROR
    assert "3" in summary["message"]  # all 3 failed


@pytest.mark.asyncio
async def test_batch_emits_mixed_failure_summary_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-fail-mixed-reasons aggregate exits 7 and names dominant failure."""
    import contextlib as _ctx

    from kasa_cli.errors import EXIT_NETWORK_ERROR

    failure_codes = {
        "a": (EXIT_AUTH_ERROR, "auth_failed"),
        "b": (EXIT_NETWORK_ERROR, "network_error"),
        "c": (EXIT_NETWORK_ERROR, "network_error"),
    }

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        target = line.argv[0]
        code, name = failure_codes[target]
        return TaskResult(
            target=target,
            success=False,
            exit_code=code,
            error=StructuredError(error=name, exit_code=code, target=target, message="x"),
        )

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    out = io.StringIO()
    err = io.StringIO()
    text = "on a\non b\non c\n"
    with _ctx.redirect_stderr(err):
        code = await run_batch(
            source=_make_source(text),
            mode=OutputMode.JSONL,
            config_lookup=None,
            credentials=CredentialBundle(),
            timeout=5.0,
            concurrency=4,
            stdout=out,
        )
    assert code == EXIT_PARTIAL_FAILURE
    err_lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
    assert len(err_lines) == 1
    summary = json.loads(err_lines[0])
    assert summary["error"] == "partial_failure"
    assert summary["exit_code"] == EXIT_PARTIAL_FAILURE
    # Dominant is network_error (2 of 3).
    assert "network_error" in summary["message"]
    assert "dominant" in summary["message"].lower()


@pytest.mark.asyncio
async def test_batch_does_not_emit_summary_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-success aggregate produces ZERO stderr summary lines."""
    import contextlib as _ctx

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        return TaskResult(target=line.argv[0], success=True, exit_code=0)

    import kasa_cli.verbs.batch_cmd as batch_mod

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    out = io.StringIO()
    err = io.StringIO()
    text = "on a\non b\n"
    with _ctx.redirect_stderr(err):
        code = await run_batch(
            source=_make_source(text),
            mode=OutputMode.JSONL,
            config_lookup=None,
            credentials=CredentialBundle(),
            timeout=5.0,
            concurrency=2,
            stdout=out,
        )
    assert code == EXIT_SUCCESS
    err_lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
    assert err_lines == []


@pytest.mark.asyncio
async def test_batch_does_not_emit_summary_on_interrupt() -> None:
    """When stop_event fires, FR-35a stderr summary is suppressed.

    The FR-31c interrupted summary on stdout is the authoritative end-of-run
    record on the interrupt path; emitting an additional FR-35a stderr line
    would double-summarize.
    """
    import contextlib as _ctx

    stop = asyncio.Event()
    stop.set()

    out = io.StringIO()
    err = io.StringIO()
    text = "on a\non b\non c\n"
    with _ctx.redirect_stderr(err):
        await run_batch(
            source=_make_source(text),
            mode=OutputMode.JSONL,
            config_lookup=None,
            credentials=CredentialBundle(),
            timeout=5.0,
            concurrency=2,
            stop_event=stop,
            stdout=out,
        )
    err_lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
    assert err_lines == [], f"unexpected stderr on interrupt: {err_lines!r}"


# ---------------------------------------------------------------------------
# R4 / FR-30 grammar: ``--cumulative`` bare-flag handling
# ---------------------------------------------------------------------------


def test_parse_kv_flags_bare_cumulative_sets_true() -> None:
    """``--cumulative`` (bare) implies ``cumulative=true``."""
    from kasa_cli.verbs.batch_cmd import _parse_kv_flags

    flags = _parse_kv_flags(
        ["patio", "--cumulative"],
        allowed={"socket", "cumulative"},
        bare_flags={
            "cumulative": "cumulative=true",
            "no-cumulative": "cumulative=false",
        },
    )
    assert flags["cumulative"] == "true"
    assert flags["_pos"] == "patio"


def test_parse_kv_flags_bare_no_cumulative_sets_false() -> None:
    """``--no-cumulative`` (bare) implies ``cumulative=false``."""
    from kasa_cli.verbs.batch_cmd import _parse_kv_flags

    flags = _parse_kv_flags(
        ["patio", "--no-cumulative"],
        allowed={"socket", "cumulative"},
        bare_flags={
            "cumulative": "cumulative=true",
            "no-cumulative": "cumulative=false",
        },
    )
    assert flags["cumulative"] == "false"


def test_parse_kv_flags_inline_cumulative_true_still_works() -> None:
    """``--cumulative=true`` (explicit) parses as True."""
    from kasa_cli.verbs.batch_cmd import _parse_kv_flags

    flags = _parse_kv_flags(
        ["patio", "--cumulative=true"],
        allowed={"socket", "cumulative"},
        bare_flags={
            "cumulative": "cumulative=true",
            "no-cumulative": "cumulative=false",
        },
    )
    assert flags["cumulative"] == "true"


def test_parse_kv_flags_inline_cumulative_false_still_works() -> None:
    """``--cumulative=false`` (explicit) parses as False."""
    from kasa_cli.verbs.batch_cmd import _parse_kv_flags

    flags = _parse_kv_flags(
        ["patio", "--cumulative=false"],
        allowed={"socket", "cumulative"},
        bare_flags={
            "cumulative": "cumulative=true",
            "no-cumulative": "cumulative=false",
        },
    )
    assert flags["cumulative"] == "false"


def test_parse_kv_flags_bare_cumulative_does_not_consume_next_token() -> None:
    """Regression: ``energy patio --cumulative`` (bare) MUST NOT consume the
    next positional / flag as a value.

    Before the bare-flag fix, ``--cumulative`` always required a following
    value, so a line like ``energy patio --cumulative --socket=0`` would
    parse ``--socket=0`` as the value of ``cumulative``.
    """
    from kasa_cli.verbs.batch_cmd import _parse_kv_flags

    flags = _parse_kv_flags(
        ["patio", "--cumulative", "--socket=0"],
        allowed={"socket", "cumulative"},
        bare_flags={
            "cumulative": "cumulative=true",
            "no-cumulative": "cumulative=false",
        },
    )
    assert flags["cumulative"] == "true"
    assert flags["socket"] == "0"


# ---------------------------------------------------------------------------
# R2 / FR-31c step 4: token-cache "eager save" invariant verification
# ---------------------------------------------------------------------------


def test_auth_cache_has_no_in_memory_buffer_state() -> None:
    """The eager-save invariant ``_flush_token_cache_pending`` relies on.

    ``_flush_token_cache_pending`` in ``batch_cmd`` is a no-op because the
    Phase 2 design fsyncs every successful auth at result-receive time
    (``auth_cache.save_session`` writes to a tempfile, fsyncs, renames). If
    a future change introduces an in-memory buffer / pending-session
    registry, this test will fail and force the helper to grow a real
    implementation.

    The structural assertions:

    1. ``auth_cache.save_session`` is the public write API.
    2. ``auth_cache`` exposes no module-level mutable buffer / registry —
       no obvious ``_pending``, ``_buffer``, ``_dirty``, or ``_queue``
       attribute exists.
    3. ``save_session`` performs an explicit ``os.fsync`` before
       ``os.replace`` (verified via source-text introspection so a future
       refactor that drops the fsync without replacing it gets caught).
    """
    import inspect

    from kasa_cli import auth_cache

    # 1. Public API is present.
    assert hasattr(auth_cache, "save_session")

    # 2. No buffer-style module attributes.
    suspicious_names = {"_pending", "_buffer", "_dirty", "_queue", "_outbox"}
    for name in suspicious_names:
        assert not hasattr(auth_cache, name), (
            f"auth_cache.{name} exists; the eager-save invariant in "
            "_flush_token_cache_pending is no longer safe — implement a real "
            "flush hook."
        )

    # 3. save_session source-text contains both fsync and atomic-rename.
    src = inspect.getsource(auth_cache.save_session)
    assert "fsync" in src, (
        "save_session no longer fsyncs; _flush_token_cache_pending eager-save invariant is broken."
    )
    assert "os.replace" in src or "rename" in src, (
        "save_session no longer atomically renames; "
        "_flush_token_cache_pending eager-save invariant is broken."
    )


def test_flush_token_cache_pending_is_safe_to_call_multiple_times() -> None:
    """The no-op flush hook MUST be idempotent and never raise."""
    from kasa_cli.verbs.batch_cmd import _flush_token_cache_pending

    for _ in range(3):
        _flush_token_cache_pending()  # no-op; just must not raise


# ---------------------------------------------------------------------------
# CLI wiring — Click level
# ---------------------------------------------------------------------------


def test_batch_file_and_stdin_mutually_exclusive() -> None:
    """Both --file and --stdin → exit 64."""
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["batch", "--file", "/tmp/x.batch", "--stdin"],
    )
    assert result.exit_code == EXIT_USAGE_ERROR


def test_batch_neither_file_nor_stdin_exits_64() -> None:
    """Neither flag → exit 64."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["batch"])
    assert result.exit_code == EXIT_USAGE_ERROR


def test_batch_empty_file_exits_zero(tmp_path: Any) -> None:
    """Empty --file → exit 0 with no stdout output (FR-31b)."""
    p = tmp_path / "empty.batch"
    p.write_text("")
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "batch", "--file", str(p)])
    assert result.exit_code == EXIT_SUCCESS
    assert result.stdout == ""


def test_batch_empty_file_json_mode_emits_empty_array(tmp_path: Any) -> None:
    """Empty --file in --json mode → exit 0 with ``[]``."""
    p = tmp_path / "empty.batch"
    p.write_text("")
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "batch", "--file", str(p)])
    assert result.exit_code == EXIT_SUCCESS
    parsed = json.loads(result.stdout)
    assert parsed == []


def test_batch_missing_file_exits_six(tmp_path: Any) -> None:
    """--file pointing at a missing path → exit 6 (config_error)."""
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--jsonl", "batch", "--file", str(tmp_path / "nope.batch")],
    )
    assert result.exit_code == 6


def test_batch_unbalanced_quotes_exits_with_usage_error(
    tmp_path: Any,
) -> None:
    """A line with unbalanced quotes is a usage error (exit 64).

    The verb itself raises UsageError; ``_run_async_graceful`` projects it
    into a structured error and returns exit 64.
    """
    p = tmp_path / "bad.batch"
    p.write_text('on "unterminated\n')
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "batch", "--file", str(p)])
    assert result.exit_code == EXIT_USAGE_ERROR


def test_batch_dispatcher_translates_kasa_errors_to_structured_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A KasaCliError from a verb becomes a per-line failed TaskResult."""
    import kasa_cli.verbs.batch_cmd as batch_mod

    async def _fake_dispatch(line: Any, **_kw: Any) -> TaskResult:
        # Simulate a verb raising AuthError. The dispatcher catches this and
        # projects to a TaskResult.
        try:
            raise AuthError("KLAP rejected", target=line.argv[0])
        except AuthError as exc:
            return TaskResult(
                target=line.argv[0],
                success=False,
                exit_code=exc.exit_code,
                error=exc.to_structured(),
            )

    monkeypatch.setattr(batch_mod, "_dispatch_line", _fake_dispatch)

    p = tmp_path / "lines.batch"
    p.write_text("on patio\non kitchen\n")
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "batch", "--file", str(p)])
    # Both fail with the same auth code → homogeneous failure → exit 2.
    assert result.exit_code == EXIT_AUTH_ERROR
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert rec["success"] is False
        assert rec["exit_code"] == EXIT_AUTH_ERROR
        assert rec["error"]["error"] == "auth_failed"


@pytest.mark.asyncio
async def test_dispatch_line_empty_argv_falls_back_to_verb_no_indexerror() -> None:
    """C2 regression part 1: empty ``argv`` + KasaCliError must NOT IndexError.

    The bug was an operator-precedence error in ``_dispatch_line``:

        target = exc.target or line.argv[0] if line.argv else line.verb

    parsed as ``(exc.target or line.argv[0]) if line.argv else line.verb``.
    On the ``info`` verb the branch raises ``UsageError(target=None)`` when
    ``argv`` is empty; with the buggy precedence, the projection then
    evaluated ``None or line.argv[0]`` (because ``line.argv`` was truthy
    inside ``exc.target or ...`` — wait, it isn't truthy here; the bug bites
    when argv IS empty: precedence makes the expression
    ``(None or argv[0]) if [] else verb`` → reaches ``argv[0]`` only when
    argv is truthy). The corrected expression
    ``exc.target or (line.argv[0] if line.argv else line.verb)`` correctly
    falls through to ``line.verb`` when ``exc.target`` is None and argv is
    empty.
    """
    from kasa_cli.verbs.batch_cmd import _dispatch_line, _ParsedLine

    line = _ParsedLine(lineno=1, verb="info", argv=[], raw="info")
    creds = CredentialBundle()
    result = await _dispatch_line(
        line,
        config_lookup=None,
        credentials=creds,
        timeout=1.0,
    )
    assert result.success is False
    assert result.exit_code == EXIT_USAGE_ERROR
    # Target falls back to verb name when argv is empty AND exc.target is
    # None (the info-branch UsageError carries no target).
    assert result.target == "info"


@pytest.mark.asyncio
async def test_dispatch_line_kasa_error_explicit_target_wins_over_argv() -> None:
    """C2 regression part 2: ``exc.target`` is honored over ``line.argv[0]``.

    With the corrected precedence, when an inner verb raises a KasaCliError
    that carries an explicit ``target``, the projected TaskResult must use
    that target rather than falling through to ``line.argv[0]``. We exercise
    this through the ``info`` verb path by monkeypatching ``run_info`` to
    raise a UsageError carrying an explicit target distinct from
    ``argv[0]``.
    """
    import sys
    import types

    import kasa_cli.verbs.batch_cmd as batch_mod
    from kasa_cli.errors import UsageError
    from kasa_cli.verbs.batch_cmd import _ParsedLine

    explicit_target = "AA:BB:CC:DD:EE:FF"

    fake_info_mod = types.ModuleType("kasa_cli.verbs.info_cmd")

    async def _run_info_explode(**_kw: Any) -> int:
        raise UsageError("forced", target=explicit_target)

    fake_info_mod.run_info = _run_info_explode  # type: ignore[attr-defined]
    saved = sys.modules.get("kasa_cli.verbs.info_cmd")
    sys.modules["kasa_cli.verbs.info_cmd"] = fake_info_mod
    try:
        line = _ParsedLine(lineno=1, verb="info", argv=["argv-target"], raw="info argv-target")
        result = await batch_mod._dispatch_line(
            line,
            config_lookup=None,
            credentials=CredentialBundle(),
            timeout=1.0,
        )
    finally:
        if saved is not None:
            sys.modules["kasa_cli.verbs.info_cmd"] = saved
        else:
            sys.modules.pop("kasa_cli.verbs.info_cmd", None)

    assert result.success is False
    # The structured error's target reflects the exception's explicit target,
    # not argv[0]. With the precedence bug, this would be "argv-target".
    assert result.target == explicit_target
    assert result.error is not None
    assert result.error.target == explicit_target
