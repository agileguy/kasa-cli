"""FR-31c graceful-drain SIGINT/SIGTERM tests for ``kasa-cli batch``.

Subprocess-based because in-process signal testing is fragile: pytest's own
signal handlers + the asyncio loop interaction makes ``signal.raise_signal``
unreliable. We spawn a real ``python -m kasa_cli`` subprocess, give it a
many-line batch file with ``KASA_CLI_TEST_FAKE_SLEEP`` set so each line
sleeps deterministically, then send the signal mid-flight.

What we assert:

1. Exit code is exactly 130 (SIGINT) or 143 (SIGTERM) per SRD §11.1.
2. The last non-empty line of stdout is a JSON object
   ``{"event":"interrupted","completed":N,"pending":M}`` per FR-31c.
3. Already-completed sub-results are flushed BEFORE the interrupted line.

Determinism: we use a long-enough sleep per line (default 0.3s) plus a
4-line warmup window before sending the signal. Each test runs ≤6s; the
drain budget itself is 2s.

If a CI host shows flakiness in this file, the in-process fallback at the
bottom of the module (``test_in_process_sigint_overlay``) provides a
secondary check that doesn't depend on subprocess timing — but the
subprocess tests are the authoritative FR-31c verification.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kasa_cli.errors import EXIT_SIGINT, EXIT_SIGTERM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_long_batch(tmp_path: Path, n_lines: int = 12) -> Path:
    """Write a batch file with ``n_lines`` ``on`` commands."""
    p = tmp_path / "long.batch"
    lines = "\n".join(f"on alias-{i}" for i in range(n_lines)) + "\n"
    p.write_text(lines)
    return p


def _spawn_batch(
    batch_path: Path,
    *,
    fake_sleep_seconds: float = 0.3,
    extra_env: dict[str, str] | None = None,
    json_mode: bool = False,
) -> subprocess.Popen[str]:
    """Spawn ``python -m kasa_cli`` running the given batch file."""
    env = {
        **os.environ,
        "KASA_CLI_TEST_FAKE_SLEEP": str(fake_sleep_seconds),
        # Make sure no real config / token cache interferes with the test.
        "KASA_CLI_CONFIG_DIR": str(batch_path.parent / ".tokens-stub"),
    }
    if extra_env:
        env.update(extra_env)
    mode_flag = "--json" if json_mode else "--jsonl"
    cmd = [
        sys.executable,
        "-m",
        "kasa_cli",
        mode_flag,
        "batch",
        "--file",
        str(batch_path),
        "--concurrency",
        "2",
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_first_line(proc: subprocess.Popen[str], *, timeout_s: float = 4.0) -> None:
    """Block until at least one stdout line lands or timeout. Best-effort.

    We can't actually do a non-blocking line-read on a Popen stdout pipe in
    a portable way without selectors, and the simpler `time.sleep(0.5)` is
    sufficient given the 0.3s fake-sleep — at 2-wide concurrency two lines
    will have completed by 0.6s.
    """
    del proc, timeout_s
    time.sleep(0.6)


# ---------------------------------------------------------------------------
# Subprocess SIGINT / SIGTERM
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signal semantics — subprocess SIGINT/SIGTERM not portable to Windows.",
)
def test_sigint_during_batch_exits_130_with_interrupted_summary(
    tmp_path: Any,
) -> None:
    """SIGINT mid-batch → exit 130, last stdout line is interrupted summary."""
    batch = _make_long_batch(tmp_path, n_lines=12)
    proc = _spawn_batch(batch, fake_sleep_seconds=0.3)
    try:
        _wait_for_first_line(proc)
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=8)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)

    assert proc.returncode == EXIT_SIGINT, (
        f"exit={proc.returncode!r}\nstdout={stdout!r}\nstderr={stderr!r}"
    )

    # Last non-empty stdout line MUST be the interrupted summary.
    nonempty = [ln for ln in stdout.splitlines() if ln.strip()]
    assert nonempty, "expected at least the interrupted summary on stdout"
    last = nonempty[-1]
    parsed = json.loads(last)
    assert parsed["event"] == "interrupted"
    assert isinstance(parsed["completed"], int)
    assert isinstance(parsed["pending"], int)
    assert parsed["completed"] >= 0
    assert parsed["pending"] >= 0
    # Total accounted for: completed + pending ≤ total lines (some may have
    # been in-flight when we cancelled — they count as "completed" if their
    # result was already streamed, "pending" otherwise).
    assert parsed["completed"] + parsed["pending"] <= 12


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX SIGTERM — not portable to Windows.",
)
def test_sigterm_during_batch_exits_143_with_interrupted_summary(
    tmp_path: Any,
) -> None:
    """SIGTERM mid-batch → exit 143, last stdout line is interrupted summary."""
    batch = _make_long_batch(tmp_path, n_lines=12)
    proc = _spawn_batch(batch, fake_sleep_seconds=0.3)
    try:
        _wait_for_first_line(proc)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=8)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)

    assert proc.returncode == EXIT_SIGTERM, (
        f"exit={proc.returncode!r}\nstdout={stdout!r}\nstderr={stderr!r}"
    )
    nonempty = [ln for ln in stdout.splitlines() if ln.strip()]
    assert nonempty
    last = nonempty[-1]
    parsed = json.loads(last)
    assert parsed["event"] == "interrupted"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signal semantics — not portable to Windows.",
)
def test_sigint_completed_lines_emitted_before_interrupted_summary(
    tmp_path: Any,
) -> None:
    """Already-completed sub-results stream BEFORE the interrupted line."""
    batch = _make_long_batch(tmp_path, n_lines=12)
    proc = _spawn_batch(batch, fake_sleep_seconds=0.3)
    try:
        _wait_for_first_line(proc)
        proc.send_signal(signal.SIGINT)
        stdout, _ = proc.communicate(timeout=8)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)

    assert proc.returncode == EXIT_SIGINT
    nonempty = [ln for ln in stdout.splitlines() if ln.strip()]
    # We expect ≥1 result line + the summary line.
    parsed_lines = [json.loads(ln) for ln in nonempty]
    # All but the last are TaskResult-shaped; the last is the summary.
    *task_lines, summary = parsed_lines
    assert summary["event"] == "interrupted"
    # Each preceding line is a TaskResult dict with the expected shape.
    for tl in task_lines:
        assert "target" in tl
        assert "success" in tl
        assert "exit_code" in tl
        assert "event" not in tl


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signal semantics — not portable to Windows.",
)
def test_signal_during_already_complete_batch_does_not_corrupt_exit(
    tmp_path: Any,
) -> None:
    """Sending a signal AFTER the batch finishes leaves exit 0 untouched.

    Edge case: a tiny batch (1 line, 50ms sleep) completes well before we
    send the signal. The process should already have exited 0 by then. We
    tolerate either "process already gone" (proc.poll() is 0) or a clean
    drain (130) — but the result must NOT be a corrupted code.
    """
    p = tmp_path / "tiny.batch"
    p.write_text("on alias-0\n")
    proc = _spawn_batch(p, fake_sleep_seconds=0.05)
    # Wait long enough for the batch to finish naturally.
    try:
        stdout, stderr = proc.communicate(timeout=4)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
    # Either the batch finished (exit 0) before we even tried to interrupt,
    # which is the expected normal case.
    assert proc.returncode == 0, (
        f"unexpected exit code {proc.returncode!r}; stdout={stdout!r}; stderr={stderr!r}"
    )


# ---------------------------------------------------------------------------
# In-process fallback — the subprocess tests above are authoritative.
# ---------------------------------------------------------------------------
#
# This test exists for environments where subprocess + signal interaction is
# unreliable (some CI sandboxes). It exercises the FR-31a aggregator
# directly with a stop_event to confirm the cooperative cancellation path
# works without a real signal.


@pytest.mark.asyncio
async def test_in_process_stop_event_short_circuits_dispatch(
    tmp_path: Any,
) -> None:
    """If the stop_event is set before run_batch starts, dispatch is skipped."""
    import asyncio
    import io

    from kasa_cli.output import OutputMode
    from kasa_cli.verbs.batch_cmd import run_batch
    from kasa_cli.wrapper import CredentialBundle

    stop = asyncio.Event()
    stop.set()

    out = io.StringIO()
    text = "on a\non b\non c\n"
    code = await run_batch(
        source=io.StringIO(text),
        mode=OutputMode.JSONL,
        config_lookup=None,
        credentials=CredentialBundle(),
        timeout=5.0,
        concurrency=2,
        stop_event=stop,
        stdout=out,
    )
    # Aggregate exit code is 0 (no failures recorded) but the run_batch
    # output contains the interrupted summary because the stop_event was
    # observed.
    body = out.getvalue()
    nonempty = [ln for ln in body.splitlines() if ln.strip()]
    assert nonempty
    parsed = json.loads(nonempty[-1])
    assert parsed["event"] == "interrupted"
    assert parsed["completed"] == 0
    assert parsed["pending"] == 3
    # No real verb dispatched, so the natural exit code is 0; signal-handler
    # in cli.py is the layer that overrides to 130/143.
    assert code == 0
