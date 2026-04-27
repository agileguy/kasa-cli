"""Credentials resolver for kasa-cli (SRD §6).

Resolution order (FR-CRED-1 / 2 / 3, §6.2):

1. Per-target override: ``[devices.<alias>] credential_file = "<path>"``
2. Environment variables: ``KASA_USERNAME`` and ``KASA_PASSWORD``
3. Default credentials file: ``[credentials] file_path``
4. ``None`` (legacy-protocol path only — KLAP devices will then fail with
   :class:`AuthError` upstream)

Credentials file format (FR-CRED-1):

.. code-block:: json

    {
      "version": 1,
      "username": "you@example.com",
      "password": "..."
    }

Strict rules:

- Mode more permissive than 0600 → exit code 2.
- Unknown extra keys → exit code 6.
- Missing ``version`` → treated as v1 with a single deprecation warning.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from kasa_cli.config import Config
from kasa_cli.errors import AuthError, ConfigError

logger = logging.getLogger("kasa_cli")


ENV_USERNAME: Final[str] = "KASA_USERNAME"
ENV_PASSWORD: Final[str] = "KASA_PASSWORD"

CURRENT_VERSION: Final[int] = 1
ALLOWED_CRED_KEYS_V1: Final[frozenset[str]] = frozenset({"version", "username", "password"})

# Process-wide latch so the missing-version deprecation warning is emitted
# at most once per process per file path.
_DEPRECATION_LOCK = threading.Lock()
_DEPRECATION_WARNED: set[str] = set()


@dataclass(frozen=True, slots=True)
class Credentials:
    """Resolved username/password pair plus the source description."""

    username: str
    password: str
    source: str
    """Human-readable label, e.g. ``"env"``, ``"~/.config/kasa-cli/credentials"``,
    or ``"per-device:patio-plug"``. Used by ``-v`` logging."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_credentials(config: Config, alias: str | None = None) -> Credentials | None:
    """Walk the credential resolution chain.

    Args:
        config: Active config (must have ``credentials.file_path`` populated).
        alias: Target device alias; if its config entry has a
            ``credential_file`` override, that path is consulted first.

    Returns:
        Credentials on hit, or ``None`` when no source produced credentials.
    """
    # 1) Per-device override.
    if alias is not None:
        entry = config.devices.get(alias)
        if entry is not None and entry.credential_file:
            override_path = _expand(entry.credential_file)
            creds = _load_credentials_file(override_path, source_label=f"per-device:{alias}")
            if creds is not None:
                return creds
            logger.debug(
                "per-device credential file missing for alias=%s path=%s — falling through",
                alias,
                override_path,
            )

    # 2) Environment variables.
    env_user = os.environ.get(ENV_USERNAME)
    env_pass = os.environ.get(ENV_PASSWORD)
    if env_user and env_pass:
        return Credentials(username=env_user, password=env_pass, source="env")

    # 3) Default credentials file.
    default_path = _expand(config.credentials.file_path)
    creds = _load_credentials_file(default_path, source_label=str(default_path))
    if creds is not None:
        return creds

    logger.debug(
        "credentials not resolved: per-device override absent or empty, "
        "env vars unset, default file %s missing",
        default_path,
    )
    return None


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def _load_credentials_file(path: Path, *, source_label: str) -> Credentials | None:
    """Load and validate a credentials JSON file.

    Returns ``None`` when the file does not exist (FR-CRED-3 fall-through).
    Raises :class:`AuthError` on permissive mode (FR-CRED-2) and
    :class:`ConfigError` on schema problems (FR-CRED-1).
    """
    if not path.exists():
        logger.debug("credentials file not present: %s", path)
        return None

    _enforce_permissions(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot read credentials file: {path} ({exc})",
            extra={"path": str(path)},
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"credentials file is not valid JSON: {path}: {exc.msg}",
            extra={"path": str(path)},
        ) from exc

    if not isinstance(payload, dict):
        raise ConfigError(
            f"credentials file root must be a JSON object: {path}",
            extra={"path": str(path)},
        )

    version = _coerce_version(payload, path)
    if version != CURRENT_VERSION:
        raise ConfigError(
            f"unsupported credentials file version {version} in {path}; expected {CURRENT_VERSION}",
            hint="Run `kasa-cli auth migrate` once that sub-verb ships in Phase 2.",
            extra={"path": str(path), "version": version},
        )

    unknown = set(payload) - ALLOWED_CRED_KEYS_V1
    if unknown:
        raise ConfigError(
            f"unknown keys in credentials file {path}: {sorted(unknown)}",
            hint=f"Allowed: {sorted(ALLOWED_CRED_KEYS_V1)}",
            extra={"path": str(path), "unknown_keys": sorted(unknown)},
        )

    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not username:
        raise ConfigError(
            f"credentials file {path}: 'username' must be a non-empty string",
            extra={"path": str(path)},
        )
    if not isinstance(password, str) or not password:
        raise ConfigError(
            f"credentials file {path}: 'password' must be a non-empty string",
            extra={"path": str(path)},
        )

    return Credentials(username=username, password=password, source=source_label)


def _coerce_version(payload: dict[str, Any], path: Path) -> int:
    """Pull ``version`` out, defaulting to 1 with a one-shot stderr warning."""
    if "version" in payload:
        v = payload["version"]
        if not isinstance(v, int) or isinstance(v, bool):
            raise ConfigError(
                f"credentials file {path}: 'version' must be an int, got {v!r}",
                extra={"path": str(path)},
            )
        return v

    # Missing version → treat as v1 with a single deprecation warning per
    # path per process (FR-CRED-1).
    key = str(path)
    with _DEPRECATION_LOCK:
        already_warned = key in _DEPRECATION_WARNED
        if not already_warned:
            _DEPRECATION_WARNED.add(key)
    if not already_warned:
        logger.warning(
            "credentials file %s lacks a 'version' field; assuming version=1. "
            'Add `"version": 1` to silence this warning. A future `kasa-cli '
            "auth migrate` sub-verb will rewrite older files in place.",
            path,
        )
    return 1


def _enforce_permissions(path: Path) -> None:
    """Reject permissive modes — and refuse symlinks outright (FR-CRED-2 / R5).

    A symlink to a 0600-mode target file would otherwise pass: ``path.stat()``
    follows the link and reads the target's mode. By refusing symlinks before
    any stat call, we ensure the actual file an operator sees in
    ``ls -l <path>`` is the one we audited.
    """
    if path.is_symlink():
        raise AuthError(
            f"credentials file {path} is a symlink; refusing for safety",
            hint="Replace the symlink with the actual file or use a per-device override.",
            extra={"path": str(path)},
        )
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise AuthError(
            f"credentials file {path} has mode {oct(mode)}; expected 0600",
            hint=f"Run: chmod 600 {path}",
            extra={"path": str(path), "mode": oct(mode)},
        )


def _expand(raw_path: str) -> Path:
    """Expand ``~`` and environment variables, returning a fully-resolved Path."""
    return Path(os.path.expandvars(raw_path)).expanduser()


# ---------------------------------------------------------------------------
# Test seam — clear the deprecation latch between tests
# ---------------------------------------------------------------------------


def _reset_deprecation_state_for_tests() -> None:
    """Test-only: clear the once-per-process deprecation warning latch."""
    with _DEPRECATION_LOCK:
        _DEPRECATION_WARNED.clear()


__all__ = [
    "ALLOWED_CRED_KEYS_V1",
    "CURRENT_VERSION",
    "ENV_PASSWORD",
    "ENV_USERNAME",
    "Credentials",
    "resolve_credentials",
]
