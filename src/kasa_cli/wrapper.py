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
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import kasa
from kasa.exceptions import (
    AuthenticationError,
    KasaException,
    UnsupportedDeviceError,
)
from kasa.exceptions import TimeoutError as KasaTimeoutError
from kasa.module import Module

from kasa_cli.errors import (
    AuthError,
    DeviceError,
    NetworkError,
    NotFoundError,
    UnsupportedFeatureError,
)
from kasa_cli.types import Device, Reading, Socket

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


def _detect_protocol(device: kasa.Device) -> Literal["legacy", "klap"]:
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


def _state_of(device: kasa.Device) -> Literal["on", "off", "mixed"]:
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

    # python-kasa 0.10.2's DeviceConfig.timeout is typed ``int | None``;
    # ``int(0.5)`` truncates to 0 and disables timeouts, so we ceil to a
    # minimum of 1 second. The ``asyncio.wait_for`` outer guard still uses
    # the original float for sub-second cancellation precision.
    device_timeout = max(1, math.ceil(timeout))
    try:
        kdev = await asyncio.wait_for(
            kasa.Device.connect(
                host=host,
                config=kasa.DeviceConfig(host=host, credentials=creds, timeout=device_timeout),
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
        raise UnsupportedFeatureError(
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


# --- Phase 2 Engineer B additions ---------------------------------------------
#
# These helpers cover the SRD §5.6 (Energy) and §5.7 (Schedule) verbs. They are
# kept inside ``wrapper.py`` so the ``kasa.*`` import boundary stays narrow —
# verb modules consume the returned dataclasses without touching python-kasa.
#
# Engineer A is independently appending light-control helpers (``set_brightness``,
# ``set_color_temp``, ``set_hsv``) below this section in their own delimited
# block. The PM merge will concatenate both blocks; do NOT relocate code from
# this section.


def _is_ep40m(model: str | None) -> bool:
    """Return True for EP40M variants (no hardware emeter — SRD §3.1)."""
    if not model:
        return False
    return model.upper().startswith("EP40M")


def _energy_module(kdev: kasa.Device) -> object | None:
    """Return the :class:`kasa.interfaces.Energy` module on ``kdev`` or ``None``.

    Typed as ``object | None`` rather than ``Energy | None`` because
    ``kasa_cli.wrapper`` is the sole owner of the ``kasa.*`` boundary and the
    Energy interface type doesn't need to leak into mypy contexts elsewhere.
    """
    modules = getattr(kdev, "modules", None)
    if modules is None:
        return None
    try:
        result: object | None = modules.get(Module.Energy)
    except Exception:  # pragma: no cover — defensive
        return None
    return result


def _coerce_float(value: object) -> float | None:
    """Best-effort coerce ``value`` to ``float``; ``None`` if it can't be."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_one_emeter(
    energy: object,
    *,
    cumulative: bool,
) -> tuple[float, float, float, float | None, float | None]:
    """Pull live readings out of one Energy module instance.

    Returns ``(power_w, voltage_v, current_a, today_kwh, month_kwh)``. The
    cumulative pair is ``(None, None)`` when ``cumulative=False``.
    """
    power = _coerce_float(getattr(energy, "current_consumption", None)) or 0.0
    voltage = _coerce_float(getattr(energy, "voltage", None)) or 0.0
    current = _coerce_float(getattr(energy, "current", None)) or 0.0
    today: float | None = None
    month: float | None = None
    if cumulative:
        today = _coerce_float(getattr(energy, "consumption_today", None))
        month = _coerce_float(getattr(energy, "consumption_this_month", None))
    return power, voltage, current, today, month


async def read_energy(
    kdev: kasa.Device,
    *,
    socket: int | None,
    cumulative: bool,
    alias_override: str | None = None,
) -> Reading:
    """Return an SRD §10.3 Reading for ``kdev`` (or one of its child sockets).

    Args:
        kdev: A connected ``kasa.Device``. Caller is responsible for an initial
            ``update()`` so the Energy module's properties are populated.
        socket: 1-indexed child socket on a multi-socket strip (HS300). When
            ``None``, the strip total is returned (parent Energy module if
            present, otherwise the sum of children's emeters).
        cumulative: Whether to populate ``today_kwh`` / ``month_kwh``.
        alias_override: Optional alias to stamp on the Reading; falls back to
            ``kdev.alias`` (or the child's alias when ``socket`` is set).

    Raises:
        UnsupportedFeatureError: ``kdev`` lacks an Energy module entirely
            (e.g., HS200, HS210, EP40M).
    """
    model = str(getattr(kdev, "model", "") or "")
    if _is_ep40m(model):
        raise UnsupportedFeatureError(
            (
                f"EP40M ({model}) is supported as a device but lacks a hardware "
                "emeter. Energy readings are not available on this model."
            ),
            target=getattr(kdev, "alias", None),
            hint="Use a KP115/KP125/HS110/HS300/EP25 for energy monitoring.",
        )

    parent_alias = alias_override or str(getattr(kdev, "alias", "") or "")

    # Per-socket path — explicit child query.
    if socket is not None:
        children = list(getattr(kdev, "children", []) or [])
        if not children:
            raise UnsupportedFeatureError(
                f"Target {parent_alias!r} has no child sockets; --socket invalid",
                target=parent_alias,
            )
        if socket < 1 or socket > len(children):
            raise UnsupportedFeatureError(
                f"--socket {socket} out of range (1..{len(children)}) for {parent_alias!r}",
                target=parent_alias,
            )
        child = children[socket - 1]
        child_energy = _energy_module(child)
        if child_energy is None:
            raise UnsupportedFeatureError(
                (
                    f"Socket {socket} on {parent_alias!r} ({model}) does not expose "
                    "an Energy module on python-kasa 0.10.2."
                ),
                target=parent_alias,
            )
        power, voltage, current, today, month = _read_one_emeter(
            child_energy, cumulative=cumulative
        )
        child_alias = str(getattr(child, "alias", None) or f"socket-{socket}")
        return Reading(
            ts=_utcnow_iso(),
            alias=child_alias,
            socket=socket,
            current_power_w=power,
            voltage_v=voltage,
            current_a=current,
            today_kwh=today,
            month_kwh=month,
        )

    # Strip total / single-socket path. Prefer the parent's Energy module if
    # present (KP115, EP25, etc., and HS300 in some firmware revs); otherwise
    # sum the child emeters (HS300 firmware revs that only expose per-socket).
    parent_energy = _energy_module(kdev)
    if parent_energy is not None:
        power, voltage, current, today, month = _read_one_emeter(
            parent_energy, cumulative=cumulative
        )
        return Reading(
            ts=_utcnow_iso(),
            alias=parent_alias,
            socket=None,
            current_power_w=power,
            voltage_v=voltage,
            current_a=current,
            today_kwh=today,
            month_kwh=month,
        )

    children = list(getattr(kdev, "children", []) or [])
    if not children:
        raise UnsupportedFeatureError(
            (
                f"Target {parent_alias!r} ({model}) does not expose energy "
                "monitoring on python-kasa 0.10.2."
            ),
            target=parent_alias,
            hint="Energy monitoring is supported on HS110/HS300/KP115/KP125/EP25.",
        )

    # Sum-the-children fallback. Power and current are summed (the strip
    # really does draw the sum of its sockets); voltage cannot be meaningfully
    # summed because all sockets share the AC line, so we surface the LAST
    # non-zero voltage we observed across the children.
    #
    # We pick "last non-zero" rather than "first" because:
    #   1. Children share the AC line — every reporting socket sees the same
    #      voltage in steady state, so any non-zero reading is correct.
    #   2. Walking children in order and overwriting on each non-zero read
    #      makes the implementation a one-liner, and review feedback (C3)
    #      flagged the prior docstring lie ("first child that reports it"
    #      while the loop kept overwriting). The contract now matches the
    #      code: last non-zero wins.
    total_power = 0.0
    last_voltage = 0.0
    total_current = 0.0
    today_total: float | None = 0.0 if cumulative else None
    month_total: float | None = 0.0 if cumulative else None
    saw_any_emeter = False
    for child in children:
        e = _energy_module(child)
        if e is None:
            continue
        saw_any_emeter = True
        power, voltage, current, today, month = _read_one_emeter(e, cumulative=cumulative)
        total_power += power
        if voltage:
            last_voltage = voltage
        total_current += current
        if cumulative:
            if today is not None and today_total is not None:
                today_total += today
            if month is not None and month_total is not None:
                month_total += month

    if not saw_any_emeter:
        raise UnsupportedFeatureError(
            (
                f"Target {parent_alias!r} ({model}) reports children but none expose "
                "an Energy module."
            ),
            target=parent_alias,
        )

    return Reading(
        ts=_utcnow_iso(),
        alias=parent_alias,
        socket=None,
        current_power_w=total_power,
        voltage_v=last_voltage,
        current_a=total_current,
        today_kwh=today_total,
        month_kwh=month_total,
    )


def _format_wday(wday: object) -> str:
    """Translate python-kasa's 7-element weekday list into a flat label.

    The schedule rule's ``wday`` is a list of 0/1 ints. Index ordering is the
    device's; we tolerate either Mon-first or Sun-first by emitting names
    that match the *positions* python-kasa uses upstream (Mon..Sun in the
    test fixtures). Tools that need machine-precise weekday lookups should
    consume the rule dict's structured fields directly.
    """
    if not isinstance(wday, (list, tuple)):
        return ""
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    enabled: list[str] = []
    for i, flag in enumerate(wday):
        if i >= len(names):
            break
        try:
            if int(flag):
                enabled.append(names[i])
        except (TypeError, ValueError):
            continue
    return ",".join(enabled)


def _format_smin(smin: object) -> str:
    """Render minutes-since-midnight as ``HH:MM``; empty string on garbage."""
    if not isinstance(smin, (int, float)) or isinstance(smin, bool):
        return ""
    try:
        m = int(smin)
    except (TypeError, ValueError):
        return ""
    if m < 0 or m >= 24 * 60:
        return ""
    return f"{m // 60:02d}:{m % 60:02d}"


def _action_label(action: object) -> str:
    """Translate a python-kasa ``Action`` enum (or int) into a flat string."""
    # Accept the enum, the underlying int, or anything with a ``name`` attr.
    name = getattr(action, "name", None)
    if isinstance(name, str):
        if name == "TurnOn":
            return "on"
        if name == "TurnOff":
            return "off"
        if name == "Disabled":
            return "disabled"
        if name == "Unknown":
            return "unknown"
        return name.lower()
    if not isinstance(action, (int, float)) or isinstance(action, bool):
        return "unknown"
    try:
        as_int = int(action)
    except (TypeError, ValueError):
        return "unknown"
    return {-1: "disabled", 0: "off", 1: "on"}.get(as_int, "unknown")


def _rule_to_dict(rule: object) -> dict[str, object]:
    """Translate one python-kasa ``Rule`` (or rule-shaped object) into a dict."""
    rule_id = str(getattr(rule, "id", "") or "")
    enable = bool(getattr(rule, "enable", 0) or 0)
    repeat = int(getattr(rule, "repeat", 0) or 0)
    wday_repr = _format_wday(getattr(rule, "wday", None))
    smin_repr = _format_smin(getattr(rule, "smin", None))
    action_repr = _action_label(getattr(rule, "sact", None))

    if repeat and wday_repr:
        time_spec = f"weekly {wday_repr} {smin_repr}".strip()
    elif smin_repr:
        time_spec = f"daily {smin_repr}"
    else:
        time_spec = "unspecified"

    return {
        "id": rule_id,
        "enabled": enable,
        "time_spec": time_spec,
        "action": action_repr,
    }


async def read_schedule(kdev: kasa.Device) -> list[dict[str, object]]:
    """Return the list of rule dicts on ``kdev`` (legacy IOT only).

    Raises:
        UnsupportedFeatureError: KLAP/Smart device — python-kasa 0.10.2 does
            not expose a schedule module under ``kasa/smart/modules/``. The
            error message matches the exact string mandated by SRD FR-24a.
    """
    modules = getattr(kdev, "modules", None)
    schedule_module: object | None = None
    if modules is not None:
        try:
            schedule_module = modules.get(Module.IotSchedule)
        except Exception:  # pragma: no cover — defensive
            schedule_module = None

    if schedule_module is None:
        raise UnsupportedFeatureError(
            (
                "python-kasa 0.10.2 does not expose schedule listing for "
                "KLAP/Smart-protocol devices; revisit when upstream adds a "
                "Schedule module to kasa/smart/modules/."
            ),
            target=getattr(kdev, "alias", None),
            hint="Use cron / systemd timers / launchd for KLAP devices.",
        )

    rules_attr = getattr(schedule_module, "rules", None)
    rules: list[object]
    if rules_attr is None:
        rules = []
    elif callable(rules_attr):
        try:
            rules = list(rules_attr())  # support older method-shaped APIs
        except Exception:
            rules = []
    else:
        rules = list(rules_attr)
    return [_rule_to_dict(r) for r in rules]


# --- Light / dimming helpers (Phase 2) ----------------------------------------
#
# python-kasa 0.10.x exposes brightness/color/color-temp control via the
# ``Light`` interface module: ``device.modules.get(Module.Light)`` returns an
# object with ``set_brightness``, ``set_hsv``, ``set_color_temp`` (or None if
# the device does not advertise the capability). For multi-socket strips the
# Light module lives on the per-socket child device, not the parent — callers
# pass ``socket=N`` (1-indexed) and we route to ``device.children[N-1]``.
#
# Capability detection uses the narrower modules (``Module.Brightness``,
# ``Module.Color``, ``Module.ColorTemperature``) so the verb can return a
# precise ``unsupported_feature`` exit code per FR-20 (e.g. "this device is
# dimmable but not color-capable") instead of a generic device error.


def _select_target(kdev: kasa.Device, socket: int | None) -> kasa.Device:
    """Pick the parent device or one of its children based on ``socket``.

    * Single-socket devices accept ``socket=None`` or ``socket=1``; any other
      explicit socket index is a usage error.
    * Multi-socket devices REQUIRE an explicit ``socket`` (the verb layer
      enforces this; the wrapper just trusts what it's handed). ``socket=N``
      maps to ``children[N-1]`` 1-indexed; out-of-range raises ``UsageError``.
    """
    from kasa_cli.errors import UsageError

    children = list(getattr(kdev, "children", None) or [])
    if not children:
        if socket is not None and socket != 1:
            raise UsageError(
                f"--socket {socket} not valid for single-socket device",
                target=getattr(kdev, "alias", None),
            )
        return kdev
    if socket is None:
        # The verb layer enforces the require-socket rule for multi-socket
        # strips. If we get here without one, the caller has a bug.
        raise UsageError(
            "Multi-socket device requires --socket <n> or --socket all",
            target=getattr(kdev, "alias", None),
        )
    if socket < 1 or socket > len(children):
        raise UsageError(
            f"--socket {socket} out of range (1..{len(children)})",
            target=getattr(kdev, "alias", None),
        )
    child: kasa.Device = children[socket - 1]
    return child


def _light_module(kdev: kasa.Device) -> object | None:
    """Return the ``Light`` interface module if the device advertises it."""
    modules = getattr(kdev, "modules", None)
    if modules is None:
        return None
    try:
        # Late import: ``kasa.Module`` is part of python-kasa's public API.
        from kasa import Module

        light = modules.get(Module.Light) if hasattr(modules, "get") else None
    except (ImportError, AttributeError):
        return None
    return light


def _has_module(kdev: kasa.Device, module_name: str) -> bool:
    """Return True if ``kdev.modules`` exposes the named module.

    ``module_name`` is the string attribute on ``kasa.Module`` (e.g.
    ``"Brightness"``, ``"Color"``, ``"ColorTemperature"``).
    """
    modules = getattr(kdev, "modules", None)
    if modules is None:
        return False
    try:
        from kasa import Module

        mod = getattr(Module, module_name, None)
    except ImportError:
        return False
    if mod is None:
        return False
    if hasattr(modules, "__contains__"):
        try:
            return mod in modules
        except TypeError:
            return False
    return False


async def set_brightness(
    kdev: kasa.Device,
    brightness: int,
    *,
    socket: int | None = None,
) -> None:
    """Set brightness 0..100 on a dimmable device or per-socket child (FR-16).

    Raises :class:`UnsupportedFeatureError` (exit 5) if the device (or the
    selected socket) does not advertise the ``Brightness`` module. Raises
    :class:`UsageError` (exit 64) if ``brightness`` is out of range.
    """
    from kasa_cli.errors import UsageError

    if not isinstance(brightness, int) or brightness < 0 or brightness > 100:
        raise UsageError(
            f"--brightness must be an integer in [0, 100]; got {brightness!r}",
            target=getattr(kdev, "alias", None),
        )

    target = _select_target(kdev, socket)
    if not _has_module(target, "Brightness"):
        raise UnsupportedFeatureError(
            "Device does not support brightness control.",
            target=getattr(target, "alias", None) or getattr(kdev, "alias", None),
            hint="Only dimmable bulbs/dimmers expose --brightness.",
        )
    light = _light_module(target)
    if light is None or not hasattr(light, "set_brightness"):
        raise UnsupportedFeatureError(
            "Device advertises Brightness but no Light module is attached.",
            target=getattr(target, "alias", None) or getattr(kdev, "alias", None),
        )
    try:
        await light.set_brightness(brightness)
    except UnsupportedDeviceError as exc:
        raise UnsupportedFeatureError(
            f"Brightness rejected: {exc}",
            target=getattr(target, "alias", None),
        ) from exc
    except KasaException as exc:
        raise DeviceError(
            f"Brightness command failed: {exc}",
            target=getattr(target, "alias", None),
        ) from exc


async def set_color_temp(
    kdev: kasa.Device,
    kelvin: int,
    *,
    socket: int | None = None,
) -> None:
    """Set color temperature in kelvin on a tunable-white device (FR-17).

    Clamps to the device's supported range when the Light interface exposes
    it; otherwise lets python-kasa raise and maps the exception. Raises
    :class:`UnsupportedFeatureError` if the device lacks ``ColorTemperature``.
    """
    from kasa_cli.errors import UsageError

    if not isinstance(kelvin, int) or kelvin <= 0:
        raise UsageError(
            f"--color-temp must be a positive integer (kelvin); got {kelvin!r}",
            target=getattr(kdev, "alias", None),
        )
    # R3: plausibility guard. Real-world tunable-white bulbs run 2500-6500K;
    # the broadest physical range that makes sense for any consumer LED is
    # roughly [1000, 12000]. Values outside that band almost always mean the
    # user wrote ``--color-temp 27000`` thinking "2700K" or appended an extra
    # zero by accident. Catch it here with a hint that points at the likely
    # intent rather than letting the wrapper pass the bogus value to the
    # device's clamp logic, which would silently clip to e.g. 6500K and
    # apply a setting nobody asked for.
    if not (1000 <= kelvin <= 12000):
        raise UsageError(
            (
                f"--color-temp {kelvin}K is outside the plausible range "
                f"[1000, 12000]; did you mean {kelvin // 10}K?"
            ),
            target=getattr(kdev, "alias", None),
        )

    target = _select_target(kdev, socket)
    if not _has_module(target, "ColorTemperature"):
        raise UnsupportedFeatureError(
            "Device does not support color-temperature control.",
            target=getattr(target, "alias", None) or getattr(kdev, "alias", None),
            hint="Only tunable-white bulbs expose --color-temp.",
        )
    light = _light_module(target)
    if light is None or not hasattr(light, "set_color_temp"):
        raise UnsupportedFeatureError(
            "Device advertises ColorTemperature but no Light module is attached.",
            target=getattr(target, "alias", None) or getattr(kdev, "alias", None),
        )

    # Clamp to device-reported range when it advertises one. The canonical
    # 0.10.x access path is ``light.get_feature("color_temp")`` returning a
    # Feature with ``minimum_value`` / ``maximum_value`` integers.
    clamped = kelvin
    try:
        feat = light.get_feature("color_temp") if hasattr(light, "get_feature") else None
    except Exception:  # feature lookup MUST never crash this path
        feat = None
    if feat is not None:
        lo = getattr(feat, "minimum_value", None)
        hi = getattr(feat, "maximum_value", None)
        if isinstance(lo, int) and clamped < lo:
            clamped = lo
        if isinstance(hi, int) and clamped > hi:
            clamped = hi

    try:
        await light.set_color_temp(clamped)
    except UnsupportedDeviceError as exc:
        raise UnsupportedFeatureError(
            f"Color-temp rejected: {exc}",
            target=getattr(target, "alias", None),
        ) from exc
    except KasaException as exc:
        raise DeviceError(
            f"Color-temp command failed: {exc}",
            target=getattr(target, "alias", None),
        ) from exc


async def set_hsv(
    kdev: kasa.Device,
    h: int,
    s: int,
    v: int,
    *,
    socket: int | None = None,
) -> None:
    """Set HSV on a color-capable bulb (FR-18 / FR-19 / FR-19a).

    Validates ``0 <= h < 360``, ``0 <= s <= 100``, ``0 <= v <= 100``. Raises
    :class:`UnsupportedFeatureError` if the device lacks the ``Color`` module.
    """
    from kasa_cli.errors import UsageError

    if not (isinstance(h, int) and 0 <= h < 360):
        raise UsageError(
            f"--hsv hue must be an integer in [0, 360); got {h!r}",
            target=getattr(kdev, "alias", None),
        )
    if not (isinstance(s, int) and 0 <= s <= 100):
        raise UsageError(
            f"--hsv saturation must be an integer in [0, 100]; got {s!r}",
            target=getattr(kdev, "alias", None),
        )
    if not (isinstance(v, int) and 0 <= v <= 100):
        raise UsageError(
            f"--hsv value must be an integer in [0, 100]; got {v!r}",
            target=getattr(kdev, "alias", None),
        )

    target = _select_target(kdev, socket)
    if not _has_module(target, "Color"):
        raise UnsupportedFeatureError(
            "Device does not support color (HSV) control.",
            target=getattr(target, "alias", None) or getattr(kdev, "alias", None),
            hint="Only color bulbs and light strips expose --hsv/--hex/--color.",
        )
    light = _light_module(target)
    if light is None or not hasattr(light, "set_hsv"):
        raise UnsupportedFeatureError(
            "Device advertises Color but no Light module is attached.",
            target=getattr(target, "alias", None) or getattr(kdev, "alias", None),
        )
    try:
        await light.set_hsv(h, s, v)
    except UnsupportedDeviceError as exc:
        raise UnsupportedFeatureError(
            f"HSV rejected: {exc}",
            target=getattr(target, "alias", None),
        ) from exc
    except KasaException as exc:
        raise DeviceError(
            f"HSV command failed: {exc}",
            target=getattr(target, "alias", None),
        ) from exc
