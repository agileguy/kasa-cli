"""Output formatting and emission for kasa-cli (SRD §5.10, §11.2).

Three output modes (``OutputMode``):

* ``TEXT`` — fixed-width human-readable on tty (default when stdout is a tty
  and neither ``--json`` nor ``--jsonl`` is set).
* ``JSON`` — pretty multi-line JSON with ``indent=2``.
* ``JSONL`` — one JSON object per line, no trailing whitespace; default when
  stdout is a pipe.

``--quiet`` suppresses stdout entirely; only the exit code communicates.

Strict invariant (FR-35a): in ``JSON`` and ``JSONL`` modes, **every** byte
written to stdout MUST round-trip through ``json.loads``. We validate before
writing so a programming error here cannot ever produce malformed JSON.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, TextIO

from kasa_cli.errors import StructuredError
from kasa_cli.types import Device, Reading


class OutputMode(Enum):
    """Output rendering mode for stdout."""

    TEXT = "text"
    JSON = "json"
    JSONL = "jsonl"
    QUIET = "quiet"


# --- Mode detection -----------------------------------------------------------


def detect_mode(
    *,
    json_flag: bool,
    jsonl_flag: bool,
    quiet: bool,
    stream: TextIO | None = None,
) -> OutputMode:
    """Resolve flags + tty state into a single :class:`OutputMode`.

    Precedence: ``--quiet`` > ``--json`` > ``--jsonl`` > tty/pipe heuristic.
    """
    if quiet:
        return OutputMode.QUIET
    if json_flag:
        return OutputMode.JSON
    if jsonl_flag:
        return OutputMode.JSONL
    s = stream if stream is not None else sys.stdout
    if hasattr(s, "isatty") and s.isatty():
        return OutputMode.TEXT
    return OutputMode.JSONL


# --- Serialization helpers ----------------------------------------------------


def _to_jsonable(item: object) -> Any:
    """Convert a dataclass / mapping / scalar into a JSON-safe Python value."""
    if is_dataclass(item) and not isinstance(item, type):
        return _to_jsonable(asdict(item))
    if isinstance(item, dict):
        return {str(k): _to_jsonable(v) for k, v in item.items()}
    if isinstance(item, (list, tuple)):
        return [_to_jsonable(v) for v in item]
    if isinstance(item, (str, int, float, bool)) or item is None:
        return item
    return str(item)


def _safe_dumps(payload: object, *, pretty: bool) -> str:
    """Dump and round-trip-validate (FR-35a). Returns the validated string."""
    jsonable = _to_jsonable(payload)
    if pretty:
        text = json.dumps(jsonable, indent=2, sort_keys=True)
    else:
        text = json.dumps(jsonable, separators=(",", ":"), sort_keys=True)
    # FR-35a: belt-and-suspenders — if the round-trip fails, raise, never
    # spew malformed JSON to stdout.
    json.loads(text)
    return text


# --- Text formatters ----------------------------------------------------------


def device_to_text(device: Device) -> str:
    """Render a Device record as one line of human-readable text."""
    state = device.state
    return (
        f"{device.alias or '-':<16} {device.ip:<15} {device.mac:<17} "
        f"{device.model:<8} {device.protocol:<6} {state}"
    )


def list_view_to_text(item: dict[str, Any]) -> str:
    """Render one list-view entry (FR-6b shape)."""
    online = item.get("online")
    online_s = "-" if online is None else ("yes" if online else "no")
    return (
        f"{item.get('alias', '-') or '-':<16} "
        f"{item.get('ip', '-') or '-':<15} "
        f"{item.get('mac', '-') or '-':<17} {online_s}"
    )


def reading_to_text(reading: Reading) -> str:
    """Render an energy Reading as a single text line."""
    socket = f" socket={reading.socket}" if reading.socket is not None else ""
    cumulative = ""
    if reading.today_kwh is not None or reading.month_kwh is not None:
        today = "-" if reading.today_kwh is None else f"{reading.today_kwh:.3f}"
        month = "-" if reading.month_kwh is None else f"{reading.month_kwh:.3f}"
        cumulative = f"  today={today}kWh  month={month}kWh"
    return (
        f"[{reading.ts}] {reading.alias}{socket}  "
        f"power={reading.current_power_w:.1f}W  "
        f"voltage={reading.voltage_v:.1f}V  "
        f"current={reading.current_a:.3f}A{cumulative}"
    )


# --- Emission -----------------------------------------------------------------


def emit(
    item: object,
    mode: OutputMode,
    *,
    formatter: Callable[[object], str],
    stream: TextIO | None = None,
) -> None:
    """Emit a single record in the requested mode."""
    s = stream if stream is not None else sys.stdout
    if mode is OutputMode.QUIET:
        return
    if mode is OutputMode.TEXT:
        s.write(formatter(item))
        s.write("\n")
        return
    if mode is OutputMode.JSON:
        s.write(_safe_dumps(item, pretty=True))
        s.write("\n")
        return
    # JSONL
    s.write(_safe_dumps(item, pretty=False))
    s.write("\n")


def emit_one(
    item: object,
    mode: OutputMode,
    *,
    formatter: Callable[[object], str],
    stream: TextIO | None = None,
) -> None:
    """Emit a single streaming record AND flush the stream.

    Same rendering as :func:`emit` for ``TEXT`` / ``JSONL`` (a single line per
    item) but with an explicit ``flush()`` after the write so live consumers
    (e.g. ``--watch`` tailers, downstream pipes) see each tick the moment it
    lands rather than after the underlying buffer fills.

    ``JSON`` mode emits the bare item as a pretty-printed top-level value; this
    is the right shape for a one-shot fetch but is NOT meaningful for streamed
    iteration. Callers streaming under ``JSON`` mode should buffer and call
    :func:`emit_stream` once at the end so the output is a single JSON array
    (which is the `--json` contract). ``QUIET`` writes nothing.
    """
    s = stream if stream is not None else sys.stdout
    if mode is OutputMode.QUIET:
        return
    if mode is OutputMode.TEXT:
        s.write(formatter(item))
        s.write("\n")
        s.flush()
        return
    if mode is OutputMode.JSON:
        s.write(_safe_dumps(item, pretty=True))
        s.write("\n")
        s.flush()
        return
    # JSONL
    s.write(_safe_dumps(item, pretty=False))
    s.write("\n")
    s.flush()


def emit_stream(
    items: Iterable[object],
    mode: OutputMode,
    *,
    formatter: Callable[[object], str],
    stream: TextIO | None = None,
) -> None:
    """Emit a stream of records.

    In ``JSON`` mode we collect into a single array and emit pretty-printed
    once. In ``JSONL`` mode each item gets its own validated line. ``TEXT``
    delegates each item to ``formatter``. ``QUIET`` writes nothing.
    """
    s = stream if stream is not None else sys.stdout
    if mode is OutputMode.QUIET:
        return
    if mode is OutputMode.JSON:
        materialized = list(items)
        s.write(_safe_dumps(materialized, pretty=True))
        s.write("\n")
        return
    if mode is OutputMode.JSONL:
        for item in items:
            s.write(_safe_dumps(item, pretty=False))
            s.write("\n")
        return
    # TEXT
    for item in items:
        s.write(formatter(item))
        s.write("\n")


def emit_error(
    err: StructuredError,
    mode: OutputMode,
    *,
    stream: TextIO | None = None,
) -> None:
    """Emit a :class:`StructuredError` to stderr (SRD §11.2).

    Always emitted as JSON regardless of ``mode``. ``--quiet`` does NOT
    suppress structured errors — operators still need to know why the exit
    code is non-zero.

    Uses :meth:`StructuredError.to_json` so null optional fields (``target``,
    ``hint``) are omitted per SRD §11.2's example shape. FR-35a is enforced by
    a defensive ``json.loads`` round-trip immediately before write — a
    malformed payload raises rather than reaching stdout/stderr.
    """
    del mode  # All modes emit the same JSON envelope.
    s = stream if stream is not None else sys.stderr
    text = err.to_json()
    json.loads(text)  # FR-35a guard — never spew malformed JSON.
    s.write(text)
    s.write("\n")
