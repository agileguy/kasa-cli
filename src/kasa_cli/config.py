"""TOML configuration loader for kasa-cli (SRD §9).

Resolution order (FR-40 / FR-40a / FR-40b):

1. ``--config <path>`` flag (passed through ``explicit_path``)
2. ``KASA_CLI_CONFIG`` environment variable
3. ``~/.config/kasa-cli/config.toml`` (default)
4. Built-in defaults (no file present)

Strict-mode rule: when (1) or (2) selects a path, the path MUST exist and be
readable. Silent fallback is forbidden — exit code 6. When the *default* path
is absent, the CLI logs an INFO line on stderr and proceeds with built-ins.

This module never *writes* user config files. v1 group mutation is out of
scope (FR-29b); ``config show`` produces TOML for inspection only.
"""

from __future__ import annotations

import io
import logging
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from kasa_cli.errors import ConfigError

logger = logging.getLogger("kasa_cli")


# ---------------------------------------------------------------------------
# Built-in defaults (SRD §9.2)
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS: Final[int] = 5
DEFAULT_CONCURRENCY: Final[int] = 10
DEFAULT_OUTPUT_FORMAT: Final[str] = "auto"
DEFAULT_CREDENTIALS_FILE: Final[str] = "~/.config/kasa-cli/credentials"

VALID_OUTPUT_FORMATS: Final[frozenset[str]] = frozenset({"auto", "text", "json", "jsonl"})

ENV_CONFIG_PATH: Final[str] = "KASA_CLI_CONFIG"

# Computed lazily at load time (not at module import) so tests can redirect
# ``$HOME`` via ``monkeypatch.setattr(Path, "home", ...)`` without patching
# this constant separately. See _default_config_path() below.
DEFAULT_CONFIG_PATH: Final[Path] = Path("~/.config/kasa-cli/config.toml").expanduser()


def _default_config_path() -> Path:
    """Return the default config-file path, resolved at call time.

    Honors $HOME / Path.home() at the moment of the call. Tests that
    redirect HOME / Path.home() rely on this not being cached at import.
    """
    return Path("~/.config/kasa-cli/config.toml").expanduser()


# ---------------------------------------------------------------------------
# Schema dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DefaultsConfig:
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY
    output_format: str = DEFAULT_OUTPUT_FORMAT


@dataclass(slots=True)
class CredentialsConfig:
    file_path: str = DEFAULT_CREDENTIALS_FILE


@dataclass(slots=True)
class LoggingConfig:
    file: str | None = None


@dataclass(slots=True)
class DeviceEntry:
    alias: str
    ip: str | None = None
    mac: str | None = None
    credential_file: str | None = None


@dataclass(slots=True)
class Config:
    """Effective configuration after precedence resolution (SRD §9.2)."""

    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    devices: dict[str, DeviceEntry] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)
    source_path: Path | None = None
    """Path the config was loaded from; ``None`` when built-in defaults only."""


# Allowed top-level table names — anything else is a config-error (exit 6).
_ALLOWED_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    {"defaults", "credentials", "logging", "devices", "groups"}
)
_ALLOWED_DEFAULTS_KEYS: Final[frozenset[str]] = frozenset(
    {"timeout_seconds", "concurrency", "output_format"}
)
_ALLOWED_CREDENTIALS_KEYS: Final[frozenset[str]] = frozenset({"file_path"})
_ALLOWED_LOGGING_KEYS: Final[frozenset[str]] = frozenset({"file"})
_ALLOWED_DEVICE_KEYS: Final[frozenset[str]] = frozenset({"ip", "mac", "credential_file"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(explicit_path: Path | None = None) -> Config:
    """Resolve and load the effective config.

    Args:
        explicit_path: If non-``None``, treats this as the ``--config`` flag —
            the file MUST exist or :class:`ConfigError` is raised (FR-40a).

    Returns:
        Config: Fully populated config; ``source_path`` is ``None`` only when
        built-in defaults are used.

    Raises:
        ConfigError: explicit path missing, malformed TOML, unknown keys,
            unresolvable group→alias references.
    """
    chosen, strict = _resolve_path(explicit_path)
    if chosen is None:
        logger.info("no config file found, using defaults")
        return Config()

    if not chosen.exists():
        if strict:
            raise ConfigError(
                f"config file not found: {chosen}",
                hint=(
                    "Pass an existing path to --config, unset KASA_CLI_CONFIG, or create the file."
                ),
                extra={"path": str(chosen)},
            )
        # Default-path miss is informational, not an error.
        logger.info("no config file found, using defaults")
        return Config()

    try:
        raw = chosen.read_bytes()
    except OSError as exc:
        raise ConfigError(
            f"cannot read config file: {chosen} ({exc})",
            extra={"path": str(chosen)},
        ) from exc

    config = _parse_and_validate(raw, chosen)
    return config


def validate_config(path: Path) -> None:
    """Validate a candidate config file.

    Used by ``kasa-cli config validate``. Raises :class:`ConfigError` on any
    failure; returns ``None`` on success. Does NOT install the file as the
    active config.
    """
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}",
            extra={"path": str(path)},
        )
    raw = path.read_bytes()
    _parse_and_validate(raw, path)


def effective_toml(config: Config) -> str:
    """Render a Config as TOML for ``kasa-cli config show``.

    Standard library has no TOML *writer*; we hand-render. The output is
    canonical (sections in fixed order, tables alphabetized within sections,
    trailing newline) so tests can assert determinism.
    """
    buf = io.StringIO()

    # [defaults]
    buf.write("[defaults]\n")
    buf.write(f"timeout_seconds = {config.defaults.timeout_seconds}\n")
    buf.write(f"concurrency = {config.defaults.concurrency}\n")
    buf.write(f'output_format = "{config.defaults.output_format}"\n')
    buf.write("\n")

    # [credentials]
    buf.write("[credentials]\n")
    buf.write(f'file_path = "{config.credentials.file_path}"\n')
    buf.write("\n")

    # [logging]
    buf.write("[logging]\n")
    if config.logging.file is None:
        buf.write('# file = "~/.local/state/kasa-cli/log"\n')
    else:
        buf.write(f'file = "{config.logging.file}"\n')
    buf.write("\n")

    # [devices.<alias>]
    for alias in sorted(config.devices):
        entry = config.devices[alias]
        buf.write(f"[devices.{alias}]\n")
        if entry.ip is not None:
            buf.write(f'ip = "{entry.ip}"\n')
        if entry.mac is not None:
            buf.write(f'mac = "{entry.mac}"\n')
        if entry.credential_file is not None:
            buf.write(f'credential_file = "{entry.credential_file}"\n')
        buf.write("\n")

    # [groups]
    if config.groups:
        buf.write("[groups]\n")
        for group_name in sorted(config.groups):
            members = config.groups[group_name]
            members_repr = ", ".join(f'"{m}"' for m in members)
            buf.write(f"{group_name} = [{members_repr}]\n")
        buf.write("\n")

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_path(explicit_path: Path | None) -> tuple[Path | None, bool]:
    """Return ``(path, strict)``.

    ``strict`` is True when the path came from ``--config`` or
    ``KASA_CLI_CONFIG`` — a missing file at that path is an exit-code-6 error.
    """
    if explicit_path is not None:
        return explicit_path, True

    env_value = os.environ.get(ENV_CONFIG_PATH)
    if env_value:
        return Path(env_value).expanduser(), True

    return _default_config_path(), False


def _parse_and_validate(raw: bytes, path: Path) -> Config:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"config file is not valid UTF-8: {path}",
            extra={"path": str(path)},
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"malformed TOML in {path}: {exc}",
            extra={"path": str(path)},
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"config root must be a TOML table, got {type(data).__name__}",
            extra={"path": str(path)},
        )

    unknown_top = set(data) - _ALLOWED_TOP_LEVEL
    if unknown_top:
        raise ConfigError(
            f"unknown top-level table(s) in config: {sorted(unknown_top)}",
            hint=f"Allowed: {sorted(_ALLOWED_TOP_LEVEL)}",
            extra={"path": str(path)},
        )

    defaults = _parse_defaults(data.get("defaults", {}), path)
    credentials = _parse_credentials(data.get("credentials", {}), path)
    logging_cfg = _parse_logging(data.get("logging", {}), path)
    devices = _parse_devices(data.get("devices", {}), path)
    groups = _parse_groups(data.get("groups", {}), devices, path)

    return Config(
        defaults=defaults,
        credentials=credentials,
        logging=logging_cfg,
        devices=devices,
        groups=groups,
        source_path=path,
    )


def _require_table(value: Any, key: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"[{key}] must be a TOML table, got {type(value).__name__}",
            extra={"path": str(path)},
        )
    return value


def _parse_defaults(raw: Any, path: Path) -> DefaultsConfig:
    table = _require_table(raw, "defaults", path)
    unknown = set(table) - _ALLOWED_DEFAULTS_KEYS
    if unknown:
        raise ConfigError(
            f"unknown keys in [defaults]: {sorted(unknown)}",
            extra={"path": str(path)},
        )

    out = DefaultsConfig()
    if "timeout_seconds" in table:
        v = table["timeout_seconds"]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ConfigError(
                f"[defaults] timeout_seconds must be a positive int, got {v!r}",
                extra={"path": str(path)},
            )
        out = replace(out, timeout_seconds=v)
    if "concurrency" in table:
        v = table["concurrency"]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ConfigError(
                f"[defaults] concurrency must be a positive int, got {v!r}",
                extra={"path": str(path)},
            )
        out = replace(out, concurrency=v)
    if "output_format" in table:
        v = table["output_format"]
        if not isinstance(v, str) or v not in VALID_OUTPUT_FORMATS:
            raise ConfigError(
                f"[defaults] output_format must be one of "
                f"{sorted(VALID_OUTPUT_FORMATS)}, got {v!r}",
                extra={"path": str(path)},
            )
        out = replace(out, output_format=v)
    return out


def _parse_credentials(raw: Any, path: Path) -> CredentialsConfig:
    table = _require_table(raw, "credentials", path)
    unknown = set(table) - _ALLOWED_CREDENTIALS_KEYS
    if unknown:
        raise ConfigError(
            f"unknown keys in [credentials]: {sorted(unknown)}",
            extra={"path": str(path)},
        )
    out = CredentialsConfig()
    if "file_path" in table:
        v = table["file_path"]
        if not isinstance(v, str) or not v:
            raise ConfigError(
                f"[credentials] file_path must be a non-empty string, got {v!r}",
                extra={"path": str(path)},
            )
        out = replace(out, file_path=v)
    return out


def _parse_logging(raw: Any, path: Path) -> LoggingConfig:
    table = _require_table(raw, "logging", path)
    unknown = set(table) - _ALLOWED_LOGGING_KEYS
    if unknown:
        raise ConfigError(
            f"unknown keys in [logging]: {sorted(unknown)}",
            extra={"path": str(path)},
        )
    out = LoggingConfig()
    if "file" in table:
        v = table["file"]
        if not isinstance(v, str) or not v:
            raise ConfigError(
                f"[logging] file must be a non-empty string, got {v!r}",
                extra={"path": str(path)},
            )
        out = replace(out, file=v)
    return out


def _parse_devices(raw: Any, path: Path) -> dict[str, DeviceEntry]:
    table = _require_table(raw, "devices", path)
    out: dict[str, DeviceEntry] = {}
    for alias, entry_raw in table.items():
        if not isinstance(alias, str) or not alias:
            raise ConfigError(
                f"[devices] alias must be a non-empty string, got {alias!r}",
                extra={"path": str(path)},
            )
        entry_table = _require_table(entry_raw, f"devices.{alias}", path)
        unknown = set(entry_table) - _ALLOWED_DEVICE_KEYS
        if unknown:
            raise ConfigError(
                f"unknown keys in [devices.{alias}]: {sorted(unknown)}",
                extra={"path": str(path)},
            )
        ip = _opt_str(entry_table, "ip", f"devices.{alias}", path)
        mac = _opt_str(entry_table, "mac", f"devices.{alias}", path)
        cred = _opt_str(entry_table, "credential_file", f"devices.{alias}", path)
        out[alias] = DeviceEntry(alias=alias, ip=ip, mac=mac, credential_file=cred)
    return out


def _parse_groups(
    raw: Any,
    devices: dict[str, DeviceEntry],
    path: Path,
) -> dict[str, list[str]]:
    table = _require_table(raw, "groups", path)
    out: dict[str, list[str]] = {}
    for group_name, members_raw in table.items():
        if not isinstance(group_name, str) or not group_name:
            raise ConfigError(
                f"[groups] name must be a non-empty string, got {group_name!r}",
                extra={"path": str(path)},
            )
        if not isinstance(members_raw, list):
            raise ConfigError(
                f"[groups] {group_name} must be an array of alias names, "
                f"got {type(members_raw).__name__}",
                extra={"path": str(path)},
            )
        members: list[str] = []
        for m in members_raw:
            if not isinstance(m, str) or not m:
                raise ConfigError(
                    f"[groups] {group_name} member must be a non-empty string, got {m!r}",
                    extra={"path": str(path)},
                )
            if m not in devices:
                raise ConfigError(
                    f"[groups] {group_name} references unknown alias: {m!r}",
                    hint="Define [devices." + m + "] or remove the reference.",
                    extra={"path": str(path), "group": group_name, "alias": m},
                )
            members.append(m)
        out[group_name] = members
    return out


def _opt_str(table: dict[str, Any], key: str, where: str, path: Path) -> str | None:
    if key not in table:
        return None
    v = table[key]
    if not isinstance(v, str) or not v:
        raise ConfigError(
            f"[{where}] {key} must be a non-empty string, got {v!r}",
            extra={"path": str(path)},
        )
    return v


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_CREDENTIALS_FILE",
    "DEFAULT_OUTPUT_FORMAT",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_CONFIG_PATH",
    "VALID_OUTPUT_FORMATS",
    "Config",
    "CredentialsConfig",
    "DefaultsConfig",
    "DeviceEntry",
    "LoggingConfig",
    "effective_toml",
    "load_config",
    "validate_config",
]
