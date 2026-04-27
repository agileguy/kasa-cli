"""Tests for the ``energy`` verb (Phase 2 Engineer B).

Covers FR-21 (single-shot Reading), FR-22 (--watch JSONL stream and the
no-cumulative default), FR-23 (unsupported devices exit 5 — including the
EP40M-specific path), and the per-socket HS300 readout.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from kasa_cli.errors import EXIT_SUCCESS, UnsupportedFeatureError
from kasa_cli.output import OutputMode
from kasa_cli.types import Reading
from kasa_cli.verbs.energy_cmd import run_energy
from kasa_cli.wrapper import CredentialBundle


def _make_lookup(
    target_to_host: dict[str, str],
) -> Callable[[str], tuple[str | None, str | None]]:
    def _lookup(target: str) -> tuple[str | None, str | None]:
        if target in target_to_host:
            return target_to_host[target], target
        if target.count(".") == 3:
            return target, None
        raise KeyError(target)

    return _lookup


# --- single-shot --------------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_single_shot_kp115(
    monkeypatch: pytest.MonkeyPatch,
    kp115_with_emeter: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-21: single Reading is emitted with all four primary fields."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return kp115_with_emeter

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_energy(
        target="kitchen-plug",
        watch_seconds=None,
        cumulative=True,
        socket=None,
        config_lookup=_make_lookup({"kitchen-plug": "192.168.1.42"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["alias"] == "kitchen-plug"
    assert parsed["socket"] is None
    assert parsed["current_power_w"] == pytest.approx(12.5)
    assert parsed["voltage_v"] == pytest.approx(120.1)
    assert parsed["current_a"] == pytest.approx(0.105)
    assert parsed["today_kwh"] == pytest.approx(0.250)
    assert parsed["month_kwh"] == pytest.approx(7.500)


# --- per-socket HS300 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_hs300_per_socket(
    monkeypatch: pytest.MonkeyPatch,
    hs300_with_emeters: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-21 + per-socket: ``--socket 1`` returns child[0]'s emeter."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_with_emeters

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_energy(
        target="office-strip",
        watch_seconds=None,
        cumulative=False,
        socket=1,
        config_lookup=_make_lookup({"office-strip": "192.168.1.51"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["socket"] == 1
    assert parsed["alias"] == "socket-a"
    assert parsed["current_power_w"] == pytest.approx(12.5)
    # Cumulative omitted with cumulative=False.
    assert parsed["today_kwh"] is None
    assert parsed["month_kwh"] is None


@pytest.mark.asyncio
async def test_energy_hs300_strip_total_sums_children(
    monkeypatch: pytest.MonkeyPatch,
    hs300_with_emeters: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --socket on HS300 with no parent emeter → sum of child powers."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_with_emeters

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_energy(
        target="office-strip",
        watch_seconds=None,
        cumulative=False,
        socket=None,
        config_lookup=_make_lookup({"office-strip": "192.168.1.51"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["socket"] is None
    # 12.5 + 5.0 + 0.0 = 17.5
    assert parsed["current_power_w"] == pytest.approx(17.5)


# --- unsupported devices ------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_ep40m_exits_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    ep40m_no_emeter: Any,
) -> None:
    """FR-23: EP40M raises UnsupportedFeatureError (exit 5) with EP40M hint."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return ep40m_no_emeter

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError) as ei:
        await run_energy(
            target="patio-ep40m",
            watch_seconds=None,
            cumulative=True,
            socket=None,
            config_lookup=_make_lookup({"patio-ep40m": "192.168.1.78"}),
            credentials=CredentialBundle(),
            timeout=2.0,
            mode=OutputMode.JSONL,
        )
    msg = str(ei.value)
    assert "EP40M" in msg
    assert "lacks" in msg.lower() or "no" in msg.lower() or "not available" in msg.lower()


@pytest.mark.asyncio
async def test_energy_hs200_switch_exits_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    hs200_no_emeter: Any,
) -> None:
    """FR-23: a non-energy device exits 5 (no Energy module)."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs200_no_emeter

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError):
        await run_energy(
            target="hallway-switch",
            watch_seconds=None,
            cumulative=True,
            socket=None,
            config_lookup=_make_lookup({"hallway-switch": "192.168.1.30"}),
            credentials=CredentialBundle(),
            timeout=2.0,
            mode=OutputMode.JSONL,
        )


# --- watch / streaming --------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_watch_emits_two_jsonl_ticks(
    monkeypatch: pytest.MonkeyPatch,
    kp115_with_emeter: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-22: --watch emits one JSON line per tick. Use _max_ticks to bound."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return kp115_with_emeter

    async def _no_sleep(_seconds: float) -> None:  # avoid wall-clock waiting
        return None

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.asyncio.sleep", _no_sleep)

    code = await run_energy(
        target="kitchen-plug",
        watch_seconds=0.5,  # sub-second supported (FR-22 / Phase 1 lesson)
        cumulative=False,
        socket=None,
        config_lookup=_make_lookup({"kitchen-plug": "192.168.1.42"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
        _max_ticks=2,
    )
    assert code == EXIT_SUCCESS
    out_lines = [
        line for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    assert len(out_lines) >= 2
    for line in out_lines:
        parsed = json.loads(line)
        assert parsed["alias"] == "kitchen-plug"
        assert parsed["today_kwh"] is None  # --no-cumulative implied


@pytest.mark.asyncio
async def test_energy_watch_no_cumulative_default(
    monkeypatch: pytest.MonkeyPatch,
    kp115_with_emeter: Any,
) -> None:
    """FR-22 explicit: with cumulative=False the Reading omits both kWh fields."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return kp115_with_emeter

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.asyncio.sleep", _no_sleep)

    captured: list[Reading] = []
    real_emit_stream = __import__(
        "kasa_cli.verbs.energy_cmd", fromlist=["emit_stream"]
    ).emit_stream

    def _spy(items: Any, *args: Any, **kwargs: Any) -> Any:
        for r in items:
            captured.append(r)
        return real_emit_stream(items, *args, **kwargs)

    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.emit_stream", _spy)

    code = await run_energy(
        target="kitchen-plug",
        watch_seconds=1.0,
        cumulative=False,
        socket=None,
        config_lookup=_make_lookup({"kitchen-plug": "192.168.1.42"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
        _max_ticks=2,
    )
    assert code == EXIT_SUCCESS
    assert len(captured) >= 1
    for r in captured:
        assert r.today_kwh is None
        assert r.month_kwh is None


@pytest.mark.asyncio
async def test_energy_watch_with_cumulative_includes_kwh(
    monkeypatch: pytest.MonkeyPatch,
    kp115_with_emeter: Any,
) -> None:
    """FR-22: ``--cumulative`` (cumulative=True) DOES include today/month."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return kp115_with_emeter

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.asyncio.sleep", _no_sleep)

    captured: list[Reading] = []
    real_emit_stream = __import__(
        "kasa_cli.verbs.energy_cmd", fromlist=["emit_stream"]
    ).emit_stream

    def _spy(items: Any, *args: Any, **kwargs: Any) -> Any:
        for r in items:
            captured.append(r)
        return real_emit_stream(items, *args, **kwargs)

    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.emit_stream", _spy)

    code = await run_energy(
        target="kitchen-plug",
        watch_seconds=0.5,
        cumulative=True,
        socket=None,
        config_lookup=_make_lookup({"kitchen-plug": "192.168.1.42"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
        _max_ticks=2,
    )
    assert code == EXIT_SUCCESS
    assert len(captured) >= 1
    for r in captured:
        assert r.today_kwh is not None
        assert r.month_kwh is not None
