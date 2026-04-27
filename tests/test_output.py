"""Tests for :mod:`kasa_cli.output`."""

from __future__ import annotations

import io
import json
from typing import Any

from kasa_cli.errors import StructuredError
from kasa_cli.output import (
    OutputMode,
    detect_mode,
    device_to_text,
    emit,
    emit_error,
    emit_stream,
    list_view_to_text,
    reading_to_text,
)
from kasa_cli.types import Device, Reading

# --- detect_mode --------------------------------------------------------------


class _PipeStream(io.StringIO):
    def isatty(self) -> bool:
        return False


class _TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_detect_mode_quiet_wins() -> None:
    assert (
        detect_mode(json_flag=True, jsonl_flag=False, quiet=True, stream=_TtyStream())
        is OutputMode.QUIET
    )


def test_detect_mode_json_flag() -> None:
    assert (
        detect_mode(json_flag=True, jsonl_flag=False, quiet=False, stream=_TtyStream())
        is OutputMode.JSON
    )


def test_detect_mode_jsonl_flag() -> None:
    assert (
        detect_mode(json_flag=False, jsonl_flag=True, quiet=False, stream=_TtyStream())
        is OutputMode.JSONL
    )


def test_detect_mode_tty_default_text() -> None:
    assert (
        detect_mode(json_flag=False, jsonl_flag=False, quiet=False, stream=_TtyStream())
        is OutputMode.TEXT
    )


def test_detect_mode_pipe_default_jsonl() -> None:
    assert (
        detect_mode(json_flag=False, jsonl_flag=False, quiet=False, stream=_PipeStream())
        is OutputMode.JSONL
    )


# --- emit / emit_stream JSON-correctness (FR-35a) -----------------------------


def _sample_device() -> Device:
    return Device(
        alias="kitchen-lamp",
        ip="192.168.1.42",
        mac="AA:BB:CC:DD:EE:01",
        model="HS100",
        hardware_version="1.0",
        firmware_version="1.5.6",
        protocol="legacy",
        features=["on", "off"],
        state="on",
        sockets=None,
        last_seen="2026-04-27T20:00:00Z",
    )


def test_emit_quiet_writes_nothing() -> None:
    out = io.StringIO()
    emit(_sample_device(), OutputMode.QUIET, formatter=device_to_text, stream=out)
    assert out.getvalue() == ""


def test_emit_text_uses_formatter() -> None:
    out = io.StringIO()
    emit(_sample_device(), OutputMode.TEXT, formatter=device_to_text, stream=out)
    assert "kitchen-lamp" in out.getvalue()
    assert out.getvalue().endswith("\n")


def test_emit_json_is_pretty_and_valid() -> None:
    out = io.StringIO()
    emit(_sample_device(), OutputMode.JSON, formatter=device_to_text, stream=out)
    text = out.getvalue()
    parsed = json.loads(text)
    assert parsed["alias"] == "kitchen-lamp"
    # Pretty mode contains indent newlines.
    assert "\n  " in text


def test_emit_jsonl_is_single_line_and_valid() -> None:
    out = io.StringIO()
    emit(_sample_device(), OutputMode.JSONL, formatter=device_to_text, stream=out)
    lines = out.getvalue().rstrip("\n").splitlines()
    assert len(lines) == 1
    json.loads(lines[0])  # round-trips


def test_emit_stream_jsonl_each_line_is_valid_json() -> None:
    out = io.StringIO()
    items = [_sample_device(), _sample_device()]
    emit_stream(items, OutputMode.JSONL, formatter=device_to_text, stream=out)
    lines = out.getvalue().rstrip("\n").splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # FR-35a: every line round-trips


def test_emit_stream_json_emits_array() -> None:
    out = io.StringIO()
    emit_stream([_sample_device()], OutputMode.JSON, formatter=device_to_text, stream=out)
    parsed = json.loads(out.getvalue())
    assert isinstance(parsed, list)
    assert parsed[0]["alias"] == "kitchen-lamp"


def test_emit_stream_text_uses_formatter_per_item() -> None:
    out = io.StringIO()
    items: list[dict[str, Any]] = [
        {"alias": "a", "ip": "1.1.1.1", "mac": "MAC", "online": True},
        {"alias": "b", "ip": "2.2.2.2", "mac": "MAC2", "online": None},
    ]
    emit_stream(
        items,
        OutputMode.TEXT,
        formatter=lambda v: list_view_to_text(v),  # type: ignore[arg-type]
        stream=out,
    )
    assert "a" in out.getvalue() and "b" in out.getvalue()


# --- emit_error (SRD §11.2) ---------------------------------------------------


def test_emit_error_writes_valid_json_to_stream() -> None:
    err = StructuredError(
        error="auth_failed",
        exit_code=2,
        target="patio-plug",
        message="KLAP rejected",
        hint="Verify credentials.",
    )
    sink = io.StringIO()
    emit_error(err, OutputMode.JSON, stream=sink)
    text = sink.getvalue()
    parsed = json.loads(text)  # FR-35a: never malformed
    assert parsed["error"] == "auth_failed"
    assert parsed["exit_code"] == 2
    assert parsed["target"] == "patio-plug"


def test_emit_error_quiet_still_writes_to_stderr() -> None:
    """`--quiet` does NOT suppress structured errors."""
    err = StructuredError(
        error="not_found",
        exit_code=4,
        target="ghost",
        message="not in config",
    )
    sink = io.StringIO()
    emit_error(err, OutputMode.QUIET, stream=sink)
    assert sink.getvalue()  # non-empty
    json.loads(sink.getvalue())


def test_emit_error_omits_null_fields_in_json() -> None:
    """C6 / SRD §11.2: ``target`` and ``hint`` keys are absent when None."""
    err = StructuredError(
        error="device_error",
        exit_code=1,
        message="something broke",
        # target and hint default to None — must be omitted.
    )
    buf = io.StringIO()
    emit_error(err, OutputMode.JSON, stream=buf)
    parsed = json.loads(buf.getvalue())
    assert "target" not in parsed
    assert "hint" not in parsed
    assert parsed["error"] == "device_error"
    assert parsed["exit_code"] == 1
    assert parsed["message"] == "something broke"


# --- formatters smoke tests ---------------------------------------------------


def test_reading_to_text_basic() -> None:
    r = Reading(
        ts="2026-04-27T20:00:00Z",
        alias="strip",
        socket=2,
        current_power_w=42.1,
        voltage_v=120.2,
        current_a=0.35,
        today_kwh=None,
        month_kwh=None,
    )
    text = reading_to_text(r)
    assert "strip" in text
    assert "socket=2" in text
    assert "42.1" in text


def test_list_view_to_text_handles_unknown_online() -> None:
    text = list_view_to_text({"alias": "x", "ip": "1.1.1.1", "mac": "MAC", "online": None})
    assert " - " in text or text.endswith(" -")


def test_list_view_to_text_handles_offline() -> None:
    text = list_view_to_text({"alias": "x", "ip": "1.1.1.1", "mac": "MAC", "online": False})
    assert "no" in text


def test_list_view_to_text_handles_online() -> None:
    text = list_view_to_text({"alias": "x", "ip": "1.1.1.1", "mac": "MAC", "online": True})
    assert "yes" in text
