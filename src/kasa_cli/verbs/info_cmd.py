"""``kasa-cli info <target>`` (SRD §5.3, FR-9, FR-10).

Resolves ``target`` to a connected ``kasa.Device``, calls ``update()`` to
populate live state (children, emeter, etc.), and emits the full Device
record per §10.1.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from kasa_cli import wrapper
from kasa_cli.errors import EXIT_OK, DeviceError
from kasa_cli.output import OutputMode, device_to_text, emit
from kasa_cli.wrapper import CredentialBundle


async def run_info(
    *,
    target: str,
    config_lookup: Callable[[str], tuple[str | None, str | None]],
    credentials: CredentialBundle,
    timeout: float,
    mode: OutputMode,
) -> int:
    """Execute the info verb."""
    kdev = await wrapper.resolve_target(
        target,
        config_lookup=config_lookup,
        credentials=credentials,
        timeout=timeout,
    )
    try:
        # FR-9 requires a live update before reporting state.
        try:
            await kdev.update()
        except Exception as exc:
            raise DeviceError(
                f"Failed to refresh device state for {target!r}: {exc}",
                target=target,
            ) from exc

        _, alias = config_lookup(target)
        record = wrapper.to_device_record(kdev, alias_override=alias)
    finally:
        with contextlib.suppress(Exception):
            await kdev.disconnect()

    emit(record, mode, formatter=lambda d: device_to_text(d))  # type: ignore[arg-type]
    return EXIT_OK
