"""Shared test fixtures for kasa-cli (Phase 1 + Phase 2).

Provides a ``MockKasaDevice`` factory that mimics enough of ``kasa.Device``'s
public surface (alias, model, hw_info, sw_info, is_on, turn_on(), turn_off(),
update(), children, host, mac, features, config, modules+Light) for the verb
modules and wrapper translation layer.

Phase 2 additions: ``modules`` mapping carrying capability sentinels, plus a
``MockLightModule`` exposing ``set_brightness`` / ``set_hsv`` / ``set_color_temp``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest


class MockLightModule:
    """Stand-in for python-kasa's :class:`kasa.interfaces.Light` module.

    Tracks call counts and the last value passed for each setter so tests can
    assert on what was sent. ``temp_min`` / ``temp_max`` define the
    color-temp clamp range; ``set_color_temp_raises`` lets a test inject a
    failure path.
    """

    def __init__(
        self,
        *,
        temp_min: int = 2500,
        temp_max: int = 6500,
        set_brightness_raises: BaseException | None = None,
        set_hsv_raises: BaseException | None = None,
        set_color_temp_raises: BaseException | None = None,
    ) -> None:
        self.temp_min = temp_min
        self.temp_max = temp_max
        self.brightness_calls: list[int] = []
        self.hsv_calls: list[tuple[int, int, int]] = []
        self.color_temp_calls: list[int] = []
        self._set_brightness_raises = set_brightness_raises
        self._set_hsv_raises = set_hsv_raises
        self._set_color_temp_raises = set_color_temp_raises

    async def set_brightness(self, brightness: int, *, transition: int | None = None) -> None:
        if self._set_brightness_raises is not None:
            raise self._set_brightness_raises
        self.brightness_calls.append(brightness)
        del transition

    async def set_hsv(
        self,
        hue: int,
        saturation: int,
        value: int | None = None,
        *,
        transition: int | None = None,
    ) -> None:
        if self._set_hsv_raises is not None:
            raise self._set_hsv_raises
        self.hsv_calls.append((hue, saturation, value if value is not None else 100))
        del transition

    async def set_color_temp(
        self,
        temp: int,
        *,
        brightness: int | None = None,
        transition: int | None = None,
    ) -> None:
        if self._set_color_temp_raises is not None:
            raise self._set_color_temp_raises
        self.color_temp_calls.append(temp)
        del brightness, transition

    def get_feature(self, name: str) -> object | None:
        if name == "color_temp":
            return _MockFeature(self.temp_min, self.temp_max)
        return None


class _MockFeature:
    """Minimal stand-in for kasa.feature.Feature (only the bounds are read)."""

    def __init__(self, minimum_value: int, maximum_value: int) -> None:
        self.minimum_value = minimum_value
        self.maximum_value = maximum_value


class _MockModuleMapping:
    """Mapping that mimics python-kasa's ``ModuleMapping`` for tests.

    Keyed by capability name string ("Brightness", "Color", "ColorTemperature",
    "Light"). Look-ups via ``mapping.get(Module.X)`` go through ``__contains__``
    / ``__getitem__`` since ``kasa.Module.X`` evaluates to a string-like name
    that compares equal to the entry keys.
    """

    def __init__(self, entries: dict[str, object]) -> None:
        self._entries = dict(entries)

    def get(self, key: object, default: object | None = None) -> object | None:
        # Convert kasa.Module.X (an enum) to its name string for our mapping.
        name = getattr(key, "name", None) or str(key)
        return self._entries.get(name, default)

    def __contains__(self, key: object) -> bool:
        name = getattr(key, "name", None) or str(key)
        return name in self._entries

    def __getitem__(self, key: object) -> object:
        name = getattr(key, "name", None) or str(key)
        return self._entries[name]


class MockKasaDevice:
    """Lightweight stand-in for ``kasa.Device`` used by tests.

    Pass ``children=[...]`` to simulate a multi-socket strip; each child is
    itself a :class:`MockKasaDevice` (so child.turn_on() etc. work). Pass
    ``light_module=MockLightModule(...)`` and ``capabilities=("Brightness",
    "Color", "ColorTemperature")`` to make the device look color-capable to
    the wrapper's ``set_*`` helpers.
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
        light_module: MockLightModule | None = None,
        capabilities: Iterable[str] | None = None,
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
        # Phase 2 capability surface.
        entries: dict[str, object] = {}
        for name in capabilities or ():
            entries[name] = True
        if light_module is not None:
            entries["Light"] = light_module
        self.light_module = light_module
        self.modules = _MockModuleMapping(entries)

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
