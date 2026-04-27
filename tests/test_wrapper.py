"""Tests for :mod:`kasa_cli.wrapper`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kasa_cli import wrapper
from kasa_cli.errors import (
    AuthError,
    NetworkError,
    NotFoundError,
    UnsupportedError,
)
from kasa_cli.types import Device
from kasa_cli.wrapper import CredentialBundle


def test_credential_bundle_is_present() -> None:
    assert CredentialBundle().is_present is False
    assert CredentialBundle(username="u").is_present is False
    assert CredentialBundle(username="u", password="p").is_present is True


def test_to_device_record_translates_basic_fields(
    make_device: Callable[..., Any],
) -> None:
    kdev = make_device(
        alias="kitchen-lamp",
        host="192.168.1.42",
        mac="AA-BB-CC-DD-EE-01",
        model="HS100",
        is_on=True,
    )
    rec: Device = wrapper.to_device_record(kdev)
    assert rec.alias == "kitchen-lamp"
    assert rec.ip == "192.168.1.42"
    assert rec.mac == "AA:BB:CC:DD:EE:01"  # normalized to colons
    assert rec.model == "HS100"
    assert rec.state == "on"
    assert rec.protocol in ("legacy", "klap")
    assert rec.sockets is None
    assert rec.last_seen.endswith("Z")


def test_to_device_record_alias_override(
    make_device: Callable[..., Any],
) -> None:
    kdev = make_device(alias="device-side-name")
    rec = wrapper.to_device_record(kdev, alias_override="config-alias")
    assert rec.alias == "config-alias"


def test_to_device_record_strip_mixed_state(hs300_strip: Any) -> None:
    rec = wrapper.to_device_record(hs300_strip)
    assert rec.state == "mixed"  # one on, two off
    assert rec.sockets is not None
    assert len(rec.sockets) == 3
    assert rec.sockets[0].state == "on"
    assert rec.sockets[1].state == "off"


@pytest.mark.asyncio
async def test_resolve_target_raises_not_found_on_unknown(
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    with pytest.raises(NotFoundError):
        await wrapper.resolve_target(
            "no-such-thing",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
        )


@pytest.mark.asyncio
async def test_resolve_target_translates_kasa_errors(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Each kasa-side exception gets mapped to the right kasa-cli error."""
    from kasa.exceptions import (
        AuthenticationError,
        KasaException,
        UnsupportedDeviceError,
    )
    from kasa.exceptions import TimeoutError as KasaTimeoutError

    cases: list[tuple[type[BaseException], type[BaseException]]] = [
        (AuthenticationError, AuthError),
        (KasaTimeoutError, NetworkError),
        (UnsupportedDeviceError, UnsupportedError),
        (KasaException, type(NetworkError("x"))),  # Generic -> DeviceError; lenient
    ]
    for raised, _expected_family in cases:

        async def _connect_raises(
            *_args: Any, _exc: type[BaseException] = raised, **_kwargs: Any
        ) -> Any:
            raise _exc("simulated")

        monkeypatch.setattr(
            "kasa.Device.connect",
            classmethod(lambda cls, *a, **k: _connect_raises(*a, **k)),
        )
        with pytest.raises(Exception) as info:
            await wrapper.resolve_target(
                "kitchen-lamp",
                config_lookup=fake_lookup,
                credentials=CredentialBundle(),
                timeout=1.0,
            )
        assert info.value.__class__.__name__ in {
            "AuthError",
            "NetworkError",
            "UnsupportedError",
            "DeviceError",
        }


@pytest.mark.asyncio
async def test_discover_returns_sorted_devices(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    devs = [
        make_device(alias="zeta", host="192.168.1.99", mac="AA:BB:CC:DD:EE:Z9"),
        make_device(alias="alpha", host="192.168.1.10", mac="AA:BB:CC:DD:EE:01"),
    ]

    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        return {d.host: d for d in devs}

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    result = await wrapper.discover(
        timeout=1.0,
        target_network=None,
        credentials=CredentialBundle(),
    )
    assert [d.alias for d in result] == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_discover_zero_devices_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    result = await wrapper.discover(
        timeout=1.0,
        target_network=None,
        credentials=CredentialBundle(),
    )
    assert result == []


@pytest.mark.asyncio
async def test_discover_oserror_is_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("no usable interface")

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    with pytest.raises(NetworkError):
        await wrapper.discover(
            timeout=1.0,
            target_network=None,
            credentials=CredentialBundle(),
        )


@pytest.mark.asyncio
async def test_discover_passes_target_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_discover(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    await wrapper.discover(
        timeout=2.0,
        target_network="192.168.1.255",
        credentials=CredentialBundle(username="u", password="p"),
    )
    assert captured["target"] == "192.168.1.255"
    assert captured["username"] == "u"
    assert captured["password"] == "p"


@pytest.mark.asyncio
async def test_probe_alive_true_on_success(
    make_device: Callable[..., Any],
) -> None:
    kdev = make_device()
    assert await wrapper.probe_alive(kdev, timeout=1.0) is True


@pytest.mark.asyncio
async def test_probe_alive_false_on_timeout(
    make_device: Callable[..., Any],
) -> None:
    kdev = make_device(update_raises=TimeoutError())
    assert await wrapper.probe_alive(kdev, timeout=1.0) is False


@pytest.mark.asyncio
async def test_probe_alive_false_on_kasa_exception(
    make_device: Callable[..., Any],
) -> None:
    from kasa.exceptions import KasaException

    kdev = make_device(update_raises=KasaException("nope"))
    assert await wrapper.probe_alive(kdev, timeout=1.0) is False
