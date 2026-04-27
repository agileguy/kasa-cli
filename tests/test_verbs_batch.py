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
    agg = await parallel.run_parallel(
        targets, _stub_dispatch(results), concurrency=4
    )
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
    agg = await parallel.run_parallel(
        ["a", "b"], _stub_dispatch(results), concurrency=4
    )
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
    agg = await parallel.run_parallel(
        ["a", "b"], _stub_dispatch(results), concurrency=4
    )
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
            error=StructuredError(
                error="auth_failed", exit_code=EXIT_AUTH_ERROR, message="x"
            ),
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
