"""Exit codes and structured-error model for kasa-cli.

This module is the single source of truth for the integer exit codes referenced
throughout the SRD (§11.1) and the `StructuredError` shape emitted to stderr in
`--json`/`--jsonl`/`--quiet` modes (§11.2). Every CLI failure path raises a
subclass of :class:`KasaCliError`; the dispatcher in ``cli.py`` (Engineer B)
maps the exception to the right exit code and prints the structured form.

The ``error`` enum strings live alongside the exit codes here so tests can
assert key stability without touching CLI code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Final

logger = logging.getLogger("kasa_cli")

# ---------------------------------------------------------------------------
# Exit code constants (SRD §11.1)
# ---------------------------------------------------------------------------

EXIT_SUCCESS: Final[int] = 0
EXIT_DEVICE_ERROR: Final[int] = 1
EXIT_AUTH_ERROR: Final[int] = 2
EXIT_NETWORK_ERROR: Final[int] = 3
EXIT_NOT_FOUND: Final[int] = 4
EXIT_UNSUPPORTED: Final[int] = 5
EXIT_CONFIG_ERROR: Final[int] = 6
EXIT_PARTIAL_FAILURE: Final[int] = 7
EXIT_USAGE_ERROR: Final[int] = 64
EXIT_SIGINT: Final[int] = 130
EXIT_SIGTERM: Final[int] = 143


# ---------------------------------------------------------------------------
# Stable error-name enum (closed set per SRD §11.2)
# ---------------------------------------------------------------------------

ERROR_NAMES: Final[frozenset[str]] = frozenset(
    {
        "device_error",
        "auth_failed",
        "network_error",
        "not_found",
        "unsupported_feature",
        "config_error",
        "partial_failure",
        "usage_error",
        "interrupted",
    }
)


# ---------------------------------------------------------------------------
# Structured error payload (SRD §11.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructuredError:
    """Stable JSON shape emitted to stderr on failure.

    Tooling MAY pattern-match on ``error``; the field set is a closed enum
    (see :data:`ERROR_NAMES`). ``target`` and ``hint`` are optional.
    """

    error: str
    exit_code: int
    message: str
    target: str | None = None
    hint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.error not in ERROR_NAMES:
            raise ValueError(
                f"unknown structured error name: {self.error!r}; "
                f"must be one of {sorted(ERROR_NAMES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable dict, omitting null optional fields."""
        payload: dict[str, Any] = {
            "error": self.error,
            "exit_code": self.exit_code,
            "message": self.message,
        }
        if self.target is not None:
            payload["target"] = self.target
        if self.hint is not None:
            payload["hint"] = self.hint
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload

    def to_json(self) -> str:
        """Return the canonical single-line JSON form for stderr emission."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StructuredError:
        """Round-trip parse. Used by tests to assert JSON stability."""
        return cls(
            error=payload["error"],
            exit_code=int(payload["exit_code"]),
            message=payload["message"],
            target=payload.get("target"),
            hint=payload.get("hint"),
            extra=dict(payload.get("extra", {})),
        )

    def asdict_full(self) -> dict[str, Any]:
        """Return all fields including null optionals (for debugging only)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class KasaCliError(Exception):
    """Base class for all CLI failures that map to a non-zero exit code.

    Subclasses fix their own ``exit_code`` and ``error`` enum string.
    Carrying the optional ``target``/``hint`` lets the dispatcher produce a
    fully populated :class:`StructuredError` without further plumbing.
    """

    exit_code: int = EXIT_DEVICE_ERROR
    error_name: str = "device_error"

    def __init__(
        self,
        message: str,
        *,
        target: str | None = None,
        hint: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.target = target
        self.hint = hint
        self.extra: dict[str, Any] = dict(extra) if extra else {}

    def to_structured(self) -> StructuredError:
        """Project the exception onto the wire-format error object."""
        return StructuredError(
            error=self.error_name,
            exit_code=self.exit_code,
            message=self.message,
            target=self.target,
            hint=self.hint,
            extra=self.extra,
        )


class DeviceError(KasaCliError):
    """Device returned an error response (non-auth, non-network). Exit 1."""

    exit_code = EXIT_DEVICE_ERROR
    error_name = "device_error"


class AuthError(KasaCliError):
    """KLAP auth failed, no creds, or creds file mode too permissive. Exit 2."""

    exit_code = EXIT_AUTH_ERROR
    error_name = "auth_failed"


class NetworkError(KasaCliError):
    """Timeout, refused, no route, broadcast bind, lock timeout. Exit 3."""

    exit_code = EXIT_NETWORK_ERROR
    error_name = "network_error"


class NotFoundError(KasaCliError):
    """Alias unknown, IP unreachable, MAC not on LAN. Exit 4."""

    exit_code = EXIT_NOT_FOUND
    error_name = "not_found"


class UnsupportedFeatureError(KasaCliError):
    """Verb/flag combo not supported by target device family. Exit 5."""

    exit_code = EXIT_UNSUPPORTED
    error_name = "unsupported_feature"


class ConfigError(KasaCliError):
    """Bad TOML, missing required file, unresolvable refs, bad cred keys. Exit 6."""

    exit_code = EXIT_CONFIG_ERROR
    error_name = "config_error"


class PartialFailureError(KasaCliError):
    """Mixed-result batch/group: at least one ok, at least one fail. Exit 7."""

    exit_code = EXIT_PARTIAL_FAILURE
    error_name = "partial_failure"


class UsageError(KasaCliError):
    """Invalid CLI invocation: missing arg, mutually-exclusive flags. Exit 64."""

    exit_code = EXIT_USAGE_ERROR
    error_name = "usage_error"


class KasaInterruptError(KasaCliError):
    """SIGINT/SIGTERM during execution. Exit 130 or 143 — caller picks.

    Renamed from ``InterruptedError`` to avoid shadowing the Python builtin
    of the same name (an ``OSError`` subclass). Importing
    ``kasa_cli.errors.InterruptedError`` would silently mask the builtin
    inside this module's namespace.
    """

    exit_code = EXIT_SIGINT
    error_name = "interrupted"


__all__ = [
    "ERROR_NAMES",
    "EXIT_AUTH_ERROR",
    "EXIT_CONFIG_ERROR",
    "EXIT_DEVICE_ERROR",
    "EXIT_NETWORK_ERROR",
    "EXIT_NOT_FOUND",
    "EXIT_PARTIAL_FAILURE",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "EXIT_SUCCESS",
    "EXIT_UNSUPPORTED",
    "EXIT_USAGE_ERROR",
    "AuthError",
    "ConfigError",
    "DeviceError",
    "KasaCliError",
    "KasaInterruptError",
    "NetworkError",
    "NotFoundError",
    "PartialFailureError",
    "StructuredError",
    "UnsupportedFeatureError",
    "UsageError",
]
