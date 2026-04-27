"""Tests for ``kasa-cli set`` (FR-16..20)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kasa_cli.errors import (
    EXIT_SUCCESS,
    EXIT_UNSUPPORTED,
    EXIT_USAGE_ERROR,
    UnsupportedFeatureError,
    UsageError,
)
from kasa_cli.output import OutputMode
from kasa_cli.verbs.set_cmd import (
    parse_hex_color,
    parse_hsv_triple,
    run_set,
)
from kasa_cli.wrapper import CredentialBundle
from tests.conftest import MockKasaDevice, MockLightModule

# ---------------------------------------------------------------------------
# Pure parser tests
# ---------------------------------------------------------------------------


def test_parse_hsv_triple_happy() -> None:
    assert parse_hsv_triple("240,100,100") == (240, 100, 100)
    assert parse_hsv_triple("0,0,0") == (0, 0, 0)
    assert parse_hsv_triple("359,50,75") == (359, 50, 75)


def test_parse_hsv_triple_strips_whitespace() -> None:
    assert parse_hsv_triple("  240 , 100 , 100 ") == (240, 100, 100)


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "240,100",  # too few parts
        "240,100,100,5",  # too many parts
        "abc,100,100",  # non-int
        "360,100,100",  # hue out of range
        "240,101,100",  # sat out of range
        "240,100,101",  # value out of range
        "-1,50,50",  # negative hue
        "240,-1,50",  # negative sat
        "240,50,-1",  # negative value
    ],
)
def test_parse_hsv_triple_rejects_bad_input(value: str) -> None:
    with pytest.raises(UsageError) as info:
        parse_hsv_triple(value)
    assert info.value.exit_code == EXIT_USAGE_ERROR


def test_parse_hex_color_full_form() -> None:
    """Pure red, green, blue convert correctly."""
    assert parse_hex_color("#ff0000") == (0, 100, 100)
    assert parse_hex_color("#00ff00") == (120, 100, 100)
    assert parse_hex_color("#0000ff") == (240, 100, 100)


def test_parse_hex_color_no_leading_hash() -> None:
    assert parse_hex_color("ffffff") == (0, 0, 100)


def test_parse_hex_color_shorthand() -> None:
    """Three-digit shorthand expands to full form."""
    assert parse_hex_color("#0f0") == (120, 100, 100)
    assert parse_hex_color("0f0") == (120, 100, 100)
    assert parse_hex_color("#fff") == (0, 0, 100)


def test_parse_hex_color_black_is_zero_value() -> None:
    _h, _s, v = parse_hex_color("#000000")
    assert v == 0


@pytest.mark.parametrize(
    "value",
    [
        "",
        "#",
        "ggggg",  # bad chars
        "#12345",  # 5 digits
        "#1234567",  # 7 digits
        "#abxxyz",  # bad hex chars
    ],
)
def test_parse_hex_color_rejects_bad_input(value: str) -> None:
    with pytest.raises(UsageError) as info:
        parse_hex_color(value)
    assert info.value.exit_code == EXIT_USAGE_ERROR


# ---------------------------------------------------------------------------
# Verb integration tests — color/dimmable bulb
# ---------------------------------------------------------------------------


def _make_color_bulb(alias: str = "kitchen-lamp", model: str = "KL130") -> MockKasaDevice:
    """A device that advertises Brightness, Color, and ColorTemperature."""
    light = MockLightModule(temp_min=2500, temp_max=6500)
    return MockKasaDevice(
        alias=alias,
        model=model,
        is_on=True,
        light_module=light,
        capabilities=("Brightness", "Color", "ColorTemperature"),
    )


def _make_dimmable_only(alias: str = "dimmer", model: str = "HS220") -> MockKasaDevice:
    """A dimmer that supports brightness but NOT color or color-temp."""
    light = MockLightModule()
    return MockKasaDevice(
        alias=alias,
        model=model,
        is_on=True,
        light_module=light,
        capabilities=("Brightness",),
    )


def _make_dumb_plug(alias: str = "plug", model: str = "HS100") -> MockKasaDevice:
    """A plug with no light capabilities at all."""
    return MockKasaDevice(
        alias=alias,
        model=model,
        is_on=True,
        light_module=None,
        capabilities=(),
    )


@pytest.mark.asyncio
async def test_set_brightness_happy(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_set(
        target="kitchen-lamp",
        brightness=50,
        color_temp=None,
        hsv=None,
        hex_color=None,
        color_name=None,
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert bulb.light_module is not None
    assert bulb.light_module.brightness_calls == [50]


@pytest.mark.asyncio
async def test_set_color_temp_happy(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_set(
        target="kitchen-lamp",
        brightness=None,
        color_temp=2700,
        hsv=None,
        hex_color=None,
        color_name=None,
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert bulb.light_module is not None
    assert bulb.light_module.color_temp_calls == [2700]


@pytest.mark.asyncio
async def test_set_color_temp_clamps_below_minimum(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Below-min kelvin values are clamped to the device-reported minimum."""
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    await run_set(
        target="kitchen-lamp",
        brightness=None,
        color_temp=1000,  # below the mock's 2500 min
        hsv=None,
        hex_color=None,
        color_name=None,
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert bulb.light_module is not None
    assert bulb.light_module.color_temp_calls == [2500]


@pytest.mark.asyncio
async def test_set_hsv_happy(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_set(
        target="kitchen-lamp",
        brightness=None,
        color_temp=None,
        hsv="240,100,100",
        hex_color=None,
        color_name=None,
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert bulb.light_module is not None
    assert bulb.light_module.hsv_calls == [(240, 100, 100)]


@pytest.mark.asyncio
async def test_set_hex_routes_to_hsv(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``--hex #00ff00`` parses to (120, 100, 100)."""
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_set(
        target="kitchen-lamp",
        brightness=None,
        color_temp=None,
        hsv=None,
        hex_color="#00ff00",
        color_name=None,
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert bulb.light_module is not None
    assert bulb.light_module.hsv_calls == [(120, 100, 100)]


@pytest.mark.asyncio
async def test_set_color_name_blue(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``--color blue`` resolves to (240, 100, 100)."""
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_set(
        target="kitchen-lamp",
        brightness=None,
        color_temp=None,
        hsv=None,
        hex_color=None,
        color_name="blue",
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert bulb.light_module is not None
    assert bulb.light_module.hsv_calls == [(240, 100, 100)]


@pytest.mark.asyncio
async def test_set_unknown_color_name_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=None,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name="chartreuse",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_USAGE_ERROR


# ---------------------------------------------------------------------------
# Mutual-exclusion + at-least-one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_no_flags_is_usage_error(
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``set`` with no settings flag is a usage error."""
    with pytest.raises(UsageError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=None,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name=None,
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_USAGE_ERROR


@pytest.mark.asyncio
async def test_set_hsv_and_hex_together_is_usage_error(
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    with pytest.raises(UsageError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=None,
            color_temp=None,
            hsv="240,100,100",
            hex_color="#ffffff",
            color_name=None,
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_USAGE_ERROR


@pytest.mark.asyncio
async def test_set_color_and_hsv_together_is_usage_error(
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    with pytest.raises(UsageError):
        await run_set(
            target="kitchen-lamp",
            brightness=None,
            color_temp=None,
            hsv="1,2,3",
            hex_color=None,
            color_name="blue",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


# ---------------------------------------------------------------------------
# Capability gating (exit 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_brightness_on_dumb_plug_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """A device without Brightness capability returns exit 5."""
    plug = _make_dumb_plug()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return plug

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=50,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name=None,
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_UNSUPPORTED


@pytest.mark.asyncio
async def test_set_color_on_non_color_device_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """A dimmable-but-not-color device exits 5 on --color red."""
    dimmer = _make_dimmable_only()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dimmer

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=None,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name="red",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_UNSUPPORTED


@pytest.mark.asyncio
async def test_set_color_temp_on_non_tunable_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    dimmer = _make_dimmable_only()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dimmer

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=None,
            color_temp=4000,
            hsv=None,
            hex_color=None,
            color_name=None,
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_UNSUPPORTED


# ---------------------------------------------------------------------------
# Multi-socket strips
# ---------------------------------------------------------------------------


def _make_kp303_dimmable_strip() -> MockKasaDevice:
    """A KP303-shaped strip whose children all support Brightness."""
    children = [
        MockKasaDevice(
            alias=f"socket-{i}",
            model="KP303",
            is_on=False,
            light_module=MockLightModule(),
            capabilities=("Brightness",),
        )
        for i in range(1, 4)
    ]
    return MockKasaDevice(
        alias="office-strip",
        host="192.168.1.51",
        mac="AA:BB:CC:DD:EE:02",
        model="KP303(US)",
        children=children,
        # Parent has no per-device capabilities; the children carry them.
        capabilities=(),
    )


def _make_strip_with_dumb_children() -> MockKasaDevice:
    """A strip whose children DON'T support brightness."""
    children = [
        MockKasaDevice(
            alias=f"socket-{i}",
            model="HS300",
            is_on=False,
            light_module=None,
            capabilities=(),
        )
        for i in range(1, 4)
    ]
    return MockKasaDevice(
        alias="office-strip",
        model="HS300(US)",
        children=children,
        capabilities=(),
    )


@pytest.mark.asyncio
async def test_set_strip_socket_2_brightness_targets_child(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``--socket 2 --brightness 50`` only touches the second child."""
    strip = _make_kp303_dimmable_strip()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_set(
        target="office-strip",
        brightness=50,
        color_temp=None,
        hsv=None,
        hex_color=None,
        color_name=None,
        socket_arg="2",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert strip.children[1].light_module is not None
    assert strip.children[1].light_module.brightness_calls == [50]
    assert strip.children[0].light_module is not None
    assert strip.children[0].light_module.brightness_calls == []


@pytest.mark.asyncio
async def test_set_strip_socket_2_brightness_on_dumb_strip_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """A strip without dimmable children exits 5 even when a socket is named."""
    strip = _make_strip_with_dumb_children()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UnsupportedFeatureError) as info:
        await run_set(
            target="office-strip",
            brightness=50,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name=None,
            socket_arg="2",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_UNSUPPORTED


@pytest.mark.asyncio
async def test_set_strip_without_socket_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Multi-socket strip without --socket -> exit 64."""
    strip = _make_kp303_dimmable_strip()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_set(
            target="office-strip",
            brightness=50,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name=None,
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_USAGE_ERROR


@pytest.mark.asyncio
async def test_set_strip_socket_all_fans_out(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``--socket all`` applies the same brightness to every child."""
    strip = _make_kp303_dimmable_strip()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_set(
        target="office-strip",
        brightness=75,
        color_temp=None,
        hsv=None,
        hex_color=None,
        color_name=None,
        socket_arg="all",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    for child in strip.children:
        assert child.light_module is not None
        assert child.light_module.brightness_calls == [75]


# ---------------------------------------------------------------------------
# Out-of-range integers (FR-16 happy path is via Click IntRange in cli.py;
# the verb runner re-validates for direct callers and the error is exit 64).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_brightness_out_of_range_low(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Verb-level guard rejects brightness=-1."""
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=-1,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name=None,
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_USAGE_ERROR


@pytest.mark.asyncio
async def test_set_brightness_out_of_range_high(
    monkeypatch: pytest.MonkeyPatch,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Verb-level guard rejects brightness=101."""
    bulb = _make_color_bulb()

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return bulb

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_set(
            target="kitchen-lamp",
            brightness=101,
            color_temp=None,
            hsv=None,
            hex_color=None,
            color_name=None,
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == EXIT_USAGE_ERROR
