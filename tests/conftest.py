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


# ---------------------------------------------------------------------------
# Phase 2 Engineer B additive fixtures (Energy / Schedule mocks)
# ---------------------------------------------------------------------------


class MockEnergyModule:
    """Lightweight stand-in for ``kasa.interfaces.Energy``.

    Exposes the same property names as the real interface so the wrapper's
    ``getattr``-based readout works without any code changes when wired up to
    a :class:`MockKasaDevice`'s ``modules`` mapping.
    """

    def __init__(
        self,
        *,
        current_consumption: float | None = 12.5,
        voltage: float | None = 120.1,
        current: float | None = 0.105,
        consumption_today: float | None = 0.250,
        consumption_this_month: float | None = 7.500,
    ) -> None:
        self.current_consumption = current_consumption
        self.voltage = voltage
        self.current = current
        self.consumption_today = consumption_today
        self.consumption_this_month = consumption_this_month


class MockScheduleRule:
    """Lightweight stand-in for ``kasa.iot.modules.rulemodule.Rule``.

    The wrapper consumes attributes via ``getattr`` so a duck-typed dataclass
    is sufficient. The shape mirrors python-kasa 0.10.2's ``Rule`` (id, name,
    enable, wday, repeat, sact, smin, ...).
    """

    def __init__(
        self,
        *,
        rule_id: str = "rule-1",
        name: str = "Evening lights",
        enable: int = 1,
        wday: list[int] | None = None,
        repeat: int = 1,
        sact: int = 1,  # TurnOn
        smin: int = 22 * 60,  # 22:00
    ) -> None:
        self.id = rule_id
        self.name = name
        self.enable = enable
        self.wday = wday or [1, 1, 1, 1, 1, 0, 0]  # weekdays Mon..Fri
        self.repeat = repeat
        self.sact = _MockAction(sact)
        self.smin = smin
        # End-time fields python-kasa Rule carries but we don't surface.
        self.eact = None
        self.etime_opt = None
        self.emin = None


class _MockAction:
    """Mimics ``kasa.iot.modules.rulemodule.Action`` (an Enum)."""

    def __init__(self, value: int) -> None:
        self._value = value
        self.name = {-1: "Disabled", 0: "TurnOff", 1: "TurnOn", 2: "Unknown"}.get(value, "Unknown")

    def __int__(self) -> int:
        return self._value


class MockScheduleModule:
    """Lightweight stand-in for ``kasa.iot.modules.schedule.Schedule``."""

    def __init__(self, rules: list[MockScheduleRule] | None = None) -> None:
        self.rules: list[MockScheduleRule] = list(rules) if rules else []


class MockModulesMapping:
    """Dict-like mapping with ``.get(name)`` lookup mimicking ModuleMapping.

    python-kasa's actual ``Module.Energy`` and ``Module.IotSchedule`` are
    ``ModuleName`` instances which compare-as-string under ``str()``. We let
    the test author key the mapping directly with the ``Module.*`` constant
    they pass in so calls like ``modules.get(Module.Energy)`` round-trip.
    """

    def __init__(self, payload: dict[Any, Any] | None = None) -> None:
        self._payload: dict[Any, Any] = dict(payload) if payload else {}

    def get(self, key: Any, default: Any = None) -> Any:
        return self._payload.get(key, default)


@pytest.fixture
def make_energy_module() -> Callable[..., MockEnergyModule]:
    """Factory for :class:`MockEnergyModule`."""

    def _factory(**kwargs: Any) -> MockEnergyModule:
        return MockEnergyModule(**kwargs)

    return _factory


@pytest.fixture
def make_schedule_rule() -> Callable[..., MockScheduleRule]:
    """Factory for :class:`MockScheduleRule`."""

    def _factory(**kwargs: Any) -> MockScheduleRule:
        return MockScheduleRule(**kwargs)

    return _factory


@pytest.fixture
def hs300_with_emeters(
    make_device: Callable[..., MockKasaDevice],
    make_energy_module: Callable[..., MockEnergyModule],
) -> MockKasaDevice:
    """HS300 with per-socket Energy modules but NO parent Energy module."""
    from kasa.module import Module as KasaModule

    children: list[MockKasaDevice] = []
    for alias, watts in [("socket-a", 12.5), ("socket-b", 5.0), ("socket-c", 0.0)]:
        c = make_device(alias=alias, model="HS300", is_on=watts > 0.0)
        c.modules = MockModulesMapping(  # type: ignore[attr-defined]
            {KasaModule.Energy: make_energy_module(current_consumption=watts)}
        )
        children.append(c)
    parent = make_device(
        alias="office-strip",
        host="192.168.1.51",
        mac="AA:BB:CC:DD:EE:02",
        model="HS300(US)",
        children=children,
    )
    # No parent Energy module — exercises the sum-the-children fallback.
    parent.modules = MockModulesMapping({})  # type: ignore[attr-defined]
    return parent


@pytest.fixture
def kp115_with_emeter(
    make_device: Callable[..., MockKasaDevice],
    make_energy_module: Callable[..., MockEnergyModule],
) -> MockKasaDevice:
    """KP115 single-socket plug with a parent Energy module."""
    from kasa.module import Module as KasaModule

    plug = make_device(
        alias="kitchen-plug",
        host="192.168.1.42",
        mac="AA:BB:CC:DD:EE:03",
        model="KP115(US)",
        is_on=True,
    )
    plug.modules = MockModulesMapping(  # type: ignore[attr-defined]
        {KasaModule.Energy: make_energy_module()}
    )
    return plug


@pytest.fixture
def hs200_no_emeter(
    make_device: Callable[..., MockKasaDevice],
) -> MockKasaDevice:
    """HS200 wall switch — no Energy module."""
    sw = make_device(
        alias="hallway-switch",
        host="192.168.1.30",
        mac="AA:BB:CC:DD:EE:04",
        model="HS200(US)",
        is_on=True,
    )
    sw.modules = MockModulesMapping({})  # type: ignore[attr-defined]
    return sw


@pytest.fixture
def ep40m_no_emeter(
    make_device: Callable[..., MockKasaDevice],
) -> MockKasaDevice:
    """EP40M outdoor strip — supported as a device but lacks emeter (SRD §3.1)."""
    from kasa.module import Module as KasaModule

    children = [
        make_device(alias="ep40m-1", model="EP40M", is_on=False),
        make_device(alias="ep40m-2", model="EP40M", is_on=False),
    ]
    strip = make_device(
        alias="patio-ep40m",
        host="192.168.1.78",
        mac="AA:BB:CC:DD:EE:05",
        model="EP40M(US)",
        children=children,
    )
    strip.modules = MockModulesMapping({KasaModule.Energy: None})  # type: ignore[attr-defined]
    return strip


@pytest.fixture
def iot_plug_with_schedule(
    make_device: Callable[..., MockKasaDevice],
    make_schedule_rule: Callable[..., MockScheduleRule],
) -> MockKasaDevice:
    """A legacy IOT plug exposing two schedule rules."""
    from kasa.module import Module as KasaModule

    rules = [
        make_schedule_rule(rule_id="rule-1", name="Lights on", enable=1, sact=1, smin=22 * 60),
        make_schedule_rule(
            rule_id="rule-2",
            name="Lights off",
            enable=0,
            sact=0,
            smin=6 * 60 + 30,
            wday=[1, 0, 1, 0, 1, 0, 0],
        ),
    ]
    plug = make_device(
        alias="iot-plug",
        host="192.168.1.50",
        mac="AA:BB:CC:DD:EE:06",
        model="HS103",
        is_on=True,
    )
    plug.modules = MockModulesMapping(  # type: ignore[attr-defined]
        {KasaModule.IotSchedule: MockScheduleModule(rules=rules)}
    )
    return plug


@pytest.fixture
def klap_plug_no_schedule(
    make_device: Callable[..., MockKasaDevice],
) -> MockKasaDevice:
    """A KLAP/Smart plug — no IotSchedule module."""
    plug = make_device(
        alias="klap-plug",
        host="192.168.1.78",
        mac="AA:BB:CC:DD:EE:07",
        model="EP25(US)",
        is_on=True,
    )
    plug.modules = MockModulesMapping({})  # type: ignore[attr-defined]
    return plug
