"""Tests for kasa_cli.errors — exit codes and structured error round-trip."""

from __future__ import annotations

import json

import pytest

from kasa_cli import errors

# ---------------------------------------------------------------------------
# Exit code constants (SRD §11.1)
# ---------------------------------------------------------------------------


def test_exit_code_constants_match_srd() -> None:
    """Every SRD §11.1 exit code is exposed under the documented name and value."""
    assert errors.EXIT_SUCCESS == 0
    assert errors.EXIT_DEVICE_ERROR == 1
    assert errors.EXIT_AUTH_ERROR == 2
    assert errors.EXIT_NETWORK_ERROR == 3
    assert errors.EXIT_NOT_FOUND == 4
    assert errors.EXIT_UNSUPPORTED == 5
    assert errors.EXIT_CONFIG_ERROR == 6
    assert errors.EXIT_PARTIAL_FAILURE == 7
    assert errors.EXIT_USAGE_ERROR == 64
    assert errors.EXIT_SIGINT == 130
    assert errors.EXIT_SIGTERM == 143


def test_exit_codes_are_unique() -> None:
    """No two exit-code constants share the same integer."""
    codes = [
        errors.EXIT_SUCCESS,
        errors.EXIT_DEVICE_ERROR,
        errors.EXIT_AUTH_ERROR,
        errors.EXIT_NETWORK_ERROR,
        errors.EXIT_NOT_FOUND,
        errors.EXIT_UNSUPPORTED,
        errors.EXIT_CONFIG_ERROR,
        errors.EXIT_PARTIAL_FAILURE,
        errors.EXIT_USAGE_ERROR,
        errors.EXIT_SIGINT,
        errors.EXIT_SIGTERM,
    ]
    assert len(set(codes)) == len(codes)


# ---------------------------------------------------------------------------
# Exception → exit-code wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_cls", "expected_code", "expected_name"),
    [
        (errors.DeviceError, 1, "device_error"),
        (errors.AuthError, 2, "auth_failed"),
        (errors.NetworkError, 3, "network_error"),
        (errors.NotFoundError, 4, "not_found"),
        (errors.UnsupportedFeatureError, 5, "unsupported_feature"),
        (errors.ConfigError, 6, "config_error"),
        (errors.PartialFailureError, 7, "partial_failure"),
        (errors.UsageError, 64, "usage_error"),
        (errors.InterruptedError, 130, "interrupted"),
    ],
)
def test_exception_class_attributes(
    exc_cls: type[errors.KasaCliError], expected_code: int, expected_name: str
) -> None:
    """Every exception subclass exposes the right exit code and error name."""
    assert exc_cls.exit_code == expected_code
    assert exc_cls.error_name == expected_name


def test_exception_to_structured_propagates_fields() -> None:
    """``to_structured()`` should carry message, target, hint, and extra through."""
    exc = errors.AuthError(
        "KLAP handshake rejected",
        target="patio-plug",
        hint="Verify credentials",
        extra={"path": "/tmp/creds"},
    )
    payload = exc.to_structured()
    assert payload.error == "auth_failed"
    assert payload.exit_code == 2
    assert payload.message == "KLAP handshake rejected"
    assert payload.target == "patio-plug"
    assert payload.hint == "Verify credentials"
    assert payload.extra == {"path": "/tmp/creds"}


# ---------------------------------------------------------------------------
# StructuredError JSON round-trip (SRD §11.2)
# ---------------------------------------------------------------------------


def test_structured_error_to_json_minimum_fields() -> None:
    """Required fields always present; optional null fields omitted."""
    err = errors.StructuredError(
        error="auth_failed",
        exit_code=2,
        message="KLAP handshake rejected",
    )
    payload = json.loads(err.to_json())
    assert payload == {
        "error": "auth_failed",
        "exit_code": 2,
        "message": "KLAP handshake rejected",
    }


def test_structured_error_to_json_with_optional_fields() -> None:
    """Target, hint, and extra appear when populated."""
    err = errors.StructuredError(
        error="auth_failed",
        exit_code=2,
        message="bad creds",
        target="patio-plug",
        hint="verify file",
        extra={"path": "/x"},
    )
    payload = json.loads(err.to_json())
    assert payload == {
        "error": "auth_failed",
        "exit_code": 2,
        "message": "bad creds",
        "target": "patio-plug",
        "hint": "verify file",
        "extra": {"path": "/x"},
    }


def test_structured_error_round_trip_through_json() -> None:
    """to_json → json.loads → from_dict reconstructs an equal StructuredError."""
    original = errors.StructuredError(
        error="config_error",
        exit_code=6,
        message="bad TOML",
        target=None,
        hint="fix syntax",
        extra={"path": "/etc/foo"},
    )
    decoded = errors.StructuredError.from_dict(json.loads(original.to_json()))
    assert decoded == original


def test_structured_error_round_trip_minimal() -> None:
    """A minimal payload (no optional fields) round-trips correctly."""
    original = errors.StructuredError(
        error="device_error",
        exit_code=1,
        message="boom",
    )
    payload = json.loads(original.to_json())
    decoded = errors.StructuredError.from_dict(payload)
    assert decoded == original


def test_structured_error_rejects_unknown_error_name() -> None:
    """The ``error`` enum is closed; unknown values raise ValueError."""
    with pytest.raises(ValueError, match="unknown structured error name"):
        errors.StructuredError(
            error="not_a_real_enum_value",
            exit_code=99,
            message="…",
        )


def test_structured_error_names_are_documented() -> None:
    """Every exception's ``error_name`` is in the closed enum."""
    closed_names = errors.ERROR_NAMES
    for cls in (
        errors.DeviceError,
        errors.AuthError,
        errors.NetworkError,
        errors.NotFoundError,
        errors.UnsupportedFeatureError,
        errors.ConfigError,
        errors.PartialFailureError,
        errors.UsageError,
        errors.InterruptedError,
    ):
        assert cls.error_name in closed_names
