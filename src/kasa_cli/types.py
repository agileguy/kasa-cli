"""Data classes for kasa-cli.

ENGINEER B STUB. Engineer A owns the authoritative version of this file in a
parallel worktree. The PM merges the two branches and keeps Engineer A's copy.
This stub exposes only the public surface (per SRD §10) that Engineer B's code
imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Socket:
    """A single outlet on a multi-socket strip (SRD §10.1)."""

    index: int
    alias: str
    state: str  # "on" | "off"


@dataclass
class Device:
    """Full device record (SRD §10.1)."""

    alias: str
    ip: str
    mac: str
    model: str
    hardware_version: str
    firmware_version: str
    protocol: str  # "legacy" | "klap"
    features: list[str]
    state: str  # "on" | "off" | "mixed"
    sockets: list[Socket] | None
    last_seen: str  # ISO-8601


@dataclass
class Group:
    """Local group definition (SRD §10.2)."""

    name: str
    members: list[str]


@dataclass
class Reading:
    """Energy reading for a device or strip socket (SRD §10.3)."""

    ts: str  # ISO-8601
    alias: str
    socket: int | None
    current_power_w: float
    voltage_v: float
    current_a: float
    today_kwh: float | None
    month_kwh: float | None
