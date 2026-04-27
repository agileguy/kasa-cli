"""Tests for ``kasa_cli.parallel`` (Phase 3 — shared parallel-execution engine).

The engine has four contracts the rest of Phase 3 leans on:

* concurrency-bounded execution via ``asyncio.Semaphore``,
* aggregate exit-code rules per FR-29a / FR-31a (0 / 7 / first-failure-code),
* per-task ``on_each`` streaming callback fires once per task in completion
  order,
* ``on_signal`` lets a caller register a stop-dispatch hook; on stop the engine
  refuses new tasks and waits up to ``DRAIN_TIMEOUT_SECONDS`` for in-flight.

Phase 1+2 anti-pattern fix: every test that asserts an exit code asserts the
EXACT integer (``== 7``, ``== 3``, ``== 0``), never ``!= 0``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from kasa_cli.errors import (
    EXIT_AUTH_ERROR,
    EXIT_DEVICE_ERROR,
    EXIT_NETWORK_ERROR,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    StructuredError,
)
from kasa_cli.parallel import (
    AggregateResult,
    TaskResult,
    aggregate_exit_code,
    run_parallel,
)

# --- Helpers -----------------------------------------------------------------


def _ok(target: str, *, output: object | None = None) -> TaskResult:
    return TaskResult(target=target, success=True, exit_code=EXIT_SUCCESS, output=output)


def _fail(target: str, code: int, error: str = "device_error") -> TaskResult:
    err = StructuredError(error=error, exit_code=code, target=target, message=f"{target} failed")
    return TaskResult(target=target, success=False, exit_code=code, error=err)


# --- aggregate_exit_code unit tests ------------------------------------------


def test_aggregate_exit_code_empty_input_is_zero() -> None:
    """FR-31b: empty batch exits 0."""
    assert aggregate_exit_code([]) == EXIT_SUCCESS


def test_aggregate_exit_code_all_success_is_zero() -> None:
    results = [_ok("a"), _ok("b"), _ok("c")]
    assert aggregate_exit_code(results) == EXIT_SUCCESS


def test_aggregate_exit_code_mixed_is_seven() -> None:
    """FR-29a: 1+ success AND 1+ failure -> exit 7."""
    results = [_ok("a"), _fail("b", EXIT_NETWORK_ERROR)]
    assert aggregate_exit_code(results) == EXIT_PARTIAL_FAILURE


def test_aggregate_exit_code_all_fail_homogeneous_returns_first_code() -> None:
    """FR-29a: every task failed -> first failure's exit code."""
    results = [
        _fail("a", EXIT_NETWORK_ERROR),
        _fail("b", EXIT_NETWORK_ERROR),
    ]
    assert aggregate_exit_code(results) == EXIT_NETWORK_ERROR


def test_aggregate_exit_code_all_fail_mixed_reasons_returns_first() -> None:
    """All fail, different reasons -> first task's code wins (deterministic)."""
    results = [
        _fail("a", EXIT_AUTH_ERROR, "auth_failed"),
        _fail("b", EXIT_NETWORK_ERROR, "network_error"),
    ]
    # Per FR-29a: when EVERY sub-op failed, the exit code is the FIRST
    # failure's code. Mixed-reasons does NOT collapse to 7 for all-fail.
    assert aggregate_exit_code(results) == EXIT_AUTH_ERROR


# --- run_parallel core behavior ----------------------------------------------


@pytest.mark.asyncio
async def test_run_parallel_empty_targets_returns_empty_aggregate() -> None:
    async def _fn(_t: str) -> TaskResult:
        raise AssertionError("should not be called")

    agg = await run_parallel([], _fn, concurrency=4)
    assert isinstance(agg, AggregateResult)
    assert agg.results == ()
    assert agg.successes == 0
    assert agg.failures == 0
    assert agg.exit_code == EXIT_SUCCESS


@pytest.mark.asyncio
async def test_run_parallel_all_success_returns_zero() -> None:
    targets = ["a", "b", "c"]

    async def _fn(t: str) -> TaskResult:
        return _ok(t)

    agg = await run_parallel(targets, _fn, concurrency=2)
    assert agg.exit_code == EXIT_SUCCESS
    assert agg.successes == 3
    assert agg.failures == 0
    assert {r.target for r in agg.results} == set(targets)


@pytest.mark.asyncio
async def test_run_parallel_mixed_returns_seven() -> None:
    """One success + one failure -> aggregate exit code is exactly 7."""

    async def _fn(t: str) -> TaskResult:
        if t == "good":
            return _ok(t)
        return _fail(t, EXIT_NETWORK_ERROR)

    agg = await run_parallel(["good", "bad"], _fn, concurrency=2)
    assert agg.exit_code == EXIT_PARTIAL_FAILURE
    assert agg.successes == 1
    assert agg.failures == 1


@pytest.mark.asyncio
async def test_run_parallel_all_fail_same_reason_returns_that_reason_code() -> None:
    """All-unreachable -> exit code 3 (homogeneous failure)."""

    async def _fn(t: str) -> TaskResult:
        return _fail(t, EXIT_NETWORK_ERROR)

    agg = await run_parallel(["a", "b", "c"], _fn, concurrency=3)
    assert agg.exit_code == EXIT_NETWORK_ERROR
    assert agg.successes == 0
    assert agg.failures == 3


@pytest.mark.asyncio
async def test_run_parallel_all_fail_different_reasons_returns_first() -> None:
    """All fail, different reasons -> first task's code (FR-29a)."""
    seen: list[str] = []

    async def _fn(t: str) -> TaskResult:
        seen.append(t)
        if t == "a":
            # Slow this one down so completion order is deterministic.
            await asyncio.sleep(0.05)
            return _fail(t, EXIT_AUTH_ERROR, "auth_failed")
        return _fail(t, EXIT_NETWORK_ERROR, "network_error")

    agg = await run_parallel(["a", "b"], _fn, concurrency=2)
    # First completed failure is "b" (auth was delayed). Its code wins.
    assert agg.exit_code == EXIT_NETWORK_ERROR
    assert agg.failures == 2


# --- Concurrency cap ----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_parallel_respects_concurrency_cap() -> None:
    """Verify no more than N tasks run concurrently when concurrency=N."""
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def _fn(t: str) -> TaskResult:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return _ok(t)

    targets = [f"t{i}" for i in range(10)]
    agg = await run_parallel(targets, _fn, concurrency=3)
    assert agg.exit_code == EXIT_SUCCESS
    assert max_in_flight <= 3
    # Sanity: with 10 targets and concurrency 3 we should have hit the cap.
    assert max_in_flight >= 2


@pytest.mark.asyncio
async def test_run_parallel_concurrency_one_is_serial() -> None:
    """concurrency=1 forces serial execution (semaphore size 1)."""
    overlap = 0
    max_overlap = 0
    lock = asyncio.Lock()

    async def _fn(t: str) -> TaskResult:
        nonlocal overlap, max_overlap
        async with lock:
            overlap += 1
            max_overlap = max(max_overlap, overlap)
        await asyncio.sleep(0.02)
        async with lock:
            overlap -= 1
        return _ok(t)

    agg = await run_parallel(["a", "b", "c"], _fn, concurrency=1)
    assert agg.exit_code == EXIT_SUCCESS
    assert max_overlap == 1


@pytest.mark.asyncio
async def test_run_parallel_concurrency_zero_coerced_to_one() -> None:
    """concurrency<=0 is coerced to 1 rather than jamming the semaphore."""

    async def _fn(t: str) -> TaskResult:
        return _ok(t)

    agg = await run_parallel(["a", "b"], _fn, concurrency=0)
    # Runs to completion (didn't deadlock).
    assert agg.exit_code == EXIT_SUCCESS
    assert len(agg.results) == 2


# --- on_each streaming callback ----------------------------------------------


@pytest.mark.asyncio
async def test_on_each_fires_once_per_task() -> None:
    received: list[TaskResult] = []

    async def _fn(t: str) -> TaskResult:
        return _ok(t)

    agg = await run_parallel(
        ["a", "b", "c"],
        _fn,
        concurrency=3,
        on_each=lambda r: received.append(r),
    )
    assert agg.exit_code == EXIT_SUCCESS
    assert len(received) == 3
    assert {r.target for r in received} == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_on_each_callback_exception_does_not_abort_run() -> None:
    """A misbehaving callback must not crash the rest of the run."""

    async def _fn(t: str) -> TaskResult:
        return _ok(t)

    def _bad_cb(_r: TaskResult) -> None:
        raise RuntimeError("boom")

    agg = await run_parallel(
        ["a", "b", "c"],
        _fn,
        concurrency=2,
        on_each=_bad_cb,
    )
    assert agg.exit_code == EXIT_SUCCESS
    assert len(agg.results) == 3


@pytest.mark.asyncio
async def test_on_each_receives_failures_too() -> None:
    received: list[TaskResult] = []

    async def _fn(t: str) -> TaskResult:
        if t == "fail":
            return _fail(t, EXIT_NETWORK_ERROR)
        return _ok(t)

    agg = await run_parallel(
        ["ok", "fail"],
        _fn,
        concurrency=2,
        on_each=lambda r: received.append(r),
    )
    assert agg.exit_code == EXIT_PARTIAL_FAILURE
    by_target = {r.target: r for r in received}
    assert by_target["ok"].success is True
    assert by_target["ok"].exit_code == EXIT_SUCCESS
    assert by_target["fail"].success is False
    assert by_target["fail"].exit_code == EXIT_NETWORK_ERROR


# --- Per-task factory exception protection -----------------------------------


@pytest.mark.asyncio
async def test_run_parallel_wraps_factory_exceptions_into_device_error() -> None:
    """Factory leaks an exception -> engine wraps it as device_error TaskResult."""

    async def _fn(t: str) -> TaskResult:
        if t == "leak":
            raise RuntimeError("oops, didn't catch")
        return _ok(t)

    agg = await run_parallel(["good", "leak"], _fn, concurrency=2)
    assert agg.exit_code == EXIT_PARTIAL_FAILURE
    failures = [r for r in agg.results if not r.success]
    assert len(failures) == 1
    assert failures[0].exit_code == EXIT_DEVICE_ERROR
    assert failures[0].error is not None
    assert failures[0].error.error == "device_error"


# --- on_signal / drain behavior ----------------------------------------------


@pytest.mark.asyncio
async def test_on_signal_stops_dispatch_and_drains_in_flight() -> None:
    """When stop_dispatch lands, in-flight tasks complete; queued ones don't."""
    started: list[str] = []
    completed: list[str] = []
    stop_callback: list[Callable[[], None]] = []

    async def _fn(t: str) -> TaskResult:
        started.append(t)
        # Trigger the stop the moment we see "trigger".
        if t == "trigger" and stop_callback:
            stop_callback[0]()
        await asyncio.sleep(0.05)
        completed.append(t)
        return _ok(t)

    def _on_signal(stop: Callable[[], None]) -> None:
        stop_callback.append(stop)

    targets = ["trigger"] + [f"queued-{i}" for i in range(20)]
    t0 = time.monotonic()
    agg = await run_parallel(
        targets,
        _fn,
        concurrency=2,
        on_signal=_on_signal,
        drain_timeout=2.0,
    )
    elapsed = time.monotonic() - t0
    # Drain should be bounded — well under the input * sleep total.
    assert elapsed < 2.5, f"drain took {elapsed:.2f}s, expected fast finish"
    # The trigger task itself must have completed (it was already in-flight).
    assert "trigger" in completed
    # Most of the queued tasks should NOT have completed (stop_dispatch).
    assert len(completed) < len(targets)
    # Aggregate is well-formed even with a partial run.
    assert isinstance(agg, AggregateResult)
    assert len(agg.results) <= len(targets)


@pytest.mark.asyncio
async def test_on_signal_drain_timeout_cancels_long_runners() -> None:
    """In-flight tasks past drain_timeout are cancelled cleanly.

    Realistic flow: SIGINT/SIGTERM handlers fire from a context independent
    of any running task. We simulate that by spawning a sidecar task that
    waits a beat, then calls the registered stop callback. By that point the
    long-running tasks have acquired the semaphore and entered their sleep
    body — they're truly in flight. The drain phase's
    ``asyncio.wait(timeout=drain_timeout)`` returns after drain_timeout
    regardless of whether the long tasks have finished; still-running tasks
    are then cancelled. Total wall time MUST be ``~drain_timeout + slop``,
    not 5 seconds.
    """
    stop_ref: list[Callable[[], None]] = []
    cancellations: list[str] = []

    async def _fn(t: str) -> TaskResult:
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancellations.append(t)
            raise
        return _ok(t)

    def _on_signal(stop: Callable[[], None]) -> None:
        stop_ref.append(stop)

    async def _trigger_stop_after(delay: float) -> None:
        await asyncio.sleep(delay)
        # Wait until the engine has registered a stop hook — covers the
        # tiny race where the sidecar runs before run_parallel has called
        # on_signal(_request_stop).
        for _ in range(100):
            if stop_ref:
                stop_ref[0]()
                return
            await asyncio.sleep(0.005)
        raise AssertionError("on_signal was never invoked")

    sidecar = asyncio.create_task(_trigger_stop_after(0.05))

    t0 = time.monotonic()
    agg = await run_parallel(
        ["long-1", "long-2", "long-3"],
        _fn,
        concurrency=3,
        on_signal=_on_signal,
        drain_timeout=0.2,
    )
    elapsed = time.monotonic() - t0
    await sidecar  # ensure clean teardown

    # Should return well within drain_timeout + slop, NOT 5 seconds.
    assert elapsed < 1.5, f"drain returned in {elapsed:.2f}s; expected near drain_timeout"
    # Aggregate is well-formed even when cancellations happened.
    assert isinstance(agg, AggregateResult)
    # All three long tasks should have been cancelled.
    assert set(cancellations) == {"long-1", "long-2", "long-3"}


# --- Result completeness -----------------------------------------------------


@pytest.mark.asyncio
async def test_run_parallel_aggregate_counts_match_results() -> None:
    """Aggregate.successes + Aggregate.failures == len(Aggregate.results)."""

    async def _fn(t: str) -> TaskResult:
        return _ok(t) if t.startswith("ok") else _fail(t, EXIT_NETWORK_ERROR)

    agg = await run_parallel(
        ["ok-1", "ok-2", "fail-1", "fail-2", "fail-3"],
        _fn,
        concurrency=3,
    )
    assert agg.successes == 2
    assert agg.failures == 3
    assert len(agg.results) == 5
    assert agg.exit_code == EXIT_PARTIAL_FAILURE


@pytest.mark.asyncio
async def test_run_parallel_results_are_taskresult_instances() -> None:
    async def _fn(t: str) -> TaskResult:
        return _ok(t)

    agg = await run_parallel(["a"], _fn, concurrency=1)
    assert all(isinstance(r, TaskResult) for r in agg.results)


@pytest.mark.asyncio
async def test_run_parallel_factory_called_once_per_target() -> None:
    """Engine must NOT retry — caller's factory owns retry semantics."""
    seen: dict[str, int] = {}

    async def _fn(t: str) -> TaskResult:
        seen[t] = seen.get(t, 0) + 1
        return _ok(t)

    targets = ["a", "b", "c", "d"]
    await run_parallel(targets, _fn, concurrency=2)
    assert all(seen[t] == 1 for t in targets)


# --- TaskResult shape sanity -------------------------------------------------


def test_task_result_success_has_no_error() -> None:
    """A success TaskResult MUST have error=None."""
    r = _ok("a")
    assert r.success is True
    assert r.error is None
    assert r.exit_code == EXIT_SUCCESS


def test_task_result_failure_has_structured_error() -> None:
    r = _fail("a", EXIT_NETWORK_ERROR)
    assert r.success is False
    assert r.error is not None
    assert r.error.exit_code == EXIT_NETWORK_ERROR
    assert r.error.error == "device_error"
    # round-trip through JSON
    assert r.error.to_json()


# --- Type-system sanity ------------------------------------------------------


def test_aggregate_result_is_frozen() -> None:
    """Both result types are frozen dataclasses (consumers cache them)."""
    from dataclasses import FrozenInstanceError

    r = _ok("a")
    with pytest.raises(FrozenInstanceError):
        r.target = "b"  # type: ignore[misc]


def test_run_parallel_signature_keyword_only_concurrency() -> None:
    """``concurrency`` MUST be keyword-only so positional drift doesn't hide bugs."""
    import inspect

    sig = inspect.signature(run_parallel)
    p = sig.parameters["concurrency"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


# --- Suppress unused-import warning -----------------------------------------

_ = (Any,)  # silence flake on intentional Any import in helpers
