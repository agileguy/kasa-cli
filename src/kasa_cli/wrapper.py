"""Thin async layer over ``python-kasa``.

This module is the ONLY place in the project that imports ``kasa.*``. Verb
modules call ``wrapper.*`` exclusively — they never poke at python-kasa
directly. Keeping the boundary narrow means the rest of the codebase stays
testable with simple mocks and that any future protocol-library churn lands
in a single file.

Design notes
------------

* Engineer A owns ``credentials.py``, ``config.py``, and ``auth_cache.py``.
  This module does NOT import them at the module level. Callers (cli.py,
  verb modules) resolve credentials and the Config first, then pass plain
  values down. That makes the wrapper trivially testable without a config
  layer and avoids a circular dependency on Engineer A's branch.
* ``Discover.discover()`` already broadcasts to UDP/9999 and UDP/20002 in
  python-kasa 0.10.2 (verified empirically). FR-2 mentions 20004 too;
  python-kasa handles its own port set internally, so we just pass through.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import kasa
from kasa.exceptions import (
    AuthenticationError,
    KasaException,
    UnsupportedDeviceError,
)
from kasa.exceptions import TimeoutError as KasaTimeoutError

from kasa_cli.errors import (
    AuthError,
    DeviceError,
    NetworkError,
    NotFoundError,
    UnsupportedError,
)
from kasa_cli.types import Device, Socket

if TYPE_CHECKING:
    from kasa import Device as KasaDevice  # noqa: F401  (used only as type)


# --- Helpers ------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds resolution)."""
    return dt.datetime.now(tz=dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_mac(value: str | None) -> str:
    """Normalize a MAC string to uppercase colon-separated form."""
    if not value:
        return ""
    cleaned = value.replace("-", ":").replace(".", ":").upper()
    return cleaned


def _detect_protocol(device: kasa.Device) -> str:
    """Best-effort heuristic for ``protocol`` field in the Device record.

    python-kasa 0.10.2 exposes the encryption type via the active
    ``DeviceConfig.connection_type``. Legacy IOT plugs use the XOR transport;
    everything else is KLAP / Smart-protocol. The SRD enum is the closed pair
    ``{"legacy", "klap"}``.
    """
    cfg = getattr(device, "config", None)
    conn = getattr(cfg, "connection_type", None) if cfg is not None else None
    if conn is None:
        return "legacy"
    enc = getattr(conn, "encryption_type", None)
    enc_name = getattr(enc, "name", "") if enc is not None else ""
    if enc_name in ("Klap", "Aes", "KlapV2"):
        return "klap"
    return "legacy"


def _features_of(device: kasa.Device) -> list[str]:
    """Translate python-kasa's feature dict into the SRD's flat string list."""
    out: list[str] = []
    feats = getattr(device, "features", None)
    if isinstance(feats, dict):
        keys = sorted(feats.keys())
        out.extend(keys)
    return out


def _sockets_of(device: kasa.Device) -> list[Socket] | None:
    """Build the Socket list for multi-socket strips, or None."""
    children = getattr(device, "children", None)
    if not children:
        return None
    sockets: list[Socket] = []
    for index, child in enumerate(children, start=1):
        alias = getattr(child, "alias", None) or f"socket-{index}"
        is_on = bool(getattr(child, "is_on", False))
        sockets.append(Socket(index=index, alias=alias, state="on" if is_on else "off"))
    return sockets


def _state_of(device: kasa.Device) -> str:
    """Return ``"on"``, ``"off"``, or ``"mixed"`` for the device or strip."""
    children = getattr(device, "children", None)
    if children:
        states = [bool(getattr(c, "is_on", False)) for c in children]
        if all(states):
            return "on"
        if not any(states):
            return "off"
        return "mixed"
    return "on" if bool(getattr(device, "is_on", False)) else "off"


def to_device_record(
    kdev: kasa.Device,
    *,
    alias_override: str | None = None,
) -> Device:
    """Translate a ``kasa.Device`` instance into the SRD Device record.

    ``alias_override`` lets callers stamp a config-resolved alias when the
    device's stored alias is empty or differs.
    """
    hw_info = getattr(kdev, "hw_info", {}) or {}
    sys_info = getattr(kdev, "sys_info", {}) or {}
    # python-kasa exposes hw_info and sys_info as dicts that vary by family.
    # Use string-coerced lookups with sane fallbacks.
    hw_version = str(hw_info.get("hw_ver") or sys_info.get("hw_ver") or sys_info.get("hwVer") or "")
    fw_version = str(hw_info.get("sw_ver") or sys_info.get("sw_ver") or sys_info.get("swVer") or "")
    return Device(
        alias=alias_override or getattr(kdev, "alias", "") or "",
        ip=str(getattr(kdev, "host", "") or ""),
        mac=_normalize_mac(getattr(kdev, "mac", None)),
        model=str(getattr(kdev, "model", "") or ""),
        hardware_version=hw_version,
        firmware_version=fw_version,
        protocol=_detect_protocol(kdev),
        features=_features_of(kdev),
        state=_state_of(kdev),
        sockets=_sockets_of(kdev),
        last_seen=_utcnow_iso(),
    )


# --- Public API ---------------------------------------------------------------


@dataclass
class CredentialBundle:
    """Pre-resolved credentials passed by the caller (cli.py).

    The wrapper does not know which source supplied these — that's the
    credential-resolver's job. Either ``username``+``password`` are both set,
    or neither is set (legacy-only path).
    """

    username: str | None = None
    password: str | None = None

    @property
    def is_present(self) -> bool:
        return bool(self.username) and bool(self.password)


async def resolve_target(
    target: str,
    *,
    config_lookup: Callable[[str], tuple[str | None, str | None]],
    credentials: CredentialBundle,
    timeout: float = 5.0,
) -> kasa.Device:
    """Resolve an alias / IP / MAC ``target`` to a connected ``kasa.Device``.

    ``config_lookup`` is a callable that takes the raw ``target`` string and
    returns a tuple ``(host, alias_or_none)``. This indirection keeps the
    wrapper free of any direct dependency on Engineer A's ``config.py``.
    The CLI layer is expected to wrap a real Config instance in a closure
    before calling here; tests pass a plain dict-backed lambda.
    """
    try:
        host, _alias = config_lookup(target)
    except KeyError as exc:
        raise NotFoundError(
            f"Unknown target: {target!r}",
            target=target,
            hint="Run 'kasa-cli list' to see configured aliases.",
        ) from exc

    if not host:
        raise NotFoundError(
            f"No reachable host for target {target!r}",
            target=target,
        )

    creds: kasa.Credentials | None = None
    if credentials.is_present:
        creds = kasa.Credentials(
            username=credentials.username or "",
            password=credentials.password or "",
        )

    try:
        kdev = await asyncio.wait_for(
            kasa.Device.connect(
                host=host,
                config=kasa.DeviceConfig(host=host, credentials=creds, timeout=int(timeout)),
            ),
            timeout=timeout,
        )
    except AuthenticationError as exc:
        raise AuthError(
            f"Authentication rejected by {target!r}",
            target=target,
            hint="Verify ~/.config/kasa-cli/credentials has correct username/password.",
        ) from exc
    except KasaTimeoutError as exc:
        raise NetworkError(
            f"Timed out connecting to {target!r} ({host}) after {timeout:g}s",
            target=target,
        ) from exc
    except UnsupportedDeviceError as exc:
        raise UnsupportedError(
            f"Device {target!r} reports as unsupported: {exc}",
            target=target,
        ) from exc
    except TimeoutError as exc:
        raise NetworkError(
            f"Timed out connecting to {target!r} ({host}) after {timeout:g}s",
            target=target,
        ) from exc
    except KasaException as exc:
        raise DeviceError(
            f"Device error from {target!r}: {exc}",
            target=target,
        ) from exc

    return kdev


async def discover(
    *,
    timeout: float,
    target_network: str | None,
    credentials: CredentialBundle,
) -> list[Device]:
    """Broadcast-discover devices on the LAN (SRD §5.1).

    ``target_network`` is the directed-broadcast address (e.g.
    ``192.168.1.255``) — callers are responsible for converting a CIDR. When
    ``None`` we use python-kasa's default ``255.255.255.255`` and let the OS
    pick the interface (FR-5b's documented limitation on macOS multi-NIC).
    """
    kwargs: dict[str, object] = {
        "discovery_timeout": int(max(1, timeout)),
    }
    if target_network is not None:
        kwargs["target"] = target_network
    if credentials.is_present:
        kwargs["username"] = credentials.username
        kwargs["password"] = credentials.password

    try:
        result = await kasa.Discover.discover(**kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        # Broadcast bind failure, no usable interface, perm denied — FR-5a:
        # exit 3 (network) is reserved for these. Zero-result-on-time is OK.
        raise NetworkError(
            f"Discovery broadcast failed: {exc}",
            target=target_network,
            hint="On hosts with multiple interfaces, pass --target-network <CIDR>.",
        ) from exc
    except KasaException as exc:
        raise NetworkError(
            f"Discovery error: {exc}",
            target=target_network,
        ) from exc

    devices: list[Device] = []
    for kdev in result.values():
        devices.append(to_device_record(kdev))
    devices.sort(key=lambda d: (d.alias or "", d.ip))
    return devices


async def probe_alive(device: kasa.Device, *, timeout: float) -> bool:
    """Return True if ``device.update()`` succeeds within ``timeout``."""
    try:
        await asyncio.wait_for(device.update(), timeout=timeout)
        return True
    except (KasaException, TimeoutError, OSError):
        return False
