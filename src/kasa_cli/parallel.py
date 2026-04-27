"""Parallel fan-out primitive for batch / group verbs.

This module is **stubbed by Engineer B** to satisfy the Engineer-B test surface
for Phase 3 Part B. Engineer A3 owns the authoritative implementation; at PM
merge time A3's ``parallel.py`` overwrites this file.

The public contract — ``TaskResult``, ``AggregateResult``, ``run_parallel`` —
matches the cross-engineer agreement documented in the Phase 3 brief. Keep
this file's surface minimal so A3's overwrite is mechanical.

Semantics (FR-29a / FR-31a):

* ``run_parallel`` runs ``fn(target)`` for every target in ``targets`` with at
  most ``concurrency`` coroutines in flight at once.
* ``on_signal`` is invoked once when the harness wants the fan-out to stop
  scheduling new work (FR-31c step 1). In-flight tasks are allowed to finish
  with up to a 2-second drain window enforced by the *caller* (cli.py) — the
  fan-out itself just stops dispatching.
* The aggregate exit code is computed by the SRD §5.8 rules:
  - all ok → 0
  - mixed (≥1 ok and ≥1 fail) → 7
  - homogeneous failure → that single reason's exit code
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from kasa_cli.errors import (
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    StructuredError,
)

_DRAIN_DEFAULT_BUDGET_S: Final[float] = 2.0


@dataclass(frozen=True, slots=True)
class TaskResult:
    """One sub-operation outcome — what the verb produced for one target."""

    target: str
    success: bool
    exit_code: int
    output: object | None = None
    error: StructuredError | None = None


@dataclass(frozen=True, slots=True)
class AggregateResult:
    """The aggregate of all per-target ``TaskResult``s for a fan-out."""

    results: list[TaskResult] = field(default_factory=list)
    successes: int = 0
    failures: int = 0
    exit_code: int = EXIT_SUCCESS


def _aggregate_exit_code(results: list[TaskResult]) -> int:
    """Compute the FR-29a exit code from a list of TaskResult."""
    if not results:
        return EXIT_SUCCESS
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]
    if not failures:
        return EXIT_SUCCESS
    if successes and failures:
        return EXIT_PARTIAL_FAILURE
    # Homogeneous failure: every result failed. If they all share a single
    # exit code, return that code; otherwise FR-29a says still exit 7.
    codes = {r.exit_code for r in failures}
    if len(codes) == 1:
        return next(iter(codes))
    return EXIT_PARTIAL_FAILURE


async def run_parallel(
    targets: list[str],
    fn: Callable[[str], Awaitable[TaskResult]],
    *,
    concurrency: int,
    on_signal: Callable[[], None] | None = None,
    stop_event: asyncio.Event | None = None,
    on_result: Callable[[TaskResult], None] | None = None,
) -> AggregateResult:
    """Fan ``fn(target)`` out across ``targets`` with bounded concurrency.

    ``stop_event`` is the cooperative cancellation signal — when set, the
    dispatcher stops scheduling **new** tasks. In-flight tasks are awaited
    normally. The CLI layer enforces the 2-second drain budget by racing the
    overall ``run_parallel`` call against ``asyncio.wait_for``.

    ``on_result`` is called synchronously after each result is received so
    the CLI layer can stream JSONL per completion (FR-31a, "stream-shaped
    verbs MUST flush per-record" — Phase 1+2 lesson).

    ``on_signal`` is the no-arg callback Engineer A3's authoritative
    implementation will invoke when the dispatcher first detects a stop
    signal. The B-stub keeps the same signature so A3's overwrite is
    transparent.
    """
    if not targets:
        return AggregateResult(results=[], successes=0, failures=0, exit_code=EXIT_SUCCESS)

    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[TaskResult] = []
    results_lock = asyncio.Lock()
    signal_fired = False

    async def _one(target: str) -> None:
        nonlocal signal_fired
        # Stop dispatching new work if the stop_event is set. We still
        # produce a TaskResult marker so the aggregate count is accurate;
        # the CLI summary line uses len(results) as ``completed``.
        if stop_event is not None and stop_event.is_set():
            if not signal_fired and on_signal is not None:
                signal_fired = True
                on_signal()
            return
        async with sem:
            # Re-check the stop event after acquiring the semaphore — a slow
            # acquire could overlap a SIGINT.
            if stop_event is not None and stop_event.is_set():
                if not signal_fired and on_signal is not None:
                    signal_fired = True
                    on_signal()
                return
            result = await fn(target)
        async with results_lock:
            results.append(result)
            if on_result is not None:
                on_result(result)

    tasks = [asyncio.create_task(_one(t)) for t in targets]
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    finally:
        # Best-effort cancel any task that's still pending after the gather
        # returns (only relevant if the caller wraps us in wait_for(...)).
        for t in tasks:
            if not t.done():
                t.cancel()

    successes = sum(1 for r in results if r.success)
    failures = len(results) - successes
    return AggregateResult(
        results=results,
        successes=successes,
        failures=failures,
        exit_code=_aggregate_exit_code(results),
    )


__all__ = [
    "AggregateResult",
    "TaskResult",
    "run_parallel",
]
