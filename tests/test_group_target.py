"""Tests for ``@group-name`` target syntax fanout (Phase 3 — FR-26..29a, FR-28).

These exercise the cli.py-level ``_dispatch_target_or_group`` integration
through ``CliRunner`` end-to-end. We validate four contracts:

1. ``kasa-cli on @<group>`` issues turn_on across each member and exits 0
   when all succeed.
2. Mixed result (1 success + 1 unreachable) returns aggregate exit **7**
   (FR-29a partial failure).
3. All-unreachable returns aggregate exit **3** (homogeneous failure —
   first-failure-code rule, NOT 7).
4. ``--concurrency N`` cap is respected (no more than N tasks run
   concurrently inside the fanout).

Phase 1+2 anti-pattern fix: every test asserting an exit code asserts the
EXACT integer (``== 0``, ``== 3``, ``== 7``, ``== 64``), never ``!= 0``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kasa_cli.cli import main as cli_main

# --- Test fixtures -----------------------------------------------------------


@pytest.fixture
def group_config(tmp_path: Path) -> Path:
    """A config file with 3 devices and a 3-member group ``bedroom-lights``."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[defaults]
concurrency = 4

[devices.alpha]
ip = "10.0.0.1"

[devices.beta]
ip = "10.0.0.2"

[devices.gamma]
ip = "10.0.0.3"

[groups]
bedroom-lights = ["alpha", "beta", "gamma"]
""",
        encoding="utf-8",
    )
    return cfg_path


# --- @group fanout: happy path ----------------------------------------------


def test_on_group_target_fans_out_and_returns_zero(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """`kasa-cli on @bedroom-lights` against a 3-member group: all on, exit 0."""
    devices: dict[str, Any] = {}

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        d = make_device(alias=str(host), host=str(host), model="HS100", is_on=False)
        devices[str(host)] = d
        return d

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(group_config),
            "--jsonl",
            "on",
            "@bedroom-lights",
        ],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    # JSONL: one TaskResult per member, all success.
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert all(p["success"] for p in parsed)
    assert all(p["exit_code"] == 0 for p in parsed)
    assert {p["target"] for p in parsed} == {"alpha", "beta", "gamma"}


# --- @group fanout: mixed result -> exit 7 ----------------------------------


def test_on_group_mixed_success_and_unreachable_returns_seven(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """One member succeeds, one is unreachable -> exit code is exactly 7.

    Per FR-29a: 1+ success AND 1+ failure -> partial-failure exit 7.
    """

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        if str(host).endswith(".2"):
            # beta -> unreachable
            from kasa.exceptions import TimeoutError as KasaTimeoutError

            raise KasaTimeoutError("simulated unreachable")
        return make_device(alias=str(host), host=str(host), model="HS100", is_on=False)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(group_config),
            "--jsonl",
            "on",
            "@bedroom-lights",
        ],
    )
    assert result.exit_code == 7, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    # Three JSONL lines (one per member); two successes + one failure.
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    successes = [p for p in parsed if p["success"]]
    failures = [p for p in parsed if not p["success"]]
    assert len(successes) == 2
    assert len(failures) == 1
    # The failure should carry a §11.2 error envelope.
    assert failures[0]["error"]["error"] == "network_error"
    assert failures[0]["error"]["exit_code"] == 3


# --- @group fanout: all-unreachable -> exit 3 (homogeneous) -----------------


def test_on_group_all_unreachable_returns_exit_three(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All members unreachable -> exit 3 (homogeneous failure, NOT 7).

    Per FR-29a: every sub-op failed for the same reason -> that reason's code.
    """
    from kasa.exceptions import TimeoutError as KasaTimeoutError

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        raise KasaTimeoutError("nothing reachable")

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(group_config),
            "--jsonl",
            "on",
            "@bedroom-lights",
        ],
    )
    assert result.exit_code == 3, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert all(not p["success"] for p in parsed)
    assert all(p["exit_code"] == 3 for p in parsed)


# --- @group fanout: all-auth-failed -> exit 2 (homogeneous) -----------------


def test_on_group_all_auth_failed_returns_exit_two(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All members reject KLAP auth -> exit 2."""
    from kasa.exceptions import AuthenticationError

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        raise AuthenticationError("creds rejected")

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(group_config),
            "--jsonl",
            "on",
            "@bedroom-lights",
        ],
    )
    assert result.exit_code == 2, f"stderr: {result.stderr}\nstdout: {result.stdout}"


# --- @group fanout: unknown group -> exit 64 (usage error) ------------------


def test_unknown_group_target_returns_exit_64(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`kasa-cli on @nonexistent` fails at resolution with exit 64."""
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(group_config),
            "--jsonl",
            "on",
            "@nonexistent",
        ],
    )
    assert result.exit_code == 64, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    # Stderr should include a structured error envelope.
    err_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert err_lines
    parsed_err = json.loads(err_lines[-1])
    assert parsed_err["exit_code"] == 64
    assert parsed_err["error"] == "usage_error"


# --- @group fanout: empty group --------------------------------------------


def test_empty_group_returns_zero_and_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty group target is a no-op -> exit 0, no stdout."""
    # Note: an empty group has no members listed under [groups]. The config
    # parser would reject ``g = []`` only if it tried to validate against
    # devices; it accepts a literal empty TOML array.
    cfg_path = tmp_path / "empty.toml"
    cfg_path.write_text(
        """
[devices.alpha]
ip = "1.1.1.1"

[groups]
empty = []
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--config", str(cfg_path), "--jsonl", "on", "@empty"],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    # No member to act on -> no JSONL lines.
    assert result.stdout.strip() == ""


# --- @group + --socket combo rejected --------------------------------------


def test_socket_with_group_target_rejected_with_exit_64(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combining ``--socket`` with a ``@group`` target is a usage error.

    Different strips have different socket counts, and most groups will mix
    strip + non-strip devices. Applying the same socket index to every
    member is operationally surprising; we reject the combo at exit 64.
    """
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(group_config),
            "--jsonl",
            "on",
            "@bedroom-lights",
            "--socket",
            "1",
        ],
    )
    assert result.exit_code == 64, f"stderr: {result.stderr}\nstdout: {result.stdout}"


# --- --concurrency cap is respected during fanout ---------------------------


def test_group_fanout_respects_concurrency_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """``--concurrency 2`` caps in-flight connect+update calls at 2.

    We instrument the fake ``Device.connect`` to track concurrent in-flight
    counts and assert the maximum never exceeds the cap. The fanout dispatcher
    threads ``--concurrency`` through to ``parallel.run_parallel``'s
    semaphore, which is what enforces the bound.
    """
    # 6-member group so the cap matters.
    cfg_path = tmp_path / "many.toml"
    cfg_path.write_text(
        """
[defaults]
concurrency = 10

[devices.a]
ip = "10.1.0.1"
[devices.b]
ip = "10.1.0.2"
[devices.c]
ip = "10.1.0.3"
[devices.d]
ip = "10.1.0.4"
[devices.e]
ip = "10.1.0.5"
[devices.f]
ip = "10.1.0.6"

[groups]
many = ["a", "b", "c", "d", "e", "f"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        # Force overlap with a small sleep.
        await asyncio.sleep(0.05)
        with lock:
            in_flight -= 1
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        return make_device(alias=str(host), host=str(host), model="HS100", is_on=False)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(cfg_path),
            "--jsonl",
            "--concurrency",
            "2",
            "on",
            "@many",
        ],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert max_in_flight <= 2, f"observed max_in_flight={max_in_flight} > cap=2"
    # Sanity: with 6 members and concurrency=2 we should have hit the cap.
    assert max_in_flight >= 2


def test_group_fanout_uses_config_concurrency_when_no_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """No ``--concurrency`` flag -> ``[defaults] concurrency`` is honored."""
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(
        """
[defaults]
concurrency = 1

[devices.a]
ip = "10.2.0.1"
[devices.b]
ip = "10.2.0.2"
[devices.c]
ip = "10.2.0.3"

[groups]
g = ["a", "b", "c"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        with lock:
            in_flight -= 1
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        return make_device(alias=str(host), host=str(host), model="HS100", is_on=False)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--config", str(cfg_path), "--jsonl", "on", "@g"],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    # concurrency=1 -> serial -> max_in_flight == 1
    assert max_in_flight == 1


# --- --concurrency override beats config -----------------------------------


def test_concurrency_flag_overrides_config_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """``--concurrency 3`` beats ``[defaults] concurrency = 1`` from config."""
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(
        """
[defaults]
concurrency = 1

[devices.a]
ip = "10.3.0.1"
[devices.b]
ip = "10.3.0.2"
[devices.c]
ip = "10.3.0.3"
[devices.d]
ip = "10.3.0.4"

[groups]
g = ["a", "b", "c", "d"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        with lock:
            in_flight -= 1
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        return make_device(alias=str(host), host=str(host), model="HS100", is_on=False)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "--config",
            str(cfg_path),
            "--jsonl",
            "--concurrency",
            "3",
            "on",
            "@g",
        ],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    # We allowed 3 concurrent ops; should observe max in [2, 3], not 1.
    assert max_in_flight >= 2
    assert max_in_flight <= 3


# --- info @group emits a TaskResult per member ------------------------------


def test_info_group_target_fans_out(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """``info @bedroom-lights`` emits one TaskResult per member."""

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        return make_device(alias=str(host), host=str(host), model="HS100", is_on=True)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--config", str(group_config), "--jsonl", "info", "@bedroom-lights"],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    lines = [line for line in result.stdout.strip().splitlines() if line]
    # Each member emits one TaskResult line in fanout mode (the per-target
    # info() output is suppressed under the fanout's QUIET mode, which is the
    # documented v1 contract).
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert all(p["success"] for p in parsed)


# --- single-target fall-through is unchanged --------------------------------


def test_single_target_alias_unchanged_by_fanout_changes(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """A non-@-prefixed alias still goes through the single-target verb path.

    This regression-catches any accidental fanout-on-single-target bug
    introduced by the cli.py changes.
    """
    dev = make_device(alias="alpha", host="10.0.0.1", model="HS100", is_on=False)

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return dev

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--config", str(group_config), "--jsonl", "on", "alpha"],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    # Single-target ``on`` is silent on success; no TaskResult JSONL stream.
    assert result.stdout.strip() == ""
    assert dev.turn_on_called == 1


# --- @group on JSON mode produces a single top-level array ------------------


def test_group_target_json_mode_emits_array(
    group_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_device: Callable[..., Any],
) -> None:
    """``--json on @group`` emits a single JSON array of TaskResult dicts."""

    async def _fake_connect(*_args: Any, **kwargs: Any) -> Any:
        host = kwargs.get("host") or (kwargs.get("config") and kwargs["config"].host)
        return make_device(alias=str(host), host=str(host), model="HS100", is_on=False)

    monkeypatch.setattr("kasa.Device.connect", _fake_connect)
    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["--config", str(group_config), "--json", "on", "@bedroom-lights"],
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == 3
    assert all(p["success"] for p in parsed)


# --- Suppress unused-import lint --------------------------------------------

_ = (time,)  # silence; available for future timing-based assertions
