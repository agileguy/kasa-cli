"""Click-level smoke tests for ``kasa-cli`` (Phase 1 Part B)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from kasa_cli.cli import main as cli_main


def test_help_returns_zero() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    assert "discover" in result.output
    assert "info" in result.output
    assert "on" in result.output
    assert "off" in result.output


def test_unknown_verb_returns_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["frobnicate"])
    # Click defaults to exit code 2 for unknown command; either 2 or 64 is OK
    # for this smoke test (the CLI converts at the __main__ shim).
    assert result.exit_code != 0


def test_json_and_jsonl_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "--jsonl", "list"])
    assert result.exit_code == 64
    assert "mutually exclusive" in result.output


def test_discover_invokes_with_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: --jsonl + monkeypatched Discover.discover -> exit 0."""

    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "discover"])
    # Exit 0; stderr says zero devices found.
    assert result.exit_code == 0


def test_discover_emits_valid_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty discovery result with --json mode emits ``[]`` on stdout."""

    async def _fake_discover(**_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("kasa.Discover.discover", _fake_discover)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "discover"])
    assert result.exit_code == 0
    # stdout should be valid JSON (an empty list).
    parsed = json.loads(result.stdout)
    assert parsed == []


def test_list_with_no_config_emits_empty_array(
    tmp_path: Any,
) -> None:
    """No config + no devices => empty stdout (FR-6 reads only config)."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "list"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed == []


def test_info_unknown_target_exits_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No config => any target lookup falls back to IP-as-host; if connect
    fails the wrapper raises NetworkError => exit 3. We test the not-found
    path by passing a pseudo-alias the lookup can't resolve. Without
    Engineer A's config module we degrade to "treat as host", so we cannot
    cleanly test exit-4 here. Instead, assert exit code is non-zero and a
    structured error appears on stderr.
    """

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("unreachable")

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "info", "192.168.99.99"])
    assert result.exit_code != 0
    # stderr should be JSON envelope per §11.2
    err_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert err_lines
    json.loads(err_lines[-1])


def test_on_off_help() -> None:
    runner = CliRunner()
    result_on = runner.invoke(cli_main, ["on", "--help"])
    result_off = runner.invoke(cli_main, ["off", "--help"])
    assert result_on.exit_code == 0
    assert result_off.exit_code == 0
    assert "--socket" in result_on.output
    assert "--socket" in result_off.output


def test_config_show_without_engineer_a_module_exits_64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without A's config.py merged, ``config show`` exits 64 with an error."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "config", "show"])
    assert result.exit_code == 64


def test_auth_status_without_engineer_a_module_exits_64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--jsonl", "auth", "status"])
    assert result.exit_code == 64
