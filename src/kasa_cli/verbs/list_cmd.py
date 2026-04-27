"""``kasa-cli list`` (SRD §5.2, FR-6..8).

By default this is a pure config-read: alias, ip, mac with ``online: null``.
``--probe`` issues a per-device liveness check (concurrency-bounded) and
populates ``online`` as ``true``/``false``. ``--online-only`` implies
``--probe`` and filters to live devices.

The ListView shape per FR-6b is ``{alias, ip, mac, online: bool|null}``.
Order: aliases as defined in config (preserve insertion order).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from kasa_cli import wrapper
from kasa_cli.errors import EXIT_SUCCESS, KasaCliError
from kasa_cli.output import OutputMode, emit_stream, list_view_to_text
from kasa_cli.wrapper import CredentialBundle


async def _probe_one(
    alias: str,
    host: str | None,
    *,
    credentials: CredentialBundle,
    timeout: float,
) -> bool:
    """Connect+update one device. ``False`` on any failure.

    R1 boundary: this verb does NOT import ``kasa.*`` directly. All
    interaction with python-kasa goes through :mod:`kasa_cli.wrapper`.
    """
    if not host:
        return False

    def _config_lookup(_target: str) -> tuple[str | None, str | None]:
        return host, alias or None

    try:
        kdev = await wrapper.resolve_target(
            host,
            config_lookup=_config_lookup,
            credentials=credentials,
            timeout=timeout,
        )
    except KasaCliError:
        return False
    except Exception:
        return False

    try:
        return await wrapper.probe_alive(kdev, timeout=timeout)
    finally:
        # The wrapper's resolve_target returns a connected python-kasa Device;
        # disconnect is best-effort and must not propagate failures up to the
        # liveness boolean.
        disconnect = getattr(kdev, "disconnect", None)
        if callable(disconnect):
            with contextlib.suppress(Exception):
                await disconnect()


async def run_list(
    *,
    devices_section: list[dict[str, Any]],
    probe: bool,
    online_only: bool,
    credentials: CredentialBundle,
    timeout: float,
    concurrency: int,
    mode: OutputMode,
) -> int:
    """Execute the list verb.

    ``devices_section`` is a config-derived list of dicts shaped as
    ``{alias, ip, mac}``. The wrapper does not depend on Engineer A's Config
    type; the CLI layer flattens config first and hands us a plain list.
    """
    do_probe = probe or online_only

    views: list[dict[str, Any]] = []
    if do_probe:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _gather(entry: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                online = await _probe_one(
                    entry.get("alias", ""),
                    entry.get("ip"),
                    credentials=credentials,
                    timeout=timeout,
                )
            return {
                "alias": entry.get("alias", ""),
                "ip": entry.get("ip"),
                "mac": entry.get("mac"),
                "online": online,
            }

        views = list(await asyncio.gather(*(_gather(d) for d in devices_section)))
    else:
        views = [
            {
                "alias": d.get("alias", ""),
                "ip": d.get("ip"),
                "mac": d.get("mac"),
                "online": None,
            }
            for d in devices_section
        ]

    if online_only:
        views = [v for v in views if v["online"]]

    emit_stream(views, mode, formatter=lambda v: list_view_to_text(v))  # type: ignore[arg-type]
    return EXIT_SUCCESS
