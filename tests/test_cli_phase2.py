"""Click-runner smoke tests for Phase 2 verbs (toggle, set)."""

from __future__ import annotations

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
    """Mutually-exclusive color flags rejected at the Click layer."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--hsv", "1,2,3", "--hex", "#fff"])
    # Click's UsageError exits 2 by default; this is acceptable as long as
    # it's clearly a user-error exit code (not 0). The runner verb's own
    # UsageError would exit 64 — both are valid user-error signals.
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_set_color_and_hsv_together_is_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--color", "blue", "--hsv", "1,2,3"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_set_color_and_hex_together_is_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--color", "blue", "--hex", "#fff"])
    assert result.exit_code != 0


def test_set_brightness_out_of_click_range_rejected() -> None:
    """Click's IntRange enforces 0..100 for --brightness."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--brightness", "101"])
    assert result.exit_code != 0


def test_set_brightness_negative_rejected() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["set", "foo", "--brightness", "-1"])
    assert result.exit_code != 0
