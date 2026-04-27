"""Tests for verb implementations under :mod:`kasa_cli.verbs`."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from kasa_cli.errors import (
    EXIT_SUCCESS,
    AuthError,
    DeviceError,
    NetworkError,
    NotFoundError,
    UnsupportedFeatureError,
    UsageError,
)
from kasa_cli.output import OutputMode
from kasa_cli.verbs.discover_cmd import run_discover
from kasa_cli.verbs.info_cmd import run_info
from kasa_cli.verbs.list_cmd import run_list
from kasa_cli.verbs.onoff import run_onoff
from kasa_cli.wrapper import CredentialBundle

# --- discover -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_emits_devices_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    devs = [
        make_device(alias="alpha", host="192.168.1.10", mac="AA:BB:CC:DD:EE:01"),
    ]

    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        return {d.host: d for d in devs}

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    code = await run_discover(
        timeout=1.0,
        target_network=None,
        credentials=CredentialBundle(),
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    captured = capsys.readouterr()
    line = captured.out.strip()
    assert line  # non-empty
    parsed = json.loads(line)
    assert parsed["alias"] == "alpha"


@pytest.mark.asyncio
async def test_discover_zero_devices_exits_zero_with_info(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    code = await run_discover(
        timeout=1.0,
        target_network=None,
        credentials=CredentialBundle(),
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    err = capsys.readouterr()
    assert "0 devices found" in err.err


@pytest.mark.asyncio
async def test_discover_network_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("no usable interface")

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    with pytest.raises(NetworkError):
        await run_discover(
            timeout=1.0,
            target_network=None,
            credentials=CredentialBundle(),
            mode=OutputMode.JSONL,
        )


# --- list ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_no_probe_emits_null_online(
    capsys: pytest.CaptureFixture[str],
) -> None:
    devices = [
        {"alias": "a", "ip": "1.1.1.1", "mac": "MAC1"},
        {"alias": "b", "ip": "1.1.1.2", "mac": "MAC2"},
    ]
    code = await run_list(
        devices_section=devices,
        probe=False,
        online_only=False,
        credentials=CredentialBundle(),
        timeout=1.0,
        concurrency=2,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert len(lines) == 2
    assert all(item["online"] is None for item in lines)


@pytest.mark.asyncio
async def test_list_probe_invokes_per_device_check(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Probe path: monkeypatch Device.connect to return a live mock."""

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        return make_device(host=host, alias=host)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    devices = [{"alias": "a", "ip": "1.1.1.1", "mac": "MAC1"}]
    code = await run_list(
        devices_section=devices,
        probe=True,
        online_only=False,
        credentials=CredentialBundle(),
        timeout=1.0,
        concurrency=2,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["online"] is True


@pytest.mark.asyncio
async def test_list_online_only_filters_offline(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All connect attempts fail -> online_only filter yields empty list."""

    async def _fake_connect_fails(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("nope")

    monkeypatch.setattr("kasa.Device.connect", _fake_connect_fails)

    devices = [{"alias": "a", "ip": "1.1.1.1", "mac": "MAC1"}]
    code = await run_list(
        devices_section=devices,
        probe=False,
        online_only=True,
        credentials=CredentialBundle(),
        timeout=1.0,
        concurrency=2,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert capsys.readouterr().out.strip() == ""


# --- info ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_info_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return make_device(alias="kitchen-lamp", model="HS100", is_on=True)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_info(
        target="kitchen-lamp",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["alias"] == "kitchen-lamp"
    assert parsed["state"] == "on"


@pytest.mark.asyncio
async def test_info_unknown_target_raises_not_found(
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    with pytest.raises(NotFoundError):
        await run_info(
            target="nope",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


@pytest.mark.asyncio
async def test_info_update_failure_raises_device_error(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    from kasa.exceptions import KasaException

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return make_device(update_raises=KasaException("boom"))

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    with pytest.raises(DeviceError):
        await run_info(
            target="kitchen-lamp",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


# --- on / off -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_idempotent_already_on(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """FR-14: on against an already-on device is success and a no-op."""
    dev = make_device(alias="kitchen-lamp", model="HS100", is_on=True)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_onoff(
        action="on",
        target="kitchen-lamp",
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert dev.turn_on_called == 0  # idempotent: never called


@pytest.mark.asyncio
async def test_off_flips_state(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    dev = make_device(alias="kitchen-lamp", model="HS100", is_on=True)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_onoff(
        action="off",
        target="kitchen-lamp",
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert dev.turn_off_called == 1


@pytest.mark.asyncio
async def test_multi_socket_strip_requires_socket(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """FR-15: multi-socket strip without --socket exits 64 with sockets list."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_onoff(
            action="off",
            target="office-strip",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == 64
    # message lists the available sockets
    assert "1=" in info.value.message and "2=" in info.value.message


@pytest.mark.asyncio
async def test_multi_socket_strip_socket_all(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """`--socket all` flips every child socket."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_onoff(
        action="on",
        target="office-strip",
        socket_arg="all",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    # Two sockets were off; both should have been turned on. The third was
    # already on (idempotent).
    on_calls = sum(child.turn_on_called for child in hs300_strip.children)
    assert on_calls == 2


@pytest.mark.asyncio
async def test_multi_socket_strip_specific_socket(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_onoff(
        action="on",
        target="office-strip",
        socket_arg="2",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert hs300_strip.children[1].turn_on_called == 1
    assert hs300_strip.children[0].turn_on_called == 0


@pytest.mark.asyncio
async def test_multi_socket_strip_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_onoff(
            action="on",
            target="office-strip",
            socket_arg="99",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == 64


@pytest.mark.asyncio
async def test_multi_socket_strip_bad_socket_value(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError):
        await run_onoff(
            action="on",
            target="office-strip",
            socket_arg="not-a-number",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


@pytest.mark.asyncio
async def test_single_socket_rejects_socket_n_other_than_1(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return make_device(model="HS100", is_on=False)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError):
        await run_onoff(
            action="on",
            target="kitchen-lamp",
            socket_arg="3",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


@pytest.mark.asyncio
async def test_unknown_target_returns_not_found(
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    with pytest.raises(NotFoundError):
        await run_onoff(
            action="on",
            target="ghost",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


@pytest.mark.asyncio
async def test_auth_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    from kasa.exceptions import AuthenticationError

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        raise AuthenticationError("rejected")

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(AuthError):
        await run_onoff(
            action="on",
            target="kitchen-lamp",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(username="u", password="p"),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


@pytest.mark.asyncio
async def test_unsupported_device_propagates(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    from kasa.exceptions import UnsupportedDeviceError

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        raise UnsupportedDeviceError("model not in matrix")

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError):
        await run_onoff(
            action="on",
            target="kitchen-lamp",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
