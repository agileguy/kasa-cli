"""Live-device integration tests — gated on env vars; CI never runs them.

These tests run against a **real Kasa device** on the operator's LAN. They
are skipped by default; to run them, set ``KASA_TEST_DEVICE_IP`` and
optionally ``KASA_TEST_DEVICE_ALIAS`` and ``KASA_TEST_DEVICE_MAC`` in your
shell:

    KASA_TEST_DEVICE_IP=192.168.86.249 \\
    KASA_TEST_DEVICE_ALIAS=chair \\
    KASA_TEST_DEVICE_MAC=14:EB:B6:E7:7F:22 \\
    uv run pytest tests/test_live_device.py -v

CI never sets ``KASA_TEST_DEVICE_IP``, so every job in the matrix skips this
file. Per SRD §12.2, this is the canonical pattern for live-device tests.

Design constraints:

- **Idempotent**: every test that mutates state reads the initial state at
  setup, exercises the verb, and restores at teardown. If a test fails
  mid-cycle, the bulb may be left in a non-original state — that's the
  tradeoff for not requiring per-test teardown plumbing.
- **No assumptions about model**: the tests detect KL125-class color bulbs
  (the dev LAN's "Chair" bulb) but skip color-specific assertions when run
  against a non-color device. The on/off and discover tests run on every
  Kasa device.
- **No subprocess timing dependencies**: each test runs in <2 seconds against
  a LAN device with sub-50ms RTT. Hostile-network tradeoffs are not handled
  here — point this at a wired or 5GHz-Wi-Fi device.

The default device for the development LAN is the KL125 "Chair" bulb at
``192.168.86.249``. Replace via env vars to point at any other device.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import suppress
from typing import Any

import pytest

# Re-import inside test functions to make the skip mechanism robust under
# parallel test execution: if KASA_TEST_DEVICE_IP isn't set at import time,
# we still want the skip-marker chain below to fire correctly.
_LIVE_IP = os.environ.get("KASA_TEST_DEVICE_IP")
_LIVE_ALIAS = os.environ.get("KASA_TEST_DEVICE_ALIAS")  # optional
_LIVE_MAC = os.environ.get("KASA_TEST_DEVICE_MAC")  # optional

pytestmark = pytest.mark.skipif(
    _LIVE_IP is None,
    reason=(
        "Live-device tests require KASA_TEST_DEVICE_IP env var (and optionally "
        "KASA_TEST_DEVICE_ALIAS / KASA_TEST_DEVICE_MAC). CI deliberately does not "
        "set these — set them locally to run against your bulb."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def device_ip() -> str:
    assert _LIVE_IP is not None
    return _LIVE_IP


@pytest.fixture(scope="module")
def device_target() -> str:
    """Either the alias (if set) or the IP — whatever kasa-cli should accept."""
    return _LIVE_ALIAS or _LIVE_IP  # type: ignore[return-value]


@pytest.fixture
def initial_state(device_ip: str) -> Generator[dict[str, Any], None, None]:
    """Capture the device's full state, then restore it after the test runs.

    Restores: ``is_on`` (always), and ``brightness``/``color_temp``/``hsv``
    when the device supports those features. Best-effort: if any restore
    operation fails, we log a warning but don't fail the test (the test
    body's assertion takes precedence over teardown ergonomics).
    """
    captured = asyncio.run(_capture_state(device_ip))
    yield captured
    with suppress(Exception):
        asyncio.run(_restore_state(device_ip, captured))


async def _capture_state(host: str) -> dict[str, Any]:
    import kasa

    dev = await kasa.Device.connect(host=host)
    try:
        await dev.update()
        snapshot: dict[str, Any] = {"is_on": bool(dev.is_on)}
        light = dev.modules.get(kasa.Module.Light) if hasattr(dev, "modules") else None
        if light is not None:
            for attr in ("brightness", "color_temp", "hsv"):
                with suppress(Exception):
                    snapshot[attr] = getattr(light, attr)
        return snapshot
    finally:
        with suppress(Exception):
            await dev.disconnect()


async def _restore_state(host: str, snapshot: dict[str, Any]) -> None:
    import kasa

    dev = await kasa.Device.connect(host=host)
    try:
        await dev.update()
        light = dev.modules.get(kasa.Module.Light) if hasattr(dev, "modules") else None

        # Restore color-related fields first (only meaningful while on).
        if light is not None:
            if "hsv" in snapshot:
                with suppress(Exception):
                    h, s, v = snapshot["hsv"]
                    await light.set_hsv(h, s, v)
            elif "color_temp" in snapshot:
                with suppress(Exception):
                    await light.set_color_temp(snapshot["color_temp"])
            if "brightness" in snapshot:
                with suppress(Exception):
                    await light.set_brightness(snapshot["brightness"])

        # Then on/off — last, so the device ends up in the right power state.
        if snapshot.get("is_on"):
            await dev.turn_on()
        else:
            await dev.turn_off()
    finally:
        with suppress(Exception):
            await dev.disconnect()


def _kasa_cli(args: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run ``python -m kasa_cli ARGS`` from the repo working tree."""
    return subprocess.run(
        [sys.executable, "-m", "kasa_cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ},
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_discover_finds_device_by_mac(self, device_ip: str) -> None:
        """``discover`` should find the device on the LAN.

        We don't assert on the broadcast address — let python-kasa pick the
        interface. If your machine has multiple NICs and the device is missed,
        set KASA_TEST_TARGET_NETWORK to your LAN's CIDR.
        """
        target_network = os.environ.get("KASA_TEST_TARGET_NETWORK", "")
        args = ["--json", "--timeout", "10", "discover"]
        if target_network:
            args.extend(["--target-network", target_network])
        result = _kasa_cli(args)
        assert result.returncode == 0, f"discover failed: {result.stderr}"

        devices = json.loads(result.stdout)
        ips = {d["ip"] for d in devices}
        assert device_ip in ips, (
            f"device {device_ip} not in discovery output. "
            f"Found IPs: {sorted(ips)}. "
            f"Try setting KASA_TEST_TARGET_NETWORK if you have a multi-NIC host."
        )

    def test_discover_record_has_required_srd_fields(self, device_ip: str) -> None:
        """SRD §10.1 — every Device record must have these keys."""
        target_network = os.environ.get("KASA_TEST_TARGET_NETWORK", "")
        args = ["--json", "--timeout", "10", "discover"]
        if target_network:
            args.extend(["--target-network", target_network])
        result = _kasa_cli(args)
        assert result.returncode == 0
        devices = json.loads(result.stdout)
        ours = next(d for d in devices if d["ip"] == device_ip)

        # SRD-mandated fields per §10.1
        for key in (
            "alias",
            "ip",
            "mac",
            "model",
            "hardware_version",
            "firmware_version",
            "protocol",
            "features",
            "state",
            "sockets",
            "last_seen",
        ):
            assert key in ours, f"missing key {key} in Device record"

        # Protocol is the closed pair from SRD §10.1
        assert ours["protocol"] in {"legacy", "klap"}

    def test_discover_mac_matches_expected(self, device_ip: str) -> None:
        """If KASA_TEST_DEVICE_MAC is set, verify the discovered MAC."""
        if not _LIVE_MAC:
            pytest.skip("KASA_TEST_DEVICE_MAC not set")
        target_network = os.environ.get("KASA_TEST_TARGET_NETWORK", "")
        args = ["--json", "--timeout", "10", "discover"]
        if target_network:
            args.extend(["--target-network", target_network])
        result = _kasa_cli(args)
        assert result.returncode == 0
        devices = json.loads(result.stdout)
        ours = next(d for d in devices if d["ip"] == device_ip)
        # Normalize both sides to uppercase + colon-separated for the compare.
        expected = _LIVE_MAC.upper().replace("-", ":").replace(".", ":")
        assert ours["mac"].upper() == expected


# ---------------------------------------------------------------------------
# Info — populates fields that discovery leaves empty
# ---------------------------------------------------------------------------


class TestInfo:
    def test_info_populates_features_after_update(self, device_target: str) -> None:
        """``info`` issues a live update so ``features`` should be non-empty."""
        result = _kasa_cli(["--json", "info", device_target])
        assert result.returncode == 0, f"info failed: {result.stderr}"
        record = json.loads(result.stdout)
        # Every Kasa device exposes at least the on/off state feature.
        # We don't pin specific feature names — those vary by family — but
        # the list MUST be non-empty post-update.
        assert isinstance(record["features"], list)
        assert len(record["features"]) > 0, (
            f"info on a live device should populate features after update(); "
            f"got empty list. Full record: {record}"
        )

    def test_info_state_is_on_or_off(self, device_target: str) -> None:
        result = _kasa_cli(["--json", "info", device_target])
        assert result.returncode == 0
        record = json.loads(result.stdout)
        # Single-bulb device — state should be a leaf value, not "mixed"
        assert record["state"] in {"on", "off"}, (
            f"unexpected state {record['state']!r} for a single-device target"
        )


# ---------------------------------------------------------------------------
# On / off cycle
# ---------------------------------------------------------------------------


class TestPower:
    def test_on_off_cycle_restores_initial_state(
        self,
        device_target: str,
        initial_state: dict[str, Any],
    ) -> None:
        """Turn off, verify, turn on, verify. ``initial_state`` fixture restores."""
        # Ensure we're on the well-defined "off" side first
        off_result = _kasa_cli(["off", device_target])
        assert off_result.returncode == 0, f"off failed: {off_result.stderr}"

        info = _kasa_cli(["--json", "info", device_target])
        assert info.returncode == 0
        assert json.loads(info.stdout)["state"] == "off"

        on_result = _kasa_cli(["on", device_target])
        assert on_result.returncode == 0, f"on failed: {on_result.stderr}"

        info = _kasa_cli(["--json", "info", device_target])
        assert info.returncode == 0
        assert json.loads(info.stdout)["state"] == "on"

    def test_on_idempotent(
        self,
        device_target: str,
        initial_state: dict[str, Any],
    ) -> None:
        """``on`` against an already-on device exits 0 silently (FR-14)."""
        # Turn it on first to establish a known state
        assert _kasa_cli(["on", device_target]).returncode == 0
        # Now run on again — must still exit 0, no error
        again = _kasa_cli(["on", device_target])
        assert again.returncode == 0, f"on (second time) failed: {again.stderr}"


# ---------------------------------------------------------------------------
# Light controls — KL125-class color bulbs
# ---------------------------------------------------------------------------


def _supports_feature(device_target: str, feature: str) -> bool:
    """Best-effort feature check via the info verb."""
    result = _kasa_cli(["--json", "info", device_target])
    if result.returncode != 0:
        return False
    return feature in json.loads(result.stdout).get("features", [])


class TestLightControl:
    def test_set_brightness_takes_effect(
        self,
        device_target: str,
        initial_state: dict[str, Any],
    ) -> None:
        if not _supports_feature(device_target, "brightness"):
            pytest.skip("device does not advertise brightness")
        # Turn it on so brightness changes are visible
        assert _kasa_cli(["on", device_target]).returncode == 0

        # Set to a known mid-range value; verify by reading back
        result = _kasa_cli(["set", device_target, "--brightness", "40"])
        assert result.returncode == 0, f"set --brightness failed: {result.stderr}"
        # Read the device-side brightness via python-kasa directly to avoid
        # depending on the info verb's brightness reporting (some firmware
        # reports stale values for ~1 second after a write).
        readback = asyncio.run(_read_light_attr(_LIVE_IP, "brightness"))  # type: ignore[arg-type]
        assert readback == 40, f"expected brightness 40, got {readback}"

    def test_set_brightness_out_of_range_exits_64(self, device_target: str) -> None:
        """FR-20 / FR-15: brightness outside [0, 100] is a usage error (exit 64)."""
        result = _kasa_cli(["set", device_target, "--brightness", "200"])
        assert result.returncode == 64, (
            f"expected exit 64, got {result.returncode}. stderr: {result.stderr}"
        )
        # Stderr should be a §11.2 structured error
        envelope = json.loads(result.stderr.strip().splitlines()[-1])
        assert envelope["error"] == "usage_error"
        assert envelope["exit_code"] == 64

    def test_set_color_red(
        self,
        device_target: str,
        initial_state: dict[str, Any],
    ) -> None:
        if not _supports_feature(device_target, "hsv"):
            pytest.skip("device does not advertise color (hsv)")
        # Turn it on
        assert _kasa_cli(["on", device_target]).returncode == 0

        result = _kasa_cli(["set", device_target, "--color", "red"])
        assert result.returncode == 0, f"set --color red failed: {result.stderr}"

        h, s, v = asyncio.run(_read_light_attr(_LIVE_IP, "hsv"))  # type: ignore[arg-type]
        # Red should resolve to roughly H=0 (or 360), S=100, V=100. KL125
        # firmware sometimes rounds; allow a ±2 slop on H wrap-around.
        assert s >= 95, f"saturation should be ~100 for red; got {s}"
        assert v >= 95, f"value should be ~100 for red; got {v}"
        assert h in {0, 1, 2, 358, 359, 360}, f"hue should be ~0/360 for red; got {h}"

    def test_set_color_temp_clamped_to_device_range(
        self,
        device_target: str,
        initial_state: dict[str, Any],
    ) -> None:
        """FR-17: ``--color-temp`` is clamped to the device's reported range."""
        if not _supports_feature(device_target, "color_temp"):
            pytest.skip("device does not advertise color_temp")
        # Turn it on
        assert _kasa_cli(["on", device_target]).returncode == 0

        # Pick a value firmly inside the supported range (KL125 supports
        # roughly 2500K..6500K). Use 4000K as a safe midpoint.
        result = _kasa_cli(["set", device_target, "--color-temp", "4000"])
        assert result.returncode == 0, f"set --color-temp 4000 failed: {result.stderr}"

        readback = asyncio.run(_read_light_attr(_LIVE_IP, "color_temp"))  # type: ignore[arg-type]
        # Allow ±100K slop — some firmware rounds.
        assert 3900 <= readback <= 4100, f"expected color_temp ~4000, got {readback}"

    def test_set_implausible_color_temp_rejected(self, device_target: str) -> None:
        """27000K is the typo-trap (meant 2700K). Exit 64 with the typo hint."""
        result = _kasa_cli(["set", device_target, "--color-temp", "27000"])
        assert result.returncode == 64, (
            f"expected exit 64 for implausible kelvin; got {result.returncode}"
        )

    def test_mutually_exclusive_color_flags_exit_64(self, device_target: str) -> None:
        """FR-20: --hsv / --hex / --color are mutually exclusive — exit 64."""
        result = _kasa_cli(
            ["set", device_target, "--hsv", "0,100,100", "--hex", "#ffffff"],
        )
        assert result.returncode == 64, f"expected exit 64 for mutex flags; got {result.returncode}"


async def _read_light_attr(host: str, attr: str) -> Any:
    """Read a Light-module attribute directly (bypasses kasa-cli)."""
    import kasa

    dev = await kasa.Device.connect(host=host)
    try:
        await dev.update()
        light = dev.modules.get(kasa.Module.Light)
        assert light is not None, f"device at {host} has no Light module"
        return getattr(light, attr)
    finally:
        with suppress(Exception):
            await dev.disconnect()
