"""Tests for the ``schedule list`` verb (Phase 2 Engineer B).

Covers FR-24 (legacy IOT happy path emits a JSON array of rule dicts) and
FR-24a (KLAP/Smart device exits 5 with the SRD-mandated message).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from kasa_cli.errors import EXIT_SUCCESS, UnsupportedFeatureError
from kasa_cli.output import OutputMode
from kasa_cli.verbs.schedule_cmd import run_schedule_list
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


@pytest.mark.asyncio
async def test_schedule_list_iot_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    iot_plug_with_schedule: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-24: legacy IOT plug emits a JSON array with the two configured rules."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return iot_plug_with_schedule

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_schedule_list(
        target="iot-plug",
        config_lookup=_make_lookup({"iot-plug": "192.168.1.50"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSON,
    )
    assert code == EXIT_SUCCESS
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    rule_one = parsed[0]
    assert rule_one["id"] == "rule-1"
    assert rule_one["enabled"] is True
    assert rule_one["action"] == "on"
    # weekly+wday rendered as a flat string
    assert "22:00" in rule_one["time_spec"]


@pytest.mark.asyncio
async def test_schedule_list_klap_exits_unsupported_with_srd_message(
    monkeypatch: pytest.MonkeyPatch,
    klap_plug_no_schedule: Any,
) -> None:
    """FR-24a: KLAP/Smart raises UnsupportedFeatureError carrying the exact message."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return klap_plug_no_schedule

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError) as ei:
        await run_schedule_list(
            target="klap-plug",
            config_lookup=_make_lookup({"klap-plug": "192.168.1.78"}),
            credentials=CredentialBundle(),
            timeout=2.0,
            mode=OutputMode.JSON,
        )
    # R1: assert the FULL SRD-mandated message (not just substrings). If the
    # wrapper text drifts even by one character — punctuation, capitalization,
    # path — this test catches it. The reviewer flagged loose substring
    # matching as letting wording rot in unnoticed.
    expected = (
        "python-kasa 0.10.2 does not expose schedule listing for "
        "KLAP/Smart-protocol devices; revisit when upstream adds a "
        "Schedule module to kasa/smart/modules/."
    )
    assert expected in str(ei.value), f"got: {ei.value!r}"


@pytest.mark.asyncio
async def test_schedule_list_empty_rule_list_emits_array(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty rules → ``[]`` on stdout, exit 0."""
    from kasa.module import Module as KasaModule

    from tests.conftest import MockModulesMapping, MockScheduleModule

    plug = make_device(
        alias="iot-plug-empty",
        host="192.168.1.51",
        mac="AA:BB:CC:DD:EE:08",
        model="HS103",
        is_on=True,
    )
    plug.modules = MockModulesMapping({KasaModule.IotSchedule: MockScheduleModule(rules=[])})

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return plug

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_schedule_list(
        target="iot-plug-empty",
        config_lookup=_make_lookup({"iot-plug-empty": "192.168.1.51"}),
        credentials=CredentialBundle(),
        timeout=2.0,
        mode=OutputMode.JSON,
    )
    assert code == EXIT_SUCCESS
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == []
