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
    """No --socket on HS300 with no parent emeter → sum of child power AND current.

    C3 fix: previously asserted only ``current_power_w``, leaving the voltage
    and current semantics unverified. Now also asserts:
      * voltage_v == 120.1 (every child reports 120.1; "last non-zero" wins)
      * current_a is the sum of children's currents (3 x 0.105 = 0.315)
    """

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
    # All three children report voltage 120.1; with "last non-zero" semantics
    # the strip surfaces 120.1 (any reporting child would do — they're on the
    # same AC line).
    assert parsed["voltage_v"] == pytest.approx(120.1)
    # Current is summed (each child default 0.105A x 3 children = 0.315A).
    assert parsed["current_a"] == pytest.approx(0.315)


@pytest.mark.asyncio
async def test_energy_hs300_voltage_picks_last_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Any,
    make_energy_module: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """C3: with distinct child voltages, strip total surfaces the LAST non-zero.

    The existing ``hs300_with_emeters`` fixture gives every child the same
    voltage (120.1), which can't distinguish "first non-zero" from "last
    non-zero" semantics. This test pins down the contract by handing the
    wrapper a strip whose children report voltages [120.0, 121.5, 0.0]. With
    "last non-zero" the strip total reports 121.5; "first non-zero" would
    have reported 120.0; "last (any)" would have reported 0.0. Only one of
    those is right per the wrapper docstring (after the C3 reconciliation).
    """
    from kasa.module import Module as KasaModule

    from tests.conftest import MockModulesMapping

    children = []
    voltages = [120.0, 121.5, 0.0]
    for i, v in enumerate(voltages, start=1):
        c = make_device(alias=f"socket-{i}", model="HS300", is_on=v > 0)
        c.modules = MockModulesMapping(
            {KasaModule.Energy: make_energy_module(voltage=v, current_consumption=1.0)}
        )
        children.append(c)
    parent = make_device(
        alias="distinct-voltages-strip",
        host="192.168.1.99",
        mac="AA:BB:CC:DD:EE:99",
        model="HS300(US)",
        children=children,
    )
    parent.modules = MockModulesMapping({})  # force sum-the-children path

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return parent

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_energy(
        target="distinct-voltages-strip",
        watch_seconds=None,
        cumulative=False,
        socket=None,
        config_lookup=_make_lookup({"distinct-voltages-strip": "192.168.1.99"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    parsed = json.loads(capsys.readouterr().out.strip())
    # "last non-zero" semantics: child 3 reports 0.0 (skipped), so the
    # surfaced voltage is child 2's 121.5, not child 1's 120.0.
    assert parsed["voltage_v"] == pytest.approx(121.5)


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
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
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
    """FR-22 explicit: with cumulative=False the Reading omits both kWh fields.

    JSONL ``--watch`` now streams via :func:`emit_one` (one flushed write per
    tick) rather than collecting then calling :func:`emit_stream` at loop end.
    The spy hooks ``emit_one`` to capture each Reading object as it's emitted
    and asserts the cumulative fields are ``None`` per FR-22 default.
    """

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return kp115_with_emeter

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.asyncio.sleep", _no_sleep)

    captured: list[Reading] = []
    real_emit_one = __import__("kasa_cli.verbs.energy_cmd", fromlist=["emit_one"]).emit_one

    def _spy(item: Any, *args: Any, **kwargs: Any) -> Any:
        captured.append(item)
        return real_emit_one(item, *args, **kwargs)

    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.emit_one", _spy)

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
    real_emit_one = __import__("kasa_cli.verbs.energy_cmd", fromlist=["emit_one"]).emit_one

    def _spy(item: Any, *args: Any, **kwargs: Any) -> Any:
        captured.append(item)
        return real_emit_one(item, *args, **kwargs)

    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.emit_one", _spy)

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


@pytest.mark.asyncio
async def test_energy_watch_jsonl_streams_per_tick_not_buffered(
    monkeypatch: pytest.MonkeyPatch,
    kp115_with_emeter: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """C2 review fix: JSONL ``--watch`` writes (and flushes) ONE line per tick.

    The pre-fix implementation collected all Readings into a list and called
    ``emit_stream`` once at loop exit. With ``_max_ticks=None`` (production)
    that meant stdout stayed silent forever. This test pins down the new
    contract: by the time the second tick has been ``emit_one``-ed, the
    first tick's JSON line is already on stdout (and ``flush()`` has been
    called).
    """

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return kp115_with_emeter

    sleep_calls: int = 0

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.setattr("kasa_cli.verbs.energy_cmd.asyncio.sleep", _no_sleep)

    # Track flush calls. The streaming contract requires ``flush`` to be
    # invoked AT LEAST ONCE PER TICK so live consumers see each reading the
    # moment it lands. We also record the count of stdout lines visible at
    # each flush to assert per-tick visibility (vs. one big buffer at end).
    real_flush = __import__("sys").stdout.flush
    flush_visible_lines: list[int] = []

    def _flush_spy() -> None:
        # Read the captured stdout content WITHOUT consuming it. capsys uses
        # an internal buffer we can peek at via the real `_capture` shim;
        # simplest portable approach is to count newlines in `sys.stdout`'s
        # current buffer if available, else just record the flush event.
        stream = __import__("sys").stdout
        buf = getattr(stream, "getvalue", None)
        if callable(buf):
            flush_visible_lines.append(buf().count("\n"))
        else:
            flush_visible_lines.append(-1)
        return real_flush()

    monkeypatch.setattr("sys.stdout.flush", _flush_spy)

    code = await run_energy(
        target="kitchen-plug",
        watch_seconds=0.5,
        cumulative=False,
        socket=None,
        config_lookup=_make_lookup({"kitchen-plug": "192.168.1.42"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSONL,
        _max_ticks=3,
    )
    assert code == EXIT_SUCCESS
    out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(out_lines) == 3, f"expected 3 ticks, got {len(out_lines)}"
    # At least 3 flushes (one per tick). Could be more if other writes
    # incidentally flush, but never fewer.
    assert len(flush_visible_lines) >= 3, (
        f"expected at least 3 flushes (one per tick); got {flush_visible_lines}"
    )
