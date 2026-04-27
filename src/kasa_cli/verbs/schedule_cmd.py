"""``kasa-cli schedule list <target>`` (SRD §5.7, FR-24, FR-24a).

Read-only listing of device-stored schedule rules. Legacy IOT only — KLAP /
Smart-protocol devices fall through to ``UnsupportedFeatureError`` (exit 5)
with the SRD-mandated message about python-kasa 0.10.2 not exposing schedule
listing for KLAP devices. v1 will NEVER add ``schedule add/remove/edit``;
schedules belong in cron / systemd / launchd (SRD Decision 3).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from kasa_cli import wrapper
from kasa_cli.errors import EXIT_SUCCESS, DeviceError
from kasa_cli.output import OutputMode, emit_stream
from kasa_cli.wrapper import CredentialBundle


def _rule_to_text(rule: object) -> str:
    """One-line text rendering of a rule dict (TEXT mode only)."""
    if not isinstance(rule, dict):
        return str(rule)
    rid = rule.get("id", "-")
    enabled = "enabled" if rule.get("enabled") else "disabled"
    spec = rule.get("time_spec", "-")
    action = rule.get("action", "-")
    return f"{rid:<24} {enabled:<8} {spec:<32} -> {action}"


async def run_schedule_list(
    target: str,
    *,
    config_lookup: Callable[[str], tuple[str | None, str | None]],
    credentials: CredentialBundle,
    timeout: float,
    mode: OutputMode,
) -> int:
    """Execute ``schedule list``. Returns the desired exit code on success.

    Args:
        target: Alias / IP / MAC the user passed.
        config_lookup: Closure resolving target to ``(host, alias)``.
        credentials: Pre-resolved credentials.
        timeout: Per-operation connect timeout.
        mode: Output mode for stdout.

    Raises:
        UnsupportedFeatureError: Device is on KLAP / Smart protocol — python-
            kasa 0.10.2 does not expose a ``Schedule`` module under
            ``kasa/smart/modules/`` (FR-24a). Mapped to exit 5.
    """
    kdev = await wrapper.resolve_target(
        target,
        config_lookup=config_lookup,
        credentials=credentials,
        timeout=timeout,
    )
    try:
        # FR-9-style: refresh first so the rule list reflects on-device state.
        update = getattr(kdev, "update", None)
        if update is not None:
            try:
                await update()
            except Exception as exc:
                raise DeviceError(
                    f"Failed to refresh device state for {target!r}: {exc}",
                    target=target,
                ) from exc

        rules: list[dict[str, Any]] = [dict(r) for r in await wrapper.read_schedule(kdev)]
    finally:
        disconnect = getattr(kdev, "disconnect", None)
        if disconnect is not None:
            with contextlib.suppress(Exception):
                await disconnect()

    emit_stream(rules, mode, formatter=_rule_to_text)
    return EXIT_SUCCESS
