"""Exit codes and error types for kasa-cli.

ENGINEER B STUB. Engineer A owns the authoritative version of this file in a
parallel worktree. The PM merges the two branches and keeps Engineer A's copy.
This stub exposes only the public surface that Engineer B's code imports, so
that B's code compiles and B's tests pass independently.

Public surface (must match Engineer A exactly):
- EXIT_* integer constants
- KasaCliError base + 7 named subclasses, each with fixed ``exit_code``
- ``StructuredError`` dataclass with ``to_json()``
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# --- Exit codes (SRD §11.1) ----------------------------------------------------

EXIT_OK: int = 0
EXIT_DEVICE_ERROR: int = 1
EXIT_AUTH: int = 2
EXIT_NETWORK: int = 3
EXIT_NOT_FOUND: int = 4
EXIT_UNSUPPORTED: int = 5
EXIT_CONFIG: int = 6
EXIT_PARTIAL: int = 7
EXIT_USAGE: int = 64
EXIT_SIGINT: int = 130
EXIT_SIGTERM: int = 143


# --- Exception hierarchy -------------------------------------------------------


class KasaCliError(Exception):
    """Base class for kasa-cli errors mapped to a specific exit code."""

    exit_code: int = EXIT_DEVICE_ERROR

    def __init__(
        self,
        message: str,
        *,
        target: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.target = target
        self.hint = hint


class DeviceError(KasaCliError):
    exit_code: int = EXIT_DEVICE_ERROR


class AuthError(KasaCliError):
    exit_code: int = EXIT_AUTH


class NetworkError(KasaCliError):
    exit_code: int = EXIT_NETWORK


class NotFoundError(KasaCliError):
    exit_code: int = EXIT_NOT_FOUND


class UnsupportedError(KasaCliError):
    exit_code: int = EXIT_UNSUPPORTED


class ConfigError(KasaCliError):
    exit_code: int = EXIT_CONFIG


class UsageError(KasaCliError):
    exit_code: int = EXIT_USAGE


# --- Structured stderr error (SRD §11.2) ---------------------------------------


@dataclass
class StructuredError:
    """Stderr-emitted JSON error envelope (closed enum on ``error``)."""

    error: str
    exit_code: int
    target: str | None
    message: str
    hint: str | None = None

    def to_json(self) -> str:
        """Serialize to a single-line JSON string suitable for stderr."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)
