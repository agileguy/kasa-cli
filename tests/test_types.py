"""Tests for kasa_cli.types — dataclass shape sanity (SRD §10)."""

from __future__ import annotations

from dataclasses import fields

from kasa_cli.types import Device, Group, Reading, Socket


def test_socket_round_trips_basic_fields() -> None:
    sock = Socket(index=1, alias="lamp", state="on")
    assert sock.index == 1
    assert sock.alias == "lamp"
    assert sock.state == "on"


def test_device_single_socket_has_none_sockets() -> None:
    """Single-socket devices SHALL surface ``sockets=None``, not an empty list."""
    dev = Device(
        alias="kitchen-lamp",
        ip="192.168.1.42",
        mac="AA:BB:CC:DD:EE:01",
        model="KL130",
        hardware_version="1.0",
        firmware_version="1.5.7",
        protocol="legacy",
        state="on",
        last_seen="2026-04-27T20:11:00Z",
    )
    assert dev.sockets is None
    assert dev.features == []


def test_device_multi_socket_strip() -> None:
    """HS300/KP400-style strips populate sockets; state may be ``mixed``."""
    sockets = [
        Socket(index=1, alias="modem", state="on"),
        Socket(index=2, alias="router", state="off"),
    ]
    dev = Device(
        alias="office-strip",
        ip="192.168.1.51",
        mac="AA:BB:CC:DD:EE:02",
        model="HS300",
        hardware_version="2.0",
        firmware_version="1.0.10",
        protocol="legacy",
        state="mixed",
        last_seen="2026-04-27T20:11:00Z",
        sockets=sockets,
    )
    assert dev.sockets is not None
    assert len(dev.sockets) == 2
    assert dev.state == "mixed"


def test_group_default_members_is_empty_list() -> None:
    g = Group(name="bedroom-lights")
    assert g.members == []


def test_group_with_members() -> None:
    g = Group(name="bedroom-lights", members=["bedroom-lamp", "hallway-strip"])
    assert g.members == ["bedroom-lamp", "hallway-strip"]


def test_reading_allows_none_for_cumulative_fields() -> None:
    """today_kwh and month_kwh SHALL be ``float | None`` (FR-21, FR-22, §10.3)."""
    r = Reading(
        ts="2026-04-27T20:11:00Z",
        alias="office-strip",
        current_power_w=42.1,
        voltage_v=120.2,
        current_a=0.35,
    )
    assert r.today_kwh is None
    assert r.month_kwh is None
    assert r.socket is None


def test_reading_with_per_socket_and_cumulative() -> None:
    """Per-socket reading on HS300 with full cumulative data populated."""
    r = Reading(
        ts="2026-04-27T20:11:00Z",
        alias="office-strip",
        current_power_w=42.1,
        voltage_v=120.2,
        current_a=0.35,
        socket=2,
        today_kwh=1.234,
        month_kwh=18.567,
    )
    assert r.socket == 2
    assert r.today_kwh == 1.234
    assert r.month_kwh == 18.567


# ---------------------------------------------------------------------------
# Field-set stability — guards against accidental rename in §10.x
# ---------------------------------------------------------------------------


def test_device_field_names_match_srd() -> None:
    expected = {
        "alias",
        "ip",
        "mac",
        "model",
        "hardware_version",
        "firmware_version",
        "protocol",
        "state",
        "last_seen",
        "features",
        "sockets",
    }
    assert {f.name for f in fields(Device)} == expected


def test_socket_field_names_match_srd() -> None:
    expected = {"index", "alias", "state"}
    assert {f.name for f in fields(Socket)} == expected


def test_reading_field_names_match_srd() -> None:
    expected = {
        "ts",
        "alias",
        "current_power_w",
        "voltage_v",
        "current_a",
        "socket",
        "today_kwh",
        "month_kwh",
    }
    assert {f.name for f in fields(Reading)} == expected


def test_group_field_names_match_srd() -> None:
    expected = {"name", "members"}
    assert {f.name for f in fields(Group)} == expected
