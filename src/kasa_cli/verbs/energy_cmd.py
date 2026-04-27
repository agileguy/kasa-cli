"""``kasa-cli energy <target>`` (SRD §5.6, FR-21..23).

Single-shot mode emits one :class:`Reading` per §10.3. ``--watch <seconds>``
turns the verb into a JSONL stream of Readings at the requested interval.
``--cumulative``/``--no-cumulative`` toggles inclusion of ``today_kwh`` /
``month_kwh`` (default: cumulative for single-shot, **no-cumulative** for
``--watch`` per FR-22 — the cumulative fetch adds ~200ms per tick).

Per-socket readings on HS300 (``--socket N``) descend to ``kdev.children[N-1]``;
without ``--socket`` the strip total is returned (parent Energy module if
present, else the sum of child emeters — see ``wrapper.read_energy``).

Sub-second intervals are supported (``--watch 0.5``); the loop never truncates
to int. Phase 1 had a bug where ``int(timeout)`` quietly disabled sub-second
behaviour; this verb explicitly avoids that by using the float in
``asyncio.sleep`` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from kasa_cli import wrapper
from kasa_cli.errors import EXIT_SUCCESS, DeviceError
from kasa_cli.output import OutputMode, emit, emit_stream, reading_to_text
from kasa_cli.types import Reading
from kasa_cli.wrapper import CredentialBundle


async def _refresh(kdev: object, target: str) -> None:
    """Issue an ``update()`` and convert any error to ``DeviceError``."""
    update = getattr(kdev, "update", None)
    if update is None:
        return
    try:
        await update()
    except Exception as exc:
        raise DeviceError(
            f"Failed to refresh device state for {target!r}: {exc}",
            target=target,
        ) from exc


async def run_energy(
    target: str,
    *,
    watch_seconds: float | None,
    cumulative: bool,
    socket: int | None,
    config_lookup: Callable[[str], tuple[str | None, str | None]],
    credentials: CredentialBundle,
    timeout: float,
    mode: OutputMode,
    _max_ticks: int | None = None,
) -> int:
    """Execute the ``energy`` verb. Returns the desired exit code on success.

    Args:
        target: Alias / IP / MAC the user passed.
        watch_seconds: ``None`` for single-shot; ``> 0`` for JSONL stream.
        cumulative: Include ``today_kwh`` / ``month_kwh`` in the Reading.
        socket: 1-indexed socket on HS300; ``None`` for strip total or single
            socket.
        config_lookup: Closure resolving ``target`` to ``(host, alias)``.
        credentials: Pre-resolved credentials.
        timeout: Per-operation connect timeout.
        mode: Output mode for stdout.
        _max_ticks: Internal test hook. When set, the watch loop emits at most
            this many Readings before returning. Production callers SHOULD
            leave it ``None``; the loop runs until interrupted.

    Raises:
        UnsupportedFeatureError: Target lacks an Energy module (e.g., HS200,
            EP40M). Mapped to exit code 5 by the CLI dispatcher.
    """
    kdev = await wrapper.resolve_target(
        target,
        config_lookup=config_lookup,
        credentials=credentials,
        timeout=timeout,
    )
    _, alias_override = config_lookup(target)
    try:
        if watch_seconds is None:
            await _refresh(kdev, target)
            reading = await wrapper.read_energy(
                kdev,
                socket=socket,
                cumulative=cumulative,
                alias_override=alias_override,
            )
            emit(reading, mode, formatter=lambda r: reading_to_text(r))  # type: ignore[arg-type]
            return EXIT_SUCCESS

        # ``--watch`` mode: JSONL stream. We pass through ``mode`` unchanged
        # so ``--json`` mode still produces a single array (collected eagerly
        # via emit_stream); the streaming-by-default behaviour is what users
        # get on a pipe.
        async def _ticks() -> AsyncReadingIter:
            return AsyncReadingIter(
                kdev=kdev,
                target=target,
                socket=socket,
                cumulative=cumulative,
                alias_override=alias_override,
                interval=float(watch_seconds),
                max_ticks=_max_ticks,
            )

        readings_iter = await _ticks()
        # Collect into a list when emit_stream needs JSON-array semantics
        # (mode == JSON); for JSONL/TEXT we materialize anyway because
        # emit_stream is a sync iterator. The async iteration happens here.
        collected: list[Reading] = []
        async for r in readings_iter:
            collected.append(r)
        emit_stream(collected, mode, formatter=lambda r: reading_to_text(r))  # type: ignore[arg-type]
        return EXIT_SUCCESS
    finally:
        disconnect = getattr(kdev, "disconnect", None)
        if disconnect is not None:
            with contextlib.suppress(Exception):
                await disconnect()


class AsyncReadingIter:
    """Async iterator that yields one :class:`Reading` per ``interval`` seconds.

    Refreshes the device on each tick so the Energy module's properties are
    populated. Supports an optional ``max_ticks`` for tests.
    """

    __slots__ = (
        "_alias_override",
        "_count",
        "_cumulative",
        "_emitted",
        "_interval",
        "_kdev",
        "_max_ticks",
        "_socket",
        "_target",
    )

    def __init__(
        self,
        *,
        kdev: object,
        target: str,
        socket: int | None,
        cumulative: bool,
        alias_override: str | None,
        interval: float,
        max_ticks: int | None,
    ) -> None:
        self._kdev = kdev
        self._target = target
        self._socket = socket
        self._cumulative = cumulative
        self._alias_override = alias_override
        self._interval = max(0.0, interval)
        self._max_ticks = max_ticks
        self._count = 0
        self._emitted = 0

    def __aiter__(self) -> AsyncReadingIter:
        return self

    async def __anext__(self) -> Reading:
        if self._max_ticks is not None and self._emitted >= self._max_ticks:
            raise StopAsyncIteration
        if self._emitted > 0:
            # First tick is emitted immediately; subsequent ticks pace.
            await asyncio.sleep(self._interval)
        await _refresh(self._kdev, self._target)
        reading = await wrapper.read_energy(
            self._kdev,  # type: ignore[arg-type]
            socket=self._socket,
            cumulative=self._cumulative,
            alias_override=self._alias_override,
        )
        self._emitted += 1
        return reading
