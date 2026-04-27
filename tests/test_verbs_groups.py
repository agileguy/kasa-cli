"""Tests for the ``groups list`` verb (Phase 3 — FR-26..29b).

The verb is intentionally minimal: it reads ``config.groups`` and emits a
list-of-dicts in the requested output mode. We test the projection helper
:func:`collect_groups` directly, and the run path through both the verb
runner and the Click dispatcher.

Phase 1+2 anti-pattern fix: every test asserting an exit code asserts the
EXACT integer (``== 0``), never ``!= 0``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from kasa_cli.cli import main as cli_main
from kasa_cli.config import Config, DeviceEntry
from kasa_cli.errors import EXIT_SUCCESS
from kasa_cli.output import OutputMode
from kasa_cli.verbs.groups_cmd import (
    _group_to_text,
    collect_groups,
    run_groups_list,
)

# --- collect_groups (pure helper) ---------------------------------------------


def test_collect_groups_empty_config_returns_empty_list() -> None:
    """An empty ``[groups]`` section projects to ``[]``."""
    cfg = Config()
    assert collect_groups(cfg) == []


def test_collect_groups_emits_name_members_dicts() -> None:
    cfg = Config(
        devices={
            "alpha": DeviceEntry(alias="alpha", ip="1.1.1.1"),
            "beta": DeviceEntry(alias="beta", ip="1.1.1.2"),
        },
        groups={"bedroom-lights": ["alpha", "beta"]},
    )
    rows = collect_groups(cfg)
    assert rows == [{"name": "bedroom-lights", "members": ["alpha", "beta"]}]


def test_collect_groups_preserves_member_order() -> None:
    cfg = Config(
        devices={
            "z": DeviceEntry(alias="z"),
            "a": DeviceEntry(alias="a"),
            "m": DeviceEntry(alias="m"),
        },
        groups={"mixed": ["z", "a", "m"]},
    )
    rows = collect_groups(cfg)
    assert rows[0]["members"] == ["z", "a", "m"]


def test_collect_groups_returns_independent_lists() -> None:
    """Mutating the projected list MUST NOT mutate the source config."""
    cfg = Config(
        devices={"a": DeviceEntry(alias="a")},
        groups={"g": ["a"]},
    )
    rows = collect_groups(cfg)
    assert isinstance(rows[0]["members"], list)
    rows[0]["members"].append("MUTATION")
    # Re-collect: should still be just ["a"]
    rows2 = collect_groups(cfg)
    assert rows2[0]["members"] == ["a"]


def test_collect_groups_does_not_include_ungrouped_devices() -> None:
    """A device in ``[devices]`` but not referenced by any group is excluded."""
    cfg = Config(
        devices={
            "alpha": DeviceEntry(alias="alpha"),
            "beta": DeviceEntry(alias="beta"),
            "ungrouped": DeviceEntry(alias="ungrouped"),
        },
        groups={"only-some": ["alpha", "beta"]},
    )
    rows = collect_groups(cfg)
    # Only the group itself appears; "ungrouped" is invisible.
    assert len(rows) == 1
    assert "ungrouped" not in rows[0]["members"]


def test_collect_groups_handles_multiple_groups() -> None:
    cfg = Config(
        devices={
            "a": DeviceEntry(alias="a"),
            "b": DeviceEntry(alias="b"),
            "c": DeviceEntry(alias="c"),
        },
        groups={
            "g1": ["a", "b"],
            "g2": ["c"],
            "g3": ["a", "c"],  # alias may belong to multiple groups
        },
    )
    rows = collect_groups(cfg)
    assert len(rows) == 3
    by_name = {r["name"]: r["members"] for r in rows}
    assert by_name["g1"] == ["a", "b"]
    assert by_name["g2"] == ["c"]
    assert by_name["g3"] == ["a", "c"]


# --- _group_to_text (TEXT mode formatter) -------------------------------------


def test_group_to_text_renders_single_line() -> None:
    line = _group_to_text({"name": "g1", "members": ["a", "b"]})
    assert line == "g1: a, b"


def test_group_to_text_handles_empty_members() -> None:
    line = _group_to_text({"name": "empty-group", "members": []})
    assert line == "empty-group:"


def test_group_to_text_handles_non_dict_input_safely() -> None:
    """Defensive: a non-dict input falls back to ``str()`` rather than crashing."""
    line = _group_to_text(["unexpected"])
    # Should not raise; produces *something*.
    assert isinstance(line, str)


# --- run_groups_list (verb runner) --------------------------------------------


@pytest.mark.asyncio
async def test_run_groups_list_jsonl_emits_one_line_per_group(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config(
        devices={"a": DeviceEntry(alias="a"), "b": DeviceEntry(alias="b")},
        groups={"g1": ["a"], "g2": ["b"]},
    )
    code = await run_groups_list(config=cfg, mode=OutputMode.JSONL)
    assert code == EXIT_SUCCESS
    out = capsys.readouterr().out
    lines = [line for line in out.strip().splitlines() if line]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    by_name = {p["name"]: p for p in parsed}
    assert by_name["g1"]["members"] == ["a"]
    assert by_name["g2"]["members"] == ["b"]


@pytest.mark.asyncio
async def test_run_groups_list_json_emits_array(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config(
        devices={"a": DeviceEntry(alias="a"), "b": DeviceEntry(alias="b")},
        groups={"g1": ["a", "b"]},
    )
    code = await run_groups_list(config=cfg, mode=OutputMode.JSON)
    assert code == EXIT_SUCCESS
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed == [{"members": ["a", "b"], "name": "g1"}] or parsed == [
        {"name": "g1", "members": ["a", "b"]}
    ]


@pytest.mark.asyncio
async def test_run_groups_list_empty_returns_zero_with_empty_array(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``[groups]`` table -> exit 0, ``[]`` in JSON, empty stdout in JSONL."""
    cfg = Config()
    code = await run_groups_list(config=cfg, mode=OutputMode.JSON)
    assert code == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert json.loads(out) == []


@pytest.mark.asyncio
async def test_run_groups_list_text_mode_renders_one_line_per_group(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config(
        devices={"a": DeviceEntry(alias="a"), "b": DeviceEntry(alias="b")},
        groups={"g1": ["a"], "g2": ["b"]},
    )
    code = await run_groups_list(config=cfg, mode=OutputMode.TEXT)
    assert code == EXIT_SUCCESS
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert any(line.startswith("g1:") for line in lines)
    assert any(line.startswith("g2:") for line in lines)


@pytest.mark.asyncio
async def test_run_groups_list_quiet_mode_emits_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = Config(
        devices={"a": DeviceEntry(alias="a")},
        groups={"g1": ["a"]},
    )
    code = await run_groups_list(config=cfg, mode=OutputMode.QUIET)
    assert code == EXIT_SUCCESS
    assert capsys.readouterr().out == ""


# --- CLI dispatcher (CliRunner) -----------------------------------------------


def test_cli_groups_list_with_no_config_emits_empty_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config file present -> empty groups -> exit 0, ``[]`` in JSON."""
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "groups", "list"])
    assert result.exit_code == EXIT_SUCCESS, f"stderr: {result.stderr}"
    parsed = json.loads(result.stdout)
    assert parsed == []


def test_cli_groups_list_with_populated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a real config file with [groups] -> emits each group."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[devices.alpha]
ip = "192.168.1.10"

[devices.beta]
ip = "192.168.1.11"

[devices.gamma]
ip = "192.168.1.12"

[groups]
bedroom-lights = ["alpha", "beta"]
patio          = ["gamma"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--config", str(cfg_path), "--json", "groups", "list"])
    assert result.exit_code == EXIT_SUCCESS, f"stderr: {result.stderr}"
    parsed = json.loads(result.stdout)
    assert len(parsed) == 2
    by_name = {p["name"]: p for p in parsed}
    assert by_name["bedroom-lights"]["members"] == ["alpha", "beta"]
    assert by_name["patio"]["members"] == ["gamma"]


def test_cli_groups_help_mentions_no_mutation_in_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-29b: ``groups --help`` should make clear add/remove are NOT in v1."""
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(cli_main, ["groups", "--help"])
    assert result.exit_code == EXIT_SUCCESS
    # ``list`` is the only sub-verb; mutation should be flagged as out of scope.
    assert "list" in result.output
    # The docstring says "by hand-editing" — verify the user is pointed there.
    assert (
        "hand-editing" in result.output or "FR-29b" in result.output or "mutation" in result.output
    )


def test_cli_groups_list_jsonl_one_line_per_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[devices.a]
ip = "1.1.1.1"

[devices.b]
ip = "1.1.1.2"

[groups]
g1 = ["a"]
g2 = ["b"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--config", str(cfg_path), "--jsonl", "groups", "list"])
    assert result.exit_code == EXIT_SUCCESS, f"stderr: {result.stderr}"
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert {p["name"] for p in parsed} == {"g1", "g2"}


def test_cli_groups_list_with_dangling_group_member_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group referencing an undefined alias is a config error (exit 6).

    This is the config layer's contract (FR validation in ``_parse_groups``);
    we just confirm the CLI surfaces it cleanly when reaching ``groups list``.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[devices.alpha]
ip = "192.168.1.10"

[groups]
ghost = ["alpha", "missing-alias"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--config", str(cfg_path), "--json", "groups", "list"])
    # Exit 6 — config error, not a runtime listing error.
    assert result.exit_code == 6, f"stderr: {result.stderr}"
