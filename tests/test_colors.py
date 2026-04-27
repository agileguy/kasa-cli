"""Tests for the named-color lookup table (FR-19a, FR-19b)."""

from __future__ import annotations

import pytest

from kasa_cli.colors import (
    NAMED_COLORS,
    SUPPORTED_COLOR_NAMES,
    resolve_color_name,
)
from kasa_cli.errors import EXIT_USAGE_ERROR, UsageError


def test_supported_color_names_is_sorted_and_complete() -> None:
    """SUPPORTED_COLOR_NAMES is a sorted tuple of all 12 documented names."""
    expected = {
        "warm-white",
        "cool-white",
        "daylight",
        "red",
        "orange",
        "yellow",
        "green",
        "cyan",
        "blue",
        "purple",
        "magenta",
        "pink",
    }
    assert set(SUPPORTED_COLOR_NAMES) == expected
    assert len(SUPPORTED_COLOR_NAMES) == 12
    assert list(SUPPORTED_COLOR_NAMES) == sorted(SUPPORTED_COLOR_NAMES)


def test_named_colors_table_keys_match_supported_tuple() -> None:
    assert set(NAMED_COLORS.keys()) == set(SUPPORTED_COLOR_NAMES)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("red", (0, 100, 100)),
        ("orange", (30, 100, 100)),
        ("yellow", (60, 100, 100)),
        ("green", (120, 100, 100)),
        ("cyan", (180, 100, 100)),
        ("blue", (240, 100, 100)),
        ("purple", (270, 100, 100)),
        ("magenta", (300, 100, 100)),
        ("pink", (330, 50, 100)),
        ("warm-white", (0, 0, 100)),
        ("cool-white", (0, 0, 100)),
        ("daylight", (0, 0, 100)),
    ],
)
def test_resolve_color_name_returns_expected_triples(
    name: str, expected: tuple[int, int, int]
) -> None:
    assert resolve_color_name(name) == expected


def test_pink_uses_reduced_saturation() -> None:
    """``pink`` is documented as S=50 to read pink-not-magenta on KL bulbs."""
    h, s, v = resolve_color_name("pink")
    assert h == 330
    assert s == 50
    assert v == 100


@pytest.mark.parametrize(
    "name",
    ["RED", "Red", "rEd", "  red  ", "BLUE", "Warm-White", "WARM-WHITE"],
)
def test_resolve_color_name_is_case_insensitive_and_strips(name: str) -> None:
    """Mixed case + leading/trailing whitespace must still resolve."""
    triple = resolve_color_name(name)
    assert isinstance(triple, tuple)
    assert len(triple) == 3


def test_unknown_name_raises_usage_error_listing_supported() -> None:
    with pytest.raises(UsageError) as info:
        resolve_color_name("chartreuse")
    assert info.value.exit_code == EXIT_USAGE_ERROR
    # The hint must enumerate every supported name so users can self-correct.
    assert info.value.hint is not None
    for n in SUPPORTED_COLOR_NAMES:
        assert n in info.value.hint


def test_empty_string_raises_usage_error() -> None:
    with pytest.raises(UsageError):
        resolve_color_name("")


def test_whites_collapse_to_neutral_hsv() -> None:
    """warm/cool/daylight reduce to (0, 0, 100) — pure HSV cannot distinguish.

    This is a regression guard: if someone changes the mapping to e.g. tweak
    saturation, they need to re-think the SRD's color-temp ambiguity note.
    """
    assert resolve_color_name("warm-white") == (0, 0, 100)
    assert resolve_color_name("cool-white") == (0, 0, 100)
    assert resolve_color_name("daylight") == (0, 0, 100)


def test_named_colors_immutability_contract() -> None:
    """Sanity check: every value is a 3-int tuple with valid HSV ranges."""
    for name, triple in NAMED_COLORS.items():
        assert isinstance(triple, tuple), name
        assert len(triple) == 3, name
        h, s, v = triple
        assert 0 <= h < 360, name
        assert 0 <= s <= 100, name
        assert 0 <= v <= 100, name
