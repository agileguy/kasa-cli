"""Built-in named-color lookup table (FR-19a, FR-19b).

Phase 2 ships a compiled-in table mapping a small set of human-friendly color
names to ``(hue, saturation, value)`` integer triples. The ``set --color
<name>`` verb resolves the name through this module and then dispatches to
``wrapper.set_hsv`` like any other ``--hsv``/``--hex`` invocation.

Per FR-19b the table is intentionally **defined in code, not config** in v1
so behavior is identical across machines. User-defined color aliases are
deferred to a future phase.

Color-temperature limitation
----------------------------

``warm-white``, ``cool-white``, and ``daylight`` are nominally
*color-temperature* targets (≈2700K / ≈5000K / ≈6500K), not HSV points. Pure
HSV cannot distinguish them: all three reduce to ``(0, 0, 100)`` (achromatic
full-brightness white). When a user really wants a tunable-white target on a
color-temp-capable device, they should pass ``--color-temp <kelvin>`` rather
than ``--color warm-white``. The named whites in this table are accepted for
ergonomic reasons (so ``--color`` covers the documented name set) and produce
neutral white on a color bulb. The ``set`` verb does NOT silently re-route
``--color warm-white`` onto the color-temp path; that ambiguity stays out
until a future "named color-temp" feature is in scope.

The 9 chromatic entries (``red`` through ``pink``) are full-saturation HSV
points except ``pink``, which uses ``S=50`` to look pink-not-magenta on a
typical KL-series bulb. ``magenta`` (``H=300, S=100``) is the saturated
counterpart and is exposed alongside.
"""

from __future__ import annotations

from kasa_cli.errors import UsageError

# ---------------------------------------------------------------------------
# Lookup table — sorted alphabetically for stable iteration order in tests
# and `--help` output. All keys lower-case, hyphenated.
# ---------------------------------------------------------------------------

NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    # Achromatic whites — all reduce to neutral white in HSV. See module
    # docstring for the color-temp limitation.
    "warm-white": (0, 0, 100),
    "cool-white": (0, 0, 100),
    "daylight": (0, 0, 100),
    # Chromatic primaries / secondaries / tertiaries.
    "red": (0, 100, 100),
    "orange": (30, 100, 100),
    "yellow": (60, 100, 100),
    "green": (120, 100, 100),
    "cyan": (180, 100, 100),
    "blue": (240, 100, 100),
    "purple": (270, 100, 100),
    "magenta": (300, 100, 100),
    # Pink uses reduced saturation so it reads pink, not light magenta.
    "pink": (330, 50, 100),
}


SUPPORTED_COLOR_NAMES: tuple[str, ...] = tuple(sorted(NAMED_COLORS.keys()))


def resolve_color_name(name: str) -> tuple[int, int, int]:
    """Resolve a case-insensitive color name to its ``(h, s, v)`` triple.

    Raises :class:`kasa_cli.errors.UsageError` (exit 64) on miss, with a
    hint listing every supported name.
    """
    if not isinstance(name, str) or not name:
        raise UsageError(
            "Color name must be a non-empty string.",
            hint=f"Supported colors: {', '.join(SUPPORTED_COLOR_NAMES)}",
        )
    key = name.strip().lower()
    triple = NAMED_COLORS.get(key)
    if triple is None:
        raise UsageError(
            f"Unknown color name: {name!r}",
            hint=f"Supported colors: {', '.join(SUPPORTED_COLOR_NAMES)}",
        )
    return triple


__all__ = [
    "NAMED_COLORS",
    "SUPPORTED_COLOR_NAMES",
    "resolve_color_name",
]
