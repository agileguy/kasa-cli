"""``kasa-cli on`` / ``kasa-cli off`` (SRD §5.4, FR-11..15).

Phase 1 ships ``on`` and ``off`` only. ``toggle`` is deferred to Phase 2.

Idempotency (FR-14): calling ``on`` on an already-on device exits 0 silently.
Same for ``off``.

Multi-socket strips (FR-15): for the v1 multi-socket model set
``{KP303, KP400, EP40, HS300}``, the verb REQUIRES ``--socket <n>`` (1-indexed)
or ``--socket all``. Any other invocation against a multi-socket model is
exit code 64 (usage error) with a helpful sockets list.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Literal

import kasa

from kasa_cli import wrapper
from kasa_cli.errors import EXIT_OK, DeviceError, UnsupportedError, UsageError
from kasa_cli.output import OutputMode
from kasa_cli.wrapper import CredentialBundle

# FR-15 v1 multi-socket model set. python-kasa exposes children for these
# regardless of firmware-protocol family. KEEP THIS SET IN SYNC WITH SRD §5.4.
MULTI_SOCKET_MODELS: frozenset[str] = frozenset({"KP303", "KP400", "EP40", "HS300"})


def _is_multi_socket(model: str) -> bool:
    """Return True if ``model`` (e.g. ``"HS300(US)"``) is in the multi-socket set."""
    if not model:
        return False
    upper = model.upper()
    return any(upper.startswith(m) for m in MULTI_SOCKET_MODELS)


def _list_sockets(kdev: kasa.Device) -> list[tuple[int, str, str]]:
    """Return ``[(index, alias, state)]`` for each child socket."""
    out: list[tuple[int, str, str]] = []
    children = getattr(kdev, "children", None) or []
    for index, child in enumerate(children, start=1):
        alias = getattr(child, "alias", None) or f"socket-{index}"
        is_on = bool(getattr(child, "is_on", False))
        out.append((index, alias, "on" if is_on else "off"))
    return out


def _format_sockets_for_error(sockets: list[tuple[int, str, str]]) -> str:
    parts = [f"{i}={alias}({state})" for i, alias, state in sockets]
    return ", ".join(parts) if parts else "(no sockets reported by device)"


async def _toggle_one(kdev: kasa.Device, action: Literal["on", "off"]) -> None:
    """Apply ``on`` or ``off`` to one device or child socket. Idempotent."""
    is_on = bool(getattr(kdev, "is_on", False))
    if action == "on" and is_on:
        return  # FR-14
    if action == "off" and not is_on:
        return  # FR-14
    try:
        if action == "on":
            await kdev.turn_on()
        else:
            await kdev.turn_off()
    except Exception as exc:
        raise DeviceError(
            f"Device rejected '{action}' command: {exc}",
            target=getattr(kdev, "alias", None),
        ) from exc


async def run_onoff(
    *,
    action: Literal["on", "off"],
    target: str,
    socket_arg: str | None,
    config_lookup: Callable[[str], tuple[str | None, str | None]],
    credentials: CredentialBundle,
    timeout: float,
    mode: OutputMode,
) -> int:
    """Execute the on/off verb. Returns the desired exit code on success."""
    del mode  # on/off emit nothing on success per SRD; quiet by design.

    kdev = await wrapper.resolve_target(
        target,
        config_lookup=config_lookup,
        credentials=credentials,
        timeout=timeout,
    )
    try:
        # Refresh so children + is_on reflect live state before deciding.
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
                    hint="Try: kasa-cli on " + target + " --socket all",
                )

            children = list(getattr(kdev, "children", []) or [])
            if socket_arg.lower() == "all":
                for child in children:
                    await _toggle_one(child, action)
                return EXIT_OK

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
            await _toggle_one(children[index - 1], action)
            return EXIT_OK

        # Single-socket device. Allow only ``--socket 1`` or omitted.
        if socket_arg is not None:
            if socket_arg.lower() == "all":
                # Treat as a no-op specifier on a single-socket device.
                await _toggle_one(kdev, action)
                return EXIT_OK
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

        # We don't support strip-like devices that claim children but aren't in
        # the model allow-list. Fail loudly so we don't silently flip the wrong
        # outlet.
        children = list(getattr(kdev, "children", []) or [])
        if children and not multi:
            raise UnsupportedError(
                (
                    f"Target {target!r} reports child sockets but model "
                    f"{model!r} is not in the recognized multi-socket set. "
                    "File an issue with the model string."
                ),
                target=target,
            )

        await _toggle_one(kdev, action)
        return EXIT_OK
    finally:
        with contextlib.suppress(Exception):
            await kdev.disconnect()
