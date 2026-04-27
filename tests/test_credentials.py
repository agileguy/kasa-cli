"""Tests for kasa_cli.credentials — resolver, file format, permissions."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from kasa_cli import credentials as creds_mod
from kasa_cli.config import Config, CredentialsConfig, DeviceEntry
from kasa_cli.errors import AuthError, ConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_creds_file(
    path: Path,
    *,
    payload: dict[str, object] | str,
    mode: int = 0o600,
) -> Path:
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _config_with_default_file(path: Path) -> Config:
    return Config(credentials=CredentialsConfig(file_path=str(path)))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip both env-var creds so tests that exercise the file-only path don't bleed."""
    monkeypatch.delenv(creds_mod.ENV_USERNAME, raising=False)
    monkeypatch.delenv(creds_mod.ENV_PASSWORD, raising=False)


@pytest.fixture(autouse=True)
def _reset_deprecation_latch() -> None:
    """Clear the once-per-process deprecation warning state between tests."""
    creds_mod._reset_deprecation_state_for_tests()


# ---------------------------------------------------------------------------
# Happy path — version present
# ---------------------------------------------------------------------------


def test_default_file_with_version_one_resolves(tmp_path: Path) -> None:
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={"version": 1, "username": "user@x", "password": "secret"},
    )
    config = _config_with_default_file(creds_path)
    out = creds_mod.resolve_credentials(config)
    assert out is not None
    assert out.username == "user@x"
    assert out.password == "secret"
    assert out.source == str(creds_path)


# ---------------------------------------------------------------------------
# Missing version field — single deprecation warning, treat as v1
# ---------------------------------------------------------------------------


def test_missing_version_field_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={"username": "u", "password": "p"},
    )
    config = _config_with_default_file(creds_path)
    caplog.set_level(logging.WARNING, logger="kasa_cli")
    out1 = creds_mod.resolve_credentials(config)
    out2 = creds_mod.resolve_credentials(config)
    assert out1 is not None
    assert out2 is not None
    deprecation_lines = [
        rec for rec in caplog.records if "lacks a 'version' field" in rec.getMessage()
    ]
    assert len(deprecation_lines) == 1


# ---------------------------------------------------------------------------
# Wrong version → exit 6
# ---------------------------------------------------------------------------


def test_unsupported_version_raises_config_error(tmp_path: Path) -> None:
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={"version": 999, "username": "u", "password": "p"},
    )
    config = _config_with_default_file(creds_path)
    with pytest.raises(ConfigError) as excinfo:
        creds_mod.resolve_credentials(config)
    assert excinfo.value.exit_code == 6
    assert "unsupported credentials file version" in excinfo.value.message


def test_non_int_version_raises_config_error(tmp_path: Path) -> None:
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={"version": "one", "username": "u", "password": "p"},
    )
    config = _config_with_default_file(creds_path)
    with pytest.raises(ConfigError, match="'version' must be an int"):
        creds_mod.resolve_credentials(config)


# ---------------------------------------------------------------------------
# Unknown extra keys → exit 6
# ---------------------------------------------------------------------------


def test_unknown_keys_raise_config_error(tmp_path: Path) -> None:
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={
            "version": 1,
            "username": "u",
            "password": "p",
            "stray_key": "x",
        },
    )
    config = _config_with_default_file(creds_path)
    with pytest.raises(ConfigError) as excinfo:
        creds_mod.resolve_credentials(config)
    assert excinfo.value.exit_code == 6
    assert "stray_key" in excinfo.value.message


# ---------------------------------------------------------------------------
# Permission rejection (FR-CRED-2 → exit 2)
# ---------------------------------------------------------------------------


def test_permissive_mode_rejected(tmp_path: Path) -> None:
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={"version": 1, "username": "u", "password": "p"},
        mode=0o644,
    )
    config = _config_with_default_file(creds_path)
    with pytest.raises(AuthError) as excinfo:
        creds_mod.resolve_credentials(config)
    assert excinfo.value.exit_code == 2
    assert "0o644" in excinfo.value.message
    assert excinfo.value.hint is not None
    assert "chmod 600" in excinfo.value.hint


# ---------------------------------------------------------------------------
# Env-var fallback
# ---------------------------------------------------------------------------


def test_env_vars_used_when_default_file_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(creds_mod.ENV_USERNAME, "envuser")
    monkeypatch.setenv(creds_mod.ENV_PASSWORD, "envpass")
    config = _config_with_default_file(tmp_path / "absent.json")
    out = creds_mod.resolve_credentials(config)
    assert out is not None
    assert out.username == "envuser"
    assert out.password == "envpass"
    assert out.source == "env"


def test_partial_env_vars_skip_to_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Setting only KASA_USERNAME without KASA_PASSWORD does NOT count as a hit."""
    monkeypatch.setenv(creds_mod.ENV_USERNAME, "envuser")
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={"version": 1, "username": "u", "password": "p"},
    )
    config = _config_with_default_file(creds_path)
    out = creds_mod.resolve_credentials(config)
    assert out is not None
    assert out.username == "u"  # came from file, not env


# ---------------------------------------------------------------------------
# Per-device override beats env beats default file
# ---------------------------------------------------------------------------


def test_per_device_override_takes_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(creds_mod.ENV_USERNAME, "envuser")
    monkeypatch.setenv(creds_mod.ENV_PASSWORD, "envpass")

    default_path = _write_creds_file(
        tmp_path / "default.json",
        payload={"version": 1, "username": "default-user", "password": "x"},
    )
    override_path = _write_creds_file(
        tmp_path / "guest.json",
        payload={"version": 1, "username": "guest@x", "password": "guestpw"},
    )
    config = Config(
        credentials=CredentialsConfig(file_path=str(default_path)),
        devices={
            "guest-device": DeviceEntry(alias="guest-device", credential_file=str(override_path))
        },
    )
    out = creds_mod.resolve_credentials(config, alias="guest-device")
    assert out is not None
    assert out.username == "guest@x"
    assert out.source == "per-device:guest-device"


def test_missing_per_device_override_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-device file pointed at a missing path still uses env vars below."""
    monkeypatch.setenv(creds_mod.ENV_USERNAME, "envuser")
    monkeypatch.setenv(creds_mod.ENV_PASSWORD, "envpass")
    config = Config(
        credentials=CredentialsConfig(file_path=str(tmp_path / "absent.json")),
        devices={
            "guest-device": DeviceEntry(
                alias="guest-device",
                credential_file=str(tmp_path / "missing-override.json"),
            )
        },
    )
    out = creds_mod.resolve_credentials(config, alias="guest-device")
    assert out is not None
    assert out.source == "env"


# ---------------------------------------------------------------------------
# No-creds → None (FR-CRED-3)
# ---------------------------------------------------------------------------


def test_no_credentials_returns_none(tmp_path: Path) -> None:
    config = _config_with_default_file(tmp_path / "absent.json")
    assert creds_mod.resolve_credentials(config) is None


# ---------------------------------------------------------------------------
# Pathological JSON
# ---------------------------------------------------------------------------


def test_credentials_file_invalid_json_raises(tmp_path: Path) -> None:
    creds_path = _write_creds_file(tmp_path / "creds.json", payload="{not json")
    config = _config_with_default_file(creds_path)
    with pytest.raises(ConfigError, match="not valid JSON"):
        creds_mod.resolve_credentials(config)


def test_credentials_file_array_root_raises(tmp_path: Path) -> None:
    creds_path = _write_creds_file(tmp_path / "creds.json", payload="[1,2,3]")
    config = _config_with_default_file(creds_path)
    with pytest.raises(ConfigError, match="must be a JSON object"):
        creds_mod.resolve_credentials(config)


def test_credentials_file_empty_username_raises(tmp_path: Path) -> None:
    creds_path = _write_creds_file(
        tmp_path / "creds.json",
        payload={"version": 1, "username": "", "password": "p"},
    )
    config = _config_with_default_file(creds_path)
    with pytest.raises(ConfigError, match="username"):
        creds_mod.resolve_credentials(config)
