"""Shared parallel-execution engine for ``groups`` fanout and ``batch`` verbs.

This module exists so that two independent code paths — the ``@group`` /
``--group`` target syntax (this engineer's territory) and the ``batch`` verb
(Engineer B3's territory) — share one well-tested concurrency implementation
with consistent semantics for:

* concurrency caps (``asyncio.Semaphore``-bounded dispatch),
* per-task structured-error reporting (SRD §11.2),
* graceful drain on SIGINT/SIGTERM (FR-31c — stop dispatching, wait up to 2s
  for in-flight tasks),
* aggregate exit-code rules (FR-29a / FR-31a / SRD §11.1 — 0; 7 for partial
  or mixed-reason failures; the shared reason's code for homogeneous
  all-failure).

API contract for callers
------------------------

The caller hands :func:`run_parallel` a list of targets and a coroutine
factory ``fn(target) -> TaskResult``. The factory is responsible for ALL of
its own error handling — when a per-target operation fails, it returns a
:class:`TaskResult` with ``success=False`` rather than raising. (Exceptions
that escape the factory ARE caught here and converted to a generic
``device_error`` :class:`TaskResult`, but the recommended pattern is for
callers to catch their own ``KasaCliError``s and shape a clean per-target
record.)

Streaming
~~~~~~~~~

Per-task results stream through the optional ``on_each: Callable[[TaskResult],
None]`` callback the moment each task completes. The callback fires from the
event loop, so callers MUST keep it cheap and non-blocking — typically just
``output.emit_one(result, mode, ...)``.

Why a sync callback rather than an ``async iter_results`` generator? Three
reasons:

1. The CLI's ``emit_one`` is already a synchronous write+flush — wrapping it
   in an async generator would just add latency before each tick reaches
   stdout.
2. The signal-handler hook ``on_signal`` is also a sync callback; keeping the
   API symmetric (two sync callbacks) is easier to reason about than one
   sync hook + one async iterator.
3. Avoids materializing an extra ``asyncio.Queue`` plus its consumer task,
   which would add a second hop and complicate cancellation semantics during
   the 2-second drain.

Engineer B3 contract
--------------------

The ``on_signal`` parameter is the integration point with the top-level
SIGINT/SIGTERM handler that B3 plumbs into ``cli.py``. When the signal lands,
B3's handler calls ``on_signal()`` (synchronously); we set an internal
``_stop_dispatch`` flag, cancel the dispatch loop, and wait up to 2 seconds
for in-flight tasks to finish. Already-completed tasks have already streamed
through ``on_each``; the aggregate's ``results`` list contains exactly what
streamed — no duplication, no loss.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, TextIO, cast

from kasa_cli.errors import (
    EXIT_DEVICE_ERROR,
    EXIT_PARTIAL_FAILURE,
    EXIT_SUCCESS,
    StructuredError,
)

__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "AggregateResult",
    "TaskResult",
    "aggregate_exit_code",
    "build_aggregate_summary_error",
    "emit_aggregate_summary_to_stderr",
    "run_parallel",
]


# Per FR-31c: when SIGINT/SIGTERM lands, stop dispatching new tasks but wait up
# to this many seconds for in-flight tasks to complete before returning.
DRAIN_TIMEOUT_SECONDS: Final[float] = 2.0


@dataclass(frozen=True)
class TaskResult:
    """One per-target result inside a parallel run.

    Attributes:
        target: The raw string the operator typed (alias / IP / MAC). Stable
            across success and failure so JSONL consumers can correlate.
        success: ``True`` iff the underlying op completed without raising.
        exit_code: ``0`` on success; otherwise the SRD §11.1 code the per-task
            error mapped to (3 for network, 2 for auth, etc.).
        output: The raw value returned by the per-task coroutine. ``None`` for
            verbs that emit nothing (``on``/``off``/``toggle``).
        error: The §11.2-shaped error envelope on failure; ``None`` on success.
    """

    target: str
    success: bool
    exit_code: int
    output: object | None = None
    error: StructuredError | None = None


@dataclass(frozen=True)
class AggregateResult:
    """The aggregate of every :class:`TaskResult` in a parallel run.

    Exit-code rules (FR-29a / FR-31a / SRD §11.1 closing paragraph):
        * ``0`` if every task succeeded.
        * ``7`` (partial failure) if at least one succeeded AND at least one
          failed.
        * If every task failed for the **same** reason, the shared reason's
          exit code (e.g. all-unreachable returns ``3``, all-unauthorized
          returns ``2``).
        * If every task failed for **different** reasons (mixed failure
          reasons), the exit code is ``7`` per SRD §11.1; the structured
          stderr summary names the dominant failure.
    """

    results: tuple[TaskResult, ...]
    successes: int
    failures: int
    exit_code: int


@dataclass
class _RunState:
    """Internal mutable state for one :func:`run_parallel` invocation.

    Kept in a dataclass rather than as locals so :func:`_signal_relay` can
    flip the flag without nonlocal gymnastics.
    """

    stop_dispatch: bool = False
    pending: int = 0
    results: list[TaskResult] = field(default_factory=list)
    completion_order: list[str] = field(
        default_factory=list,
    )  # target order for stable first-failure semantics
    stop_event: asyncio.Event | None = None  # asyncio.Event signaling stop


def aggregate_exit_code(results: list[TaskResult] | tuple[TaskResult, ...]) -> int:
    """Compute the aggregate exit code from a list of per-task results.

    Public so callers can re-derive an exit code without rebuilding an
    :class:`AggregateResult` (e.g. ``cli.py`` rolling up an early-return after
    `--concurrency 0` was passed). Empty input is ``0`` per FR-31b.

    SRD §11.1 closing paragraph rules:

    * Empty results -> 0.
    * All success -> 0.
    * Some success, some failure -> 7 (partial failure).
    * All failure, **homogeneous reasons** -> the shared reason's exit code.
    * All failure, **mixed reasons** -> 7 (partial failure); the structured
      stderr summary names the dominant failure.
    """
    if not results:
        return EXIT_SUCCESS

    successes = sum(1 for r in results if r.success)
    failures = len(results) - successes

    if failures == 0:
        return EXIT_SUCCESS
    if successes > 0:
        return EXIT_PARTIAL_FAILURE
    # All failed. Homogeneous reason -> that reason's code.
    # Mixed reasons -> EXIT_PARTIAL_FAILURE per SRD §11.1.
    failure_codes = {r.exit_code for r in results if not r.success}
    if len(failure_codes) == 1:
        return next(iter(failure_codes))
    return EXIT_PARTIAL_FAILURE


async def run_parallel(
    targets: list[str],
    fn: Callable[[str], Awaitable[TaskResult]],
    *,
    concurrency: int,
    on_each: Callable[[TaskResult], None] | None = None,
    on_signal: Callable[[Callable[[], None]], None] | None = None,
    drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
) -> AggregateResult:
    """Run ``fn(target)`` for every target with a bounded concurrency cap.

    Args:
        targets: Operator-typed strings (aliases, IPs, etc.). Empty list is
            allowed and returns an empty :class:`AggregateResult` with exit 0.
        fn: Async factory producing one :class:`TaskResult` per target. The
            factory is responsible for catching its own per-target errors and
            shaping a ``success=False`` :class:`TaskResult`. Exceptions that
            escape are caught here and converted to a ``device_error`` shape.
        concurrency: Max number of tasks running concurrently. Must be >= 1;
            values <= 0 are coerced to 1 (a more strident validation belongs
            in the verb layer where it can produce a usage-error exit code).
        on_each: Optional sync callback fired the moment each task completes.
            Use this to stream JSONL/TEXT results to stdout per FR-35a. Keep
            it cheap — it runs from the event loop. ``None`` disables
            streaming and only the final aggregate is returned.
        on_signal: Optional sync hook giving the caller a way to register a
            "stop_dispatch" callable with their own SIGINT/SIGTERM handler.
            The caller invokes the registered callable when the signal lands;
            this engine then stops dispatching new tasks and waits up to
            ``drain_timeout`` seconds for in-flight tasks to complete.
        drain_timeout: Seconds to wait for in-flight tasks after a stop signal
            before forcefully cancelling. Defaults to FR-31c's 2.0.

    Returns:
        AggregateResult: ``results`` is in completion order (NOT input order)
        because per-task streaming via ``on_each`` is the canonical
        observable. The aggregate exit code follows :func:`aggregate_exit_code`
        — homogeneous all-failure surfaces the shared reason's code; mixed
        all-failure (or any partial failure) surfaces ``7`` per SRD §11.1.
    """
    # Empty input — FR-31b's "empty batch exits 0" lives at the verb layer,
    # but we honor it here too for symmetry.
    if not targets:
        return AggregateResult(results=(), successes=0, failures=0, exit_code=EXIT_SUCCESS)

    # FR-28 default is 10 — but we don't enforce a default here; the caller
    # passes whatever they resolved (CLI flag > config > built-in). We just
    # guard against degenerate values that would jam the semaphore.
    bounded_concurrency = max(1, concurrency)

    state = _RunState()
    state.stop_event = asyncio.Event()
    sem = asyncio.Semaphore(bounded_concurrency)

    def _request_stop() -> None:
        """Set the stop-dispatch flag. Called by ``on_signal`` consumers.

        Sets both a plain bool (cheap to check inside ``_wrapped``) and an
        ``asyncio.Event`` (so the dispatch loop can ``wait`` on it without
        polling). The event is created lazily when ``run_parallel`` is
        awaited so it lives on the same event loop the engine runs on.
        """
        state.stop_dispatch = True
        if state.stop_event is not None:
            # Setting an already-set Event is a cheap no-op.
            state.stop_event.set()

    if on_signal is not None:
        # Caller hands the flag-flipper into their signal handler.
        on_signal(_request_stop)

    async def _wrapped(target: str) -> TaskResult:
        """Run one task under the semaphore, with bullet-proof error capture."""
        async with sem:
            # Re-check after acquiring the semaphore — if a stop signal landed
            # while we were queued behind other tasks, skip dispatching this
            # one entirely. The caller will see ``failures == 0, successes ==
            # 0`` for skipped targets (i.e. they don't appear in results at
            # all). Per FR-31c the interrupted summary line on the verb layer
            # records the (completed, pending) count.
            if state.stop_dispatch:
                # Sentinel: raise CancelledError so gather collects it without
                # treating it as a real failure.
                raise asyncio.CancelledError("stop_dispatch flagged before dispatch")
            try:
                result = await fn(target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Caller's per-target factory leaked. Best-effort wrap so the
                # aggregate is still well-formed.
                err = StructuredError(
                    error="device_error",
                    exit_code=EXIT_DEVICE_ERROR,
                    target=target,
                    message=f"unhandled error in parallel task: {type(exc).__name__}: {exc}",
                )
                result = TaskResult(
                    target=target,
                    success=False,
                    exit_code=EXIT_DEVICE_ERROR,
                    error=err,
                )
        return result

    # Dispatch loop.
    #
    # We launch every task up front (subject to the semaphore) rather than
    # gating dispatch on ``state.stop_dispatch`` between launches. Reason:
    # ``asyncio.create_task`` is cheap and the semaphore inside ``_wrapped``
    # is what bounds actual concurrency — checking the flag at SEM ACQUIRE
    # time gives us correct skip behavior with no extra dispatcher loop. If a
    # stop signal lands mid-run, in-flight tasks (those past the sem.acquire)
    # finish naturally, and queued tasks early-out with CancelledError.
    pending: list[asyncio.Task[TaskResult]] = []
    for target in targets:
        if state.stop_dispatch:
            break  # operator hit Ctrl-C before we even started
        task = asyncio.create_task(_wrapped(target), name=f"parallel:{target}")
        pending.append(task)

    # Stream per-completion (FR-35a) — but break out IMMEDIATELY when stop
    # fires, even if no task has completed yet (FR-31c says we cease
    # dispatch and drain in-flight; we shouldn't block on completions during
    # the drain). We race ``asyncio.wait(FIRST_COMPLETED)`` against the stop
    # event each iteration.
    #
    # Type-system note: ``asyncio.wait`` is generic over a single Future
    # type. We mix per-target ``Task[TaskResult]`` with a ``Task[bool]`` for
    # the stop event, so we erase to ``asyncio.Future[Any]`` at the
    # ``asyncio.wait`` boundary and re-narrow when consuming results.
    pending_set: set[asyncio.Task[TaskResult]] = set(pending)
    stop_waiter: asyncio.Task[bool] | None = None
    try:
        while pending_set:
            if state.stop_event is None:
                # Defensive — should always be set above.
                break
            if stop_waiter is None or stop_waiter.done():
                stop_waiter = asyncio.create_task(state.stop_event.wait())
            wait_set: set[asyncio.Future[object]] = set()
            for t in pending_set:
                wait_set.add(cast(asyncio.Future[object], t))
            wait_set.add(cast(asyncio.Future[object], stop_waiter))
            done, _waiting = await asyncio.wait(
                wait_set,
                return_when=asyncio.FIRST_COMPLETED,
            )
            stop_fired_this_round = cast(asyncio.Future[object], stop_waiter) in done
            for fut in done:
                if fut is cast(asyncio.Future[object], stop_waiter):
                    continue
                task = cast(asyncio.Task[TaskResult], fut)
                pending_set.discard(task)
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception:
                    # Defensive — _wrapped already shapes errors.
                    continue
                state.results.append(result)
                if on_each is not None:
                    with contextlib.suppress(Exception):
                        on_each(result)
            if stop_fired_this_round:
                break
    except asyncio.CancelledError:
        # Outer cancellation (rare): treat as stop_dispatch.
        state.stop_dispatch = True
    finally:
        if stop_waiter is not None and not stop_waiter.done():
            stop_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await stop_waiter

    # Drain phase — only entered if a stop signal was raised. Wait up to
    # ``drain_timeout`` for any task still in flight; cancel the rest.
    if state.stop_dispatch:
        in_flight: list[asyncio.Task[TaskResult]] = [t for t in pending if not t.done()]
        if in_flight:
            drain_done, drain_still = await asyncio.wait(
                in_flight,
                timeout=max(0.0, drain_timeout),
                return_when=asyncio.ALL_COMPLETED,
            )
            for done_fut in drain_done:
                try:
                    drain_result = done_fut.result()
                except asyncio.CancelledError:
                    continue
                except Exception:
                    # Defensive — _wrapped already shapes errors; this is for
                    # the truly unexpected.
                    continue
                state.results.append(drain_result)
                if on_each is not None:
                    with contextlib.suppress(Exception):
                        on_each(drain_result)
            for still_fut in drain_still:
                still_fut.cancel()
            # Give cancellations a chance to settle so we don't leak
            # "Task was destroyed but it is pending!" warnings.
            if drain_still:
                await asyncio.gather(*drain_still, return_exceptions=True)
    else:
        # No stop signal — but we may have left tasks unfinished if the
        # outer loop bailed for any other reason. Make sure every pending
        # task is awaited so we don't leak warnings.
        leftover: list[asyncio.Task[TaskResult]] = [t for t in pending if not t.done()]
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)
            for leftover_task in leftover:
                if leftover_task.cancelled():
                    continue
                exc = leftover_task.exception()
                if exc is not None:
                    continue
                try:
                    state.results.append(leftover_task.result())
                except Exception:
                    continue

    # Build the aggregate.
    results_tuple = tuple(state.results)
    successes = sum(1 for r in results_tuple if r.success)
    failures = len(results_tuple) - successes
    exit_code = aggregate_exit_code(state.results)
    return AggregateResult(
        results=results_tuple,
        successes=successes,
        failures=failures,
        exit_code=exit_code,
    )


# ---------------------------------------------------------------------------
# FR-35a aggregate summary -> stderr (SRD §11.2)
# ---------------------------------------------------------------------------


def build_aggregate_summary_error(
    agg: AggregateResult,
    *,
    total_inputs: int | None = None,
) -> StructuredError | None:
    """Build the SRD §11.2 stderr summary error for a non-zero aggregate.

    Returns ``None`` when the aggregate succeeded (caller should not emit).

    Shape selection (SRD §11.1 closing paragraph):

    * Some success + some failure -> ``error="partial_failure"``,
      ``exit_code=7``, message names success/failure counts.
    * All failure, **homogeneous** reason -> ``error=<that reason>``,
      ``exit_code=<that reason's code>``, message names the count.
    * All failure, **mixed** reasons -> ``error="partial_failure"``,
      ``exit_code=7``, message names the dominant failure.
    * Zero results (vacuous failure: e.g. dispatch was halted before any
      task ran) -> ``error="partial_failure"``, ``exit_code=7``, message
      records the truncation.

    ``total_inputs``, when supplied, lets the caller distinguish "we
    received N inputs but only saw M results" (relevant when the engine
    short-circuited dispatch) from a clean run of M inputs.
    """
    if agg.exit_code == EXIT_SUCCESS:
        return None

    successes = agg.successes
    failures = agg.failures
    n_total = total_inputs if total_inputs is not None else (successes + failures)

    # Vacuous-failure shape (no results recorded but we still exited non-zero).
    if successes == 0 and failures == 0:
        return StructuredError(
            error="partial_failure",
            exit_code=EXIT_PARTIAL_FAILURE,
            message=(
                f"Aggregate run produced no results (expected {n_total} target(s)); "
                "exit code reflects truncation."
            ),
        )

    if successes > 0 and failures > 0:
        return StructuredError(
            error="partial_failure",
            exit_code=EXIT_PARTIAL_FAILURE,
            message=(f"{failures} of {n_total} target(s) failed; {successes} succeeded."),
        )

    # All failed.
    failure_codes = {r.exit_code for r in agg.results if not r.success}
    failure_names = [r.error.error for r in agg.results if r.error is not None]
    if len(failure_codes) == 1:
        sole_code = next(iter(failure_codes))
        # Pick a single error name if uniform; else fall back to device_error.
        unique_names = set(failure_names)
        err_name = next(iter(unique_names)) if len(unique_names) == 1 else "device_error"
        return StructuredError(
            error=err_name,
            exit_code=sole_code,
            message=f"All {failures} target(s) failed with the same reason ({err_name}).",
        )

    # Mixed failure reasons -> exit 7, name dominant failure.
    counter = Counter(failure_names)
    if counter:
        dominant_name, dominant_count = counter.most_common(1)[0]
    else:  # pragma: no cover — defensive
        dominant_name, dominant_count = "device_error", failures
    return StructuredError(
        error="partial_failure",
        exit_code=EXIT_PARTIAL_FAILURE,
        message=(
            f"All {failures} target(s) failed with mixed reasons; "
            f"dominant: {dominant_name} ({dominant_count} of {failures})."
        ),
    )


def emit_aggregate_summary_to_stderr(
    agg: AggregateResult,
    *,
    total_inputs: int | None = None,
    stream: TextIO | None = None,
) -> None:
    """Emit the FR-35a / SRD §11.2 aggregate summary to stderr (one line).

    No-op when ``agg.exit_code == EXIT_SUCCESS``. Callers MUST suppress this
    when their run was interrupted by SIGINT/SIGTERM — the FR-31c
    interrupted-summary line on stdout already covers that case.
    """
    err = build_aggregate_summary_error(agg, total_inputs=total_inputs)
    if err is None:
        return
    target_stream = stream if stream is not None else sys.stderr
    target_stream.write(err.to_json())
    target_stream.write("\n")
    target_stream.flush()
