"""``kasa-cli set`` (SRD §5.5, FR-16..20).

Single sub-verb that adjusts brightness, color-temperature, or color on a
target device. The flag groups are partially mutually-exclusive:

* ``--hsv``, ``--hex``, ``--color`` are MUTUALLY EXCLUSIVE (FR-20). At most
  one may be supplied.
* ``--brightness`` and ``--color-temp`` may be supplied alone OR alongside a
  color flag (e.g. ``--color-temp 2700 --brightness 50``).
* At least ONE flag must be supplied; ``set`` with no settings is a usage
  error (exit 64).

On a device that does not advertise the relevant capability, the wrapper
raises :class:`UnsupportedFeatureError` (exit 5) per FR-20. Multi-socket
strips re-use the same allow-list as ``on``/``off`` — for the v1 model set
``--socket <n>`` is required, with ``--socket all`` fanning the same set
operation across every child.

Hex parsing
-----------

``--hex`` accepts ``#rrggbb``, ``rrggbb``, or shorthand ``#rgb``/``rgb``.
The conversion to HSV uses the standard formula (max-min over max for
saturation, 60-degree sectors for hue, max for value), all integer-clamped
to the SRD's 0..360 / 0..100 / 0..100 ranges.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from kasa_cli import wrapper
from kasa_cli.colors import resolve_color_name
from kasa_cli.errors import EXIT_SUCCESS, UsageError
from kasa_cli.output import OutputMode
from kasa_cli.verbs.onoff import (
    _format_sockets_for_error,
    _is_multi_socket,
    _list_sockets,
)
from kasa_cli.wrapper import CredentialBundle

__all__ = ["parse_hex_color", "parse_hsv_triple", "run_set"]


# ---------------------------------------------------------------------------
# Parsing helpers — pure, no I/O.
# ---------------------------------------------------------------------------


def parse_hsv_triple(value: str) -> tuple[int, int, int]:
    """Parse ``"H,S,V"`` into a clamped ``(h, s, v)`` triple.

    Raises :class:`UsageError` (exit 64) on malformed input.
    """
    if not isinstance(value, str) or not value:
        raise UsageError("--hsv requires a 'H,S,V' triple of integers.")
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 3:
        raise UsageError(
            f"--hsv expects 3 comma-separated integers; got {value!r}",
            hint="Example: --hsv 240,100,100  (hue, saturation, value)",
        )
    try:
        h, s, v = (int(p) for p in parts)
    except ValueError as exc:
        raise UsageError(
            f"--hsv components must be integers; got {value!r}",
        ) from exc
    if not (0 <= h < 360):
        raise UsageError(f"--hsv hue must be in [0, 360); got {h}")
    if not (0 <= s <= 100):
        raise UsageError(f"--hsv saturation must be in [0, 100]; got {s}")
    if not (0 <= v <= 100):
        raise UsageError(f"--hsv value must be in [0, 100]; got {v}")
    return h, s, v


def parse_hex_color(value: str) -> tuple[int, int, int]:
    """Parse an RGB hex color into an HSV triple ``(h, s, v)`` integers.

    Accepts ``#rrggbb``, ``rrggbb``, ``#rgb``, ``rgb``. Raises
    :class:`UsageError` on malformed input.
    """
    if not isinstance(value, str) or not value:
        raise UsageError("--hex requires a hex color (e.g. #ff8800).")
    raw = value.strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) == 3:
        # Expand shorthand: "f80" -> "ff8800"
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        raise UsageError(
            f"--hex must be #rrggbb (or #rgb shorthand); got {value!r}",
            hint="Example: --hex #00ff00  or  --hex #0f0",
        )
    try:
        r = int(raw[0:2], 16)
        g = int(raw[2:4], 16)
        b = int(raw[4:6], 16)
    except ValueError as exc:  # pragma: no cover - regex-equivalent guarded above
        raise UsageError(f"--hex parse failure: {value!r}") from exc

    return _rgb_to_hsv(r, g, b)


def _rgb_to_hsv(r: int, g: int, b: int) -> tuple[int, int, int]:
    """Convert 0..255 RGB to integer HSV (0..359, 0..100, 0..100)."""
    rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
    cmax = max(rf, gf, bf)
    cmin = min(rf, gf, bf)
    delta = cmax - cmin

    if delta == 0:
        h_deg = 0.0
    elif cmax == rf:
        h_deg = 60.0 * (((gf - bf) / delta) % 6.0)
    elif cmax == gf:
        h_deg = 60.0 * (((bf - rf) / delta) + 2.0)
    else:  # cmax == bf
        h_deg = 60.0 * (((rf - gf) / delta) + 4.0)

    s_pct = 0.0 if cmax == 0 else (delta / cmax) * 100.0
    v_pct = cmax * 100.0

    h = round(h_deg) % 360
    s = max(0, min(100, round(s_pct)))
    v = max(0, min(100, round(v_pct)))
    return h, s, v


# ---------------------------------------------------------------------------
# Verb runner
# ---------------------------------------------------------------------------


def _resolve_socket(target: str, kdev: object, socket_arg: str | None) -> int | None:
    """Resolve a ``--socket`` string into the int the wrapper helpers expect.

    Returns ``None`` to mean "device-level (single socket or non-strip)".
    Raises :class:`UsageError` (exit 64) on malformed/illegal values per FR-15.

    Note: ``--socket all`` returns a sentinel (-1) ONLY for multi-socket
    strips; the caller fans out across children itself.
    """
    model = str(getattr(kdev, "model", "") or "")
    multi = _is_multi_socket(model)
    children = list(getattr(kdev, "children", None) or [])

    if multi:
        if socket_arg is None:
            sockets = _list_sockets(kdev)  # type: ignore[arg-type]
            raise UsageError(
                (
                    f"Target {target!r} is a multi-socket strip ({model}); "
                    "pass --socket <n> (1-indexed) or --socket all. "
                    f"Available sockets: {_format_sockets_for_error(sockets)}"
                ),
                target=target,
                hint=f"Try: kasa-cli set {target} --socket 1 --brightness 50",
            )
        if socket_arg.lower() == "all":
            return -1  # sentinel: caller fans out
        try:
            idx = int(socket_arg)
        except ValueError as exc:
            raise UsageError(
                f"--socket value must be a positive integer or 'all'; got {socket_arg!r}",
                target=target,
            ) from exc
        if idx < 1 or idx > len(children):
            sockets = _list_sockets(kdev)  # type: ignore[arg-type]
            raise UsageError(
                (
                    f"--socket {idx} out of range for {target!r}; "
                    f"available: {_format_sockets_for_error(sockets)}"
                ),
                target=target,
            )
        return idx

    # Single-socket device.
    if socket_arg is None:
        return None
    if socket_arg.lower() == "all":
        return None  # equivalent to "the device" on a single-socket target
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
    return None


async def _apply(
    kdev: object,
    socket: int | None,
    *,
    brightness: int | None,
    color_temp: int | None,
    hsv: tuple[int, int, int] | None,
) -> None:
    """Apply each requested setting to one device or socket."""
    # Order: brightness first, then color-temp OR hsv (the verb layer
    # rejected the conflicting combo upstream). Brightness is independent and
    # may combine with either of the others.
    if brightness is not None:
        await wrapper.set_brightness(kdev, brightness, socket=socket)  # type: ignore[arg-type]
    if color_temp is not None:
        await wrapper.set_color_temp(kdev, color_temp, socket=socket)  # type: ignore[arg-type]
    if hsv is not None:
        await wrapper.set_hsv(kdev, *hsv, socket=socket)  # type: ignore[arg-type]


async def run_set(
    target: str,
    *,
    brightness: int | None,
    color_temp: int | None,
    hsv: str | None,
    hex_color: str | None,
    color_name: str | None,
    socket_arg: str | None,
    config_lookup: Callable[[str], tuple[str | None, str | None]],
    credentials: CredentialBundle,
    timeout: float,
    mode: OutputMode,
) -> int:
    """Execute the set verb. Returns the exit code on success."""
    del mode  # set is silent on success.

    # --- Mutual-exclusion + at-least-one validation (FR-20) ----------------
    color_flags_count = sum(x is not None for x in (hsv, hex_color, color_name))
    if color_flags_count > 1:
        raise UsageError(
            "--hsv, --hex, and --color are mutually exclusive; pass at most one.",
            target=target,
        )
    if (
        brightness is None
        and color_temp is None
        and hsv is None
        and hex_color is None
        and color_name is None
    ):
        raise UsageError(
            "set requires at least one of --brightness, --color-temp, --hsv, --hex, --color.",
            target=target,
            hint="Example: kasa-cli set kitchen-bulb --brightness 50",
        )

    # --- Parse color inputs into a single HSV triple, if any --------------
    hsv_triple: tuple[int, int, int] | None = None
    if hsv is not None:
        hsv_triple = parse_hsv_triple(hsv)
    elif hex_color is not None:
        hsv_triple = parse_hex_color(hex_color)
    elif color_name is not None:
        hsv_triple = resolve_color_name(color_name)

    # --- Connect + dispatch ------------------------------------------------
    kdev = await wrapper.resolve_target(
        target,
        config_lookup=config_lookup,
        credentials=credentials,
        timeout=timeout,
    )
    try:
        # Refresh so children + capability detection reflect live state.
        try:
            await kdev.update()
        except Exception as exc:
            from kasa_cli.errors import DeviceError

            raise DeviceError(
                f"Failed to refresh state for {target!r}: {exc}",
                target=target,
            ) from exc

        socket_index = _resolve_socket(target, kdev, socket_arg)

        if socket_index == -1:
            # --socket all on a multi-socket strip: fan out across children.
            children = list(getattr(kdev, "children", None) or [])
            for i, child in enumerate(children, start=1):
                # We pass the child directly with socket=None — the wrapper's
                # _select_target sees no children and operates on the child.
                await _apply(
                    child,
                    None,
                    brightness=brightness,
                    color_temp=color_temp,
                    hsv=hsv_triple,
                )
                del i  # silence unused
            return EXIT_SUCCESS

        await _apply(
            kdev,
            socket_index,
            brightness=brightness,
            color_temp=color_temp,
            hsv=hsv_triple,
        )
        return EXIT_SUCCESS
    finally:
        with contextlib.suppress(Exception):
            await kdev.disconnect()
