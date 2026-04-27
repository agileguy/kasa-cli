"""Tests for ``kasa-cli toggle`` (FR-13)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kasa_cli.errors import EXIT_SUCCESS, UsageError
from kasa_cli.output import OutputMode
from kasa_cli.verbs.toggle_cmd import run_toggle
from kasa_cli.wrapper import CredentialBundle


@pytest.mark.asyncio
async def test_toggle_off_to_on(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """A device that's currently off becomes on after toggle."""
    dev = make_device(alias="kitchen-lamp", model="HS100", is_on=False)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_toggle(
        target="kitchen-lamp",
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert dev.turn_on_called == 1
    assert dev.turn_off_called == 0


@pytest.mark.asyncio
async def test_toggle_on_to_off(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """A device that's currently on becomes off after toggle."""
    dev = make_device(alias="kitchen-lamp", model="HS100", is_on=True)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_toggle(
        target="kitchen-lamp",
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert dev.turn_off_called == 1
    assert dev.turn_on_called == 0


@pytest.mark.asyncio
async def test_toggle_is_not_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Two toggles must produce two device commands (NOT collapsed like on/on)."""
    dev = make_device(alias="kitchen-lamp", model="HS100", is_on=False)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    # First toggle: off -> on
    await run_toggle(
        target="kitchen-lamp",
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    # Second toggle: on -> off
    await run_toggle(
        target="kitchen-lamp",
        socket_arg=None,
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert dev.turn_on_called == 1
    assert dev.turn_off_called == 1


@pytest.mark.asyncio
async def test_toggle_strip_without_socket_is_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """FR-15: a multi-socket strip without --socket must exit 64 with sockets list."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_toggle(
            target="office-strip",
            socket_arg=None,
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == 64
    # Lists each socket index for the user to pick.
    assert "1=" in info.value.message
    assert "2=" in info.value.message
    assert "3=" in info.value.message


@pytest.mark.asyncio
async def test_toggle_strip_specific_socket_flips_only_that_one(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``--socket 2`` flips only socket-2; the other sockets are untouched."""

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    # hs300_strip fixture: socket-a on, socket-b off, socket-c off.
    code = await run_toggle(
        target="office-strip",
        socket_arg="2",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    # Only the second child should have received a turn_on (it was off).
    assert hs300_strip.children[0].turn_on_called == 0
    assert hs300_strip.children[0].turn_off_called == 0
    assert hs300_strip.children[1].turn_on_called == 1
    assert hs300_strip.children[1].turn_off_called == 0
    assert hs300_strip.children[2].turn_on_called == 0
    assert hs300_strip.children[2].turn_off_called == 0


@pytest.mark.asyncio
async def test_toggle_strip_socket_all_inverts_each_independently(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``--socket all`` flips each child based on its own state, not collapsed.

    hs300_strip is [on, off, off]. After toggle --socket all it becomes
    [off, on, on] — i.e. inverted per-socket.
    """

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_toggle(
        target="office-strip",
        socket_arg="all",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    # Socket-a was on -> turn_off; sockets b and c were off -> turn_on.
    assert hs300_strip.children[0].turn_off_called == 1
    assert hs300_strip.children[0].turn_on_called == 0
    assert hs300_strip.children[1].turn_on_called == 1
    assert hs300_strip.children[1].turn_off_called == 0
    assert hs300_strip.children[2].turn_on_called == 1
    assert hs300_strip.children[2].turn_off_called == 0


@pytest.mark.asyncio
async def test_toggle_strip_out_of_range_socket(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError) as info:
        await run_toggle(
            target="office-strip",
            socket_arg="99",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )
    assert info.value.exit_code == 64


@pytest.mark.asyncio
async def test_toggle_strip_bad_socket_value(
    monkeypatch: pytest.MonkeyPatch,
    hs300_strip: Any,
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return hs300_strip

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError):
        await run_toggle(
            target="office-strip",
            socket_arg="not-a-number",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


@pytest.mark.asyncio
async def test_toggle_single_socket_rejects_socket_n_other_than_1(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """Single-socket device + --socket 3 -> usage error."""
    dev = make_device(model="HS100", is_on=False)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    with pytest.raises(UsageError):
        await run_toggle(
            target="kitchen-lamp",
            socket_arg="3",
            config_lookup=fake_lookup,
            credentials=CredentialBundle(),
            timeout=1.0,
            mode=OutputMode.JSONL,
        )


@pytest.mark.asyncio
async def test_toggle_single_socket_socket_1_works(
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
    fake_lookup: Callable[[str], tuple[str | None, str | None]],
) -> None:
    """``--socket 1`` on a single-socket device is accepted (FR-15)."""
    dev = make_device(model="HS100", is_on=False)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    code = await run_toggle(
        target="kitchen-lamp",
        socket_arg="1",
        config_lookup=fake_lookup,
        credentials=CredentialBundle(),
        timeout=1.0,
        mode=OutputMode.JSONL,
    )
    assert code == EXIT_SUCCESS
    assert dev.turn_on_called == 1
