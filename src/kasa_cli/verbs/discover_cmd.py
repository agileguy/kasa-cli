"""``kasa-cli discover`` (SRD §5.1, FR-1..5b).

Broadcast-discover devices on the LAN, emit one Device record per responder.
Zero-result-on-time exits 0 with empty output and a single INFO log line on
stderr (FR-5a). Broadcast-bind failures bubble up as :class:`NetworkError`
from :mod:`kasa_cli.wrapper`.
"""

from __future__ import annotations

import sys

from kasa_cli import wrapper
from kasa_cli.errors import EXIT_OK
from kasa_cli.output import OutputMode, device_to_text, emit_stream
from kasa_cli.wrapper import CredentialBundle


async def run_discover(
    *,
    timeout: float,
    target_network: str | None,
    credentials: CredentialBundle,
    mode: OutputMode,
) -> int:
    """Execute the discover verb.

    Returns the desired process exit code; the CLI top-level handler is
    responsible for actually invoking ``sys.exit``. Network errors bubble up
    as exceptions and are translated by the top-level handler.
    """
    devices = await wrapper.discover(
        timeout=timeout,
        target_network=target_network,
        credentials=credentials,
    )

    if not devices:
        # FR-5a: zero responders within the timeout is success, not an error.
        sys.stderr.write(f"INFO timeout reached, 0 devices found (timeout={timeout:g}s)\n")

    emit_stream(devices, mode, formatter=lambda d: device_to_text(d))  # type: ignore[arg-type]
    return EXIT_OK
