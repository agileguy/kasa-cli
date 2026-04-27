"""Click-runner smoke tests for Phase 2 verbs (toggle, set, energy, schedule)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from kasa_cli.cli import main as cli_main

# --- toggle -----------------------------------------------------------------


def test_toggle_help_lists_socket_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["toggle", "--help"])
    assert result.exit_code == 0
    assert "toggle" in result.output.lower()
    assert "--socket" in result.output


def test_toggle_appears_in_top_level_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    assert "toggle" in result.output


# --- set --------------------------------------------------------------------


def test_set_help_lists_all_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "--help"])
    assert result.exit_code == 0
    out = result.output
    for flag in ("--brightness", "--color-temp", "--hsv", "--hex", "--color", "--socket"):
        assert flag in out, f"missing {flag} in set --help"


def test_set_appears_in_top_level_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    assert "set" in result.output


def test_set_with_no_flags_is_usage_error() -> None:
    """``kasa-cli set foo`` with no settings flag must exit 64."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo"])
    assert result.exit_code == 64


def test_set_hsv_and_hex_together_is_usage_error() -> None:
    """FR-20: mutually-exclusive color flags exit 64 (not Click's default 2)."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--hsv", "1,2,3", "--hex", "#fff"])
    assert result.exit_code == 64
    assert "mutually exclusive" in result.output.lower()


def test_set_color_and_hsv_together_is_usage_error() -> None:
    """FR-20: --color + --hsv exits 64."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--color", "blue", "--hsv", "1,2,3"])
    assert result.exit_code == 64
    assert "mutually exclusive" in result.output.lower()


def test_set_color_and_hex_together_is_usage_error() -> None:
    """FR-20: --color + --hex exits 64."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--color", "blue", "--hex", "#fff"])
    assert result.exit_code == 64


def test_set_brightness_out_of_click_range_rejected() -> None:
    """FR-20: --brightness > 100 exits 64 (was 2 under IntRange)."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--brightness", "101"])
    assert result.exit_code == 64


def test_set_brightness_negative_rejected() -> None:
    """FR-20: --brightness < 0 exits 64 (was 2 under IntRange)."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--brightness", "-1"])
    assert result.exit_code == 64


# --- per-verb structured-error stderr envelope (exit 5) ----------------------
#
# These tests close the test-shape gap that hid C1: every Phase 2 verb gets
# at least one CliRunner-driven case that hits an UnsupportedFeatureError
# path and asserts BOTH ``exit_code == 5`` AND a parseable JSON envelope on
# stderr with ``error == "unsupported_feature"``. If a verb regressed back
# to a non-structured exit, this would catch it.


def _parse_last_json_line(text: str) -> dict[str, Any]:
    """Return the last non-blank line of ``text`` parsed as JSON."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "expected at least one stderr line"
    return json.loads(lines[-1])


@pytest.fixture
def _stub_unsupported_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``wrapper.resolve_target`` raise UnsupportedFeatureError.

    Used to drive every Phase 2 verb to its exit-5 structured-error path
    without touching real devices.
    """
    from kasa_cli.errors import UnsupportedFeatureError

    async def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise UnsupportedFeatureError(
            "synthetic unsupported-feature path for test",
            target="some-target",
        )

    monkeypatch.setattr("kasa_cli.wrapper.resolve_target", _raise)


def test_toggle_unsupported_emits_structured_stderr(
    _stub_unsupported_resolver: None,
) -> None:
    """``toggle`` exits 5 with a structured ``unsupported_feature`` envelope."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "toggle", "some-target"])
    assert result.exit_code == 5, (result.exit_code, result.stderr)
    payload = _parse_last_json_line(result.stderr)
    assert payload["error"] == "unsupported_feature"
    assert payload["exit_code"] == 5


def test_set_unsupported_emits_structured_stderr(
    _stub_unsupported_resolver: None,
) -> None:
    """``set`` exits 5 with a structured ``unsupported_feature`` envelope."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "set", "some-target", "--brightness", "50"])
    assert result.exit_code == 5, (result.exit_code, result.stderr)
    payload = _parse_last_json_line(result.stderr)
    assert payload["error"] == "unsupported_feature"
    assert payload["exit_code"] == 5


def test_energy_unsupported_emits_structured_stderr(
    _stub_unsupported_resolver: None,
) -> None:
    """``energy`` exits 5 with a structured ``unsupported_feature`` envelope."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "energy", "some-target"])
    assert result.exit_code == 5, (result.exit_code, result.stderr)
    payload = _parse_last_json_line(result.stderr)
    assert payload["error"] == "unsupported_feature"
    assert payload["exit_code"] == 5


def test_schedule_list_unsupported_emits_structured_stderr(
    _stub_unsupported_resolver: None,
) -> None:
    """``schedule list`` exits 5 with a structured envelope."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "schedule", "list", "some-target"])
    assert result.exit_code == 5, (result.exit_code, result.stderr)
    payload = _parse_last_json_line(result.stderr)
    assert payload["error"] == "unsupported_feature"
    assert payload["exit_code"] == 5
