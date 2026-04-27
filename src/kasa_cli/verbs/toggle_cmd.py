"""``kasa-cli toggle`` (SRD §5.4, FR-13).

Flips the current on/off state of a target device. Unlike ``on``/``off``,
toggle is intentionally NOT idempotent — calling it twice cycles the device.

Multi-socket strips (FR-15)
---------------------------

For the v1 multi-socket model set ``{KP303, KP400, EP40, HS300}`` toggle
REQUIRES ``--socket <n>`` (1-indexed) or ``--socket all``. ``--socket all`` on
a strip flips **each socket independently** based on its own current state,
so a strip with sockets ``[on, off, on]`` becomes ``[off, on, off]`` (not
``[off, off, off]``). This matches the SRD's "flip" semantic per-target and
intentionally does not collapse mixed-state strips to a single uniform
state — operators who want that should use explicit ``on`` / ``off``.

Single-socket devices accept ``--socket 1`` or no flag interchangeably.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

import kasa

from kasa_cli import wrapper
from kasa_cli.errors import EXIT_SUCCESS, DeviceError, UnsupportedFeatureError, UsageError
from kasa_cli.output import OutputMode
from kasa_cli.verbs.onoff import (
    MULTI_SOCKET_MODELS,  # re-use the v1 allow-list
    _format_sockets_for_error,
    _is_multi_socket,
    _list_sockets,
)
from kasa_cli.wrapper import CredentialBundle

# Re-export so `from kasa_cli.verbs.toggle_cmd import MULTI_SOCKET_MODELS` works
# for tests that want to assert the same list as ``onoff``.
__all__ = ["MULTI_SOCKET_MODELS", "run_toggle"]


async def _flip_one(kdev: kasa.Device) -> None:
    """Flip a single device or socket. Raises :class:`DeviceError` on failure."""
    is_on = bool(getattr(kdev, "is_on", False))
    try:
        if is_on:
            await kdev.turn_off()
        else:
            await kdev.turn_on()
    except Exception as exc:
        raise DeviceError(
            f"Device rejected toggle command: {exc}",
            target=getattr(kdev, "alias", None),
        ) from exc


async def run_toggle(
    target: str,
    *,
    socket_arg: str | None,
    config_lookup: Callable[[str], tuple[str | None, str | None]],
    credentials: CredentialBundle,
    timeout: float,
    mode: OutputMode,
) -> int:
    """Execute the toggle verb. Returns the exit code on success."""
    del mode  # toggle is silent on success per SRD's control-verb convention.

    kdev = await wrapper.resolve_target(
        target,
        config_lookup=config_lookup,
        credentials=credentials,
        timeout=timeout,
    )
    try:
        # Refresh so `is_on` and children reflect current device state.
        try:
            await kdev.update()
        except Exception as exc:
            raise DeviceError(
                f"Failed to refresh state for {target!r}: {exc}",
                target=target,
            ) from exc

        model = str(getattr(kdev, "model", "") or "")
        multi = _is_multi_socket(model)

        if multi:
            if socket_arg is None:
                sockets = _list_sockets(kdev)
                raise UsageError(
                    (
                        f"Target {target!r} is a multi-socket strip ({model}); "
                        "pass --socket <n> (1-indexed) or --socket all. "
                        f"Available sockets: {_format_sockets_for_error(sockets)}"
                    ),
                    target=target,
                    hint="Try: kasa-cli toggle " + target + " --socket all",
                )

            children = list(getattr(kdev, "children", []) or [])
            if socket_arg.lower() == "all":
                # FR-13/FR-15: flip each socket independently. Mixed-state
                # strip ends up inverted per-socket.
                for child in children:
                    await _flip_one(child)
                return EXIT_SUCCESS

            try:
                index = int(socket_arg)
            except ValueError as exc:
                raise UsageError(
                    f"--socket value must be a positive integer or 'all'; got {socket_arg!r}",
                    target=target,
                ) from exc
            if index < 1 or index > len(children):
                sockets = _list_sockets(kdev)
                raise UsageError(
                    (
                        f"--socket {index} out of range for {target!r}; "
                        f"available: {_format_sockets_for_error(sockets)}"
                    ),
                    target=target,
                )
            await _flip_one(children[index - 1])
            return EXIT_SUCCESS

        # Single-socket device: only --socket 1 (or omitted) is valid.
        if socket_arg is not None:
            if socket_arg.lower() == "all":
                # On a single-socket device, treat as a no-op specifier.
                await _flip_one(kdev)
                return EXIT_SUCCESS
            try:
                idx = int(socket_arg)
            except ValueError as exc:
                raise UsageError(
                    f"--socket value must be a positive integer or 'all'; got {socket_arg!r}",
                    target=target,
                ) from exc
            if idx != 1:
                raise UsageError(
                    f"--socket {idx} not valid for single-socket device {target!r}",
                    target=target,
                )

        # Strip-shaped device that's not in the v1 multi-socket allow-list:
        # fail loudly rather than silently flipping the wrong outlet.
        children = list(getattr(kdev, "children", []) or [])
        if children and not multi:
            raise UnsupportedFeatureError(
                (
                    f"Target {target!r} reports child sockets but model "
                    f"{model!r} is not in the recognized multi-socket set. "
                    "File an issue with the model string."
                ),
                target=target,
            )

        await _flip_one(kdev)
        return EXIT_SUCCESS
    finally:
        with contextlib.suppress(Exception):
            await kdev.disconnect()
