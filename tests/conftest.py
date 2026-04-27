"""Shared test fixtures for kasa-cli Phase 1 Part B.

Provides a ``MockKasaDevice`` factory that mimics enough of ``kasa.Device``'s
public surface (alias, model, hw_info, sw_info, is_on, turn_on(), turn_off(),
update(), children, host, mac, features, config) for the verb modules and
wrapper translation layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest


class MockKasaDevice:
    """Lightweight stand-in for ``kasa.Device`` used by tests.

    Pass ``children=[...]`` to simulate a multi-socket strip; each child is
    itself a :class:`MockKasaDevice` (so child.turn_on() etc. work).
    """

    def __init__(
        self,
        *,
        alias: str = "mock",
        host: str = "192.168.1.10",
        mac: str = "AA:BB:CC:DD:EE:01",
        model: str = "HS100",
        hw_info: dict[str, Any] | None = None,
        sys_info: dict[str, Any] | None = None,
        features: dict[str, Any] | None = None,
        is_on: bool = False,
        children: Iterable[MockKasaDevice] | None = None,
        update_raises: BaseException | None = None,
        turn_on_raises: BaseException | None = None,
        turn_off_raises: BaseException | None = None,
    ) -> None:
        self.alias = alias
        self.host = host
        self.mac = mac
        self.model = model
        self.hw_info = hw_info or {"hw_ver": "1.0", "sw_ver": "1.5.6"}
        self.sys_info = sys_info or {}
        self.features = features or {"on": True}
        self.is_on = is_on
        self.children: list[MockKasaDevice] = list(children) if children else []
        self.config = None  # protocol detection falls through to "legacy"
        self._update_raises = update_raises
        self._turn_on_raises = turn_on_raises
        self._turn_off_raises = turn_off_raises
        self.turn_on_called = 0
        self.turn_off_called = 0
        self.update_called = 0
        self.disconnect_called = 0

    @property
    def is_off(self) -> bool:  # convenience for parity with kasa.Device
        return not self.is_on

    async def update(self) -> None:
        self.update_called += 1
        if self._update_raises is not None:
            raise self._update_raises

    async def turn_on(self) -> None:
        self.turn_on_called += 1
        if self._turn_on_raises is not None:
            raise self._turn_on_raises
        self.is_on = True

    async def turn_off(self) -> None:
        self.turn_off_called += 1
        if self._turn_off_raises is not None:
            raise self._turn_off_raises
        self.is_on = False

    async def disconnect(self) -> None:
        self.disconnect_called += 1


@pytest.fixture
def make_device() -> Callable[..., MockKasaDevice]:
    """Factory for :class:`MockKasaDevice` instances."""

    def _factory(**kwargs: Any) -> MockKasaDevice:
        return MockKasaDevice(**kwargs)

    return _factory


@pytest.fixture
def hs300_strip(make_device: Callable[..., MockKasaDevice]) -> MockKasaDevice:
    """A 3-socket HS300 strip; one socket on, two off."""
    sockets = [
        make_device(alias="socket-a", model="HS300", is_on=True),
        make_device(alias="socket-b", model="HS300", is_on=False),
        make_device(alias="socket-c", model="HS300", is_on=False),
    ]
    return make_device(
        alias="office-strip",
        host="192.168.1.51",
        mac="AA:BB:CC:DD:EE:02",
        model="HS300(US)",
        children=sockets,
    )


@pytest.fixture
def fake_config() -> dict[str, Any]:
    """A minimal config-like dict consumed by ``cli._make_config_lookup``."""
    return {
        "kitchen-lamp": {
            "ip": "192.168.1.42",
            "mac": "AA:BB:CC:DD:EE:03",
        },
        "office-strip": {
            "ip": "192.168.1.51",
            "mac": "AA:BB:CC:DD:EE:02",
        },
    }


@pytest.fixture
def fake_lookup(
    fake_config: dict[str, Any],
) -> Callable[[str], tuple[str | None, str | None]]:
    """A ``config_lookup`` callable that resolves alias → (ip, alias)."""

    def _lookup(target: str) -> tuple[str | None, str | None]:
        if target in fake_config:
            return fake_config[target]["ip"], target
        if target.count(".") == 3:
            return target, None  # IP literal
        raise KeyError(target)

    return _lookup
