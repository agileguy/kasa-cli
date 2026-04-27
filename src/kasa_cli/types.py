"""Public data model for kasa-cli (SRD §10).

Plain dataclasses, no Pydantic. Field names and units track python-kasa's
``Energy`` module so the wrapper layer (Engineer B) does not have to translate
unit conventions in either direction.

Design notes:

- ``Reading.today_kwh`` and ``Reading.month_kwh`` are ``float | None`` because
  the cumulative fetch (``get_daystat`` / ``get_monthstat``) is opt-in for
  ``--watch`` mode (FR-21a, FR-22) and may legitimately fail or be skipped.
- ``Device.sockets`` is ``list[Socket] | None`` — single-socket devices SHALL
  not surface an empty list; ``None`` means "not a multi-socket strip".
- All dataclasses are ``slots=True`` to keep allocation cost low and to catch
  attribute typos statically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger("kasa_cli")


ProtocolFamily = Literal["legacy", "klap"]
PowerState = Literal["on", "off", "mixed"]
SocketState = Literal["on", "off"]


@dataclass(slots=True)
class Socket:
    """One outlet on a multi-socket strip (HS300, KP303, KP400, EP40)."""

    index: int
    """1-based socket index. SRD §10.1."""

    alias: str
    """Per-socket human alias as stored on the device."""

    state: SocketState
    """Current power state of this socket."""


@dataclass(slots=True)
class Device:
    """Full device record (SRD §10.1).

    Populated from a single ``update()`` call against the device. ``sockets``
    is ``None`` for single-socket devices and bulbs; populated for strips.
    ``state`` becomes ``"mixed"`` when a strip's sockets disagree.
    """

    alias: str
    ip: str
    mac: str
    model: str
    hardware_version: str
    firmware_version: str
    protocol: ProtocolFamily
    state: PowerState
    last_seen: str
    """ISO-8601 timestamp of the most recent successful probe."""

    features: list[str] = field(default_factory=list)
    """E.g. ``["dimmable", "color", "color-temp", "energy-monitor"]``."""

    sockets: list[Socket] | None = None
    """``None`` for single-socket devices; populated for multi-socket strips."""


@dataclass(slots=True)
class Group:
    """A locally-defined group (SRD §10.2).

    Groups exist only in the CLI config (``[groups]`` table). Members are
    alias *names*, not resolved Device records — resolution happens at command
    execution time per FR-27.
    """

    name: str
    members: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Reading:
    """One energy snapshot (SRD §10.3).

    ``current_power_w``, ``voltage_v``, and ``current_a`` come from a single
    cheap ``update()``. ``today_kwh`` and ``month_kwh`` require additional
    ``get_daystat``/``get_monthstat`` calls (~200ms extra) and are nullable
    when ``--no-cumulative`` was passed or the cumulative fetch failed.
    """

    ts: str
    """ISO-8601 timestamp of the reading."""

    alias: str
    """Resolved alias for the device. Always populated; mandatory contract."""

    current_power_w: float
    voltage_v: float
    current_a: float

    socket: int | None = None
    """1-indexed socket number for HS300; ``None`` for single-socket devices."""

    today_kwh: float | None = None
    """Cumulative kWh for current local day. Nullable per FR-21 / FR-22."""

    month_kwh: float | None = None
    """Cumulative kWh for current local month. Nullable per FR-21 / FR-22."""


__all__ = [
    "Device",
    "Group",
    "PowerState",
    "ProtocolFamily",
    "Reading",
    "Socket",
    "SocketState",
]
