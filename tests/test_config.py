"""Tests for kasa_cli.config — TOML loader, precedence, validation, show/round-trip."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kasa_cli import config as cfg_mod
from kasa_cli.errors import ConfigError

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


VALID_CONFIG = """
[defaults]
timeout_seconds = 7
concurrency = 4
output_format = "jsonl"

[credentials]
file_path = "~/.config/kasa-cli/creds.alt"

[logging]
file = "/tmp/kasa.log"

[devices.kitchen-lamp]
ip = "192.168.1.42"
mac = "AA:BB:CC:DD:EE:01"

[devices.office-strip]
ip = "192.168.1.51"
mac = "AA:BB:CC:DD:EE:02"

[groups]
bedroom-lights = ["kitchen-lamp", "office-strip"]
"""


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make sure no real ~/.config/kasa-cli or env override leaks in.

    We point ``DEFAULT_CONFIG_PATH`` at an obviously-missing tmp path so the
    "default-only" code path never hits the operator's real home dir.
    """
    monkeypatch.delenv(cfg_mod.ENV_CONFIG_PATH, raising=False)
    sentinel = tmp_path / "absent" / "config.toml"
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", sentinel)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Precedence (FR-40 / FR-40a / FR-40b)
# ---------------------------------------------------------------------------


def test_explicit_path_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--config`` flag beats env var beats default."""
    explicit = _write(tmp_path, "explicit.toml", VALID_CONFIG)
    env_path = _write(tmp_path, "env.toml", "[defaults]\ntimeout_seconds = 999\n")
    monkeypatch.setenv(cfg_mod.ENV_CONFIG_PATH, str(env_path))
    config = cfg_mod.load_config(explicit_path=explicit)
    assert config.source_path == explicit
    assert config.defaults.timeout_seconds == 7


def test_env_var_wins_over_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_path = _write(tmp_path, "env.toml", "[defaults]\ntimeout_seconds = 11\n")
    monkeypatch.setenv(cfg_mod.ENV_CONFIG_PATH, str(env_path))
    config = cfg_mod.load_config()
    assert config.source_path == env_path
    assert config.defaults.timeout_seconds == 11


def test_default_path_used_when_no_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default = _write(tmp_path, "default.toml", "[defaults]\nconcurrency = 25\n")
    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", default)
    config = cfg_mod.load_config()
    assert config.source_path == default
    assert config.defaults.concurrency == 25


def test_built_in_defaults_when_no_file_present(caplog: pytest.LogCaptureFixture) -> None:
    """FR-40b — default-path miss is informational, not an error."""
    caplog.set_level(logging.INFO, logger="kasa_cli")
    config = cfg_mod.load_config()
    assert config.source_path is None
    assert config.defaults.timeout_seconds == cfg_mod.DEFAULT_TIMEOUT_SECONDS
    assert config.defaults.concurrency == cfg_mod.DEFAULT_CONCURRENCY
    assert config.defaults.output_format == cfg_mod.DEFAULT_OUTPUT_FORMAT
    assert any("no config file found" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Strict-mode failure (FR-40a)
# ---------------------------------------------------------------------------


def test_explicit_missing_path_raises_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as excinfo:
        cfg_mod.load_config(explicit_path=missing)
    assert excinfo.value.exit_code == 6
    assert "config file not found" in excinfo.value.message


def test_env_var_missing_path_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(cfg_mod.ENV_CONFIG_PATH, str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError) as excinfo:
        cfg_mod.load_config()
    assert excinfo.value.exit_code == 6


# ---------------------------------------------------------------------------
# Parse / validate failures
# ---------------------------------------------------------------------------


def test_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.toml", "this = is not = valid = toml = ===")
    with pytest.raises(ConfigError) as excinfo:
        cfg_mod.load_config(explicit_path=bad)
    assert excinfo.value.exit_code == 6
    assert "malformed TOML" in excinfo.value.message


def test_unknown_top_level_table_raises(tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.toml", "[mystery]\nfoo = 1\n")
    with pytest.raises(ConfigError, match="unknown top-level table"):
        cfg_mod.load_config(explicit_path=bad)


def test_unknown_defaults_key_raises(tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.toml", "[defaults]\nbogus = 1\n")
    with pytest.raises(ConfigError, match=r"unknown keys in \[defaults\]"):
        cfg_mod.load_config(explicit_path=bad)


def test_invalid_output_format_raises(tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.toml", '[defaults]\noutput_format = "xml"\n')
    with pytest.raises(ConfigError, match="output_format"):
        cfg_mod.load_config(explicit_path=bad)


def test_negative_timeout_raises(tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.toml", "[defaults]\ntimeout_seconds = -1\n")
    with pytest.raises(ConfigError, match="timeout_seconds"):
        cfg_mod.load_config(explicit_path=bad)


def test_dangling_group_alias_raises(tmp_path: Path) -> None:
    bad = _write(
        tmp_path,
        "bad.toml",
        """
[devices.lamp]
ip = "1.2.3.4"

[groups]
ghosts = ["lamp", "missing-alias"]
""",
    )
    with pytest.raises(ConfigError, match="unknown alias"):
        cfg_mod.load_config(explicit_path=bad)


def test_unknown_device_key_raises(tmp_path: Path) -> None:
    bad = _write(tmp_path, "bad.toml", '[devices.lamp]\nip = "1.2.3.4"\nbogus = "x"\n')
    with pytest.raises(ConfigError, match=r"unknown keys in \[devices\.lamp\]"):
        cfg_mod.load_config(explicit_path=bad)


# ---------------------------------------------------------------------------
# validate_config (FR-40c)
# ---------------------------------------------------------------------------


def test_validate_config_happy_path(tmp_path: Path) -> None:
    p = _write(tmp_path, "ok.toml", VALID_CONFIG)
    cfg_mod.validate_config(p)  # must not raise


def test_validate_config_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        cfg_mod.validate_config(tmp_path / "nope.toml")


def test_validate_config_bad_toml_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "bad.toml", "[invalid")
    with pytest.raises(ConfigError):
        cfg_mod.validate_config(p)


# ---------------------------------------------------------------------------
# effective_toml round-trip
# ---------------------------------------------------------------------------


def test_effective_toml_round_trips_through_load(tmp_path: Path) -> None:
    """``load → effective_toml → load`` produces an equal Config."""
    p = _write(tmp_path, "ok.toml", VALID_CONFIG)
    first = cfg_mod.load_config(explicit_path=p)
    rendered = cfg_mod.effective_toml(first)
    rendered_path = _write(tmp_path, "round.toml", rendered)
    second = cfg_mod.load_config(explicit_path=rendered_path)
    # source_path differs by design — compare everything else.
    assert first.defaults == second.defaults
    assert first.credentials == second.credentials
    assert first.logging == second.logging
    assert first.devices == second.devices
    assert first.groups == second.groups


def test_effective_toml_for_built_in_defaults() -> None:
    """Defaults-only Config still renders to a valid loadable TOML body."""
    rendered = cfg_mod.effective_toml(cfg_mod.Config())
    assert "[defaults]" in rendered
    assert "[credentials]" in rendered
    assert "[logging]" in rendered
    # Logging file is a comment when None — still valid TOML.
    assert "# file" in rendered
