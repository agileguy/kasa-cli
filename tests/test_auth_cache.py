"""Tests for kasa_cli.auth_cache — atomic writes, expiry, locking, perms."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kasa_cli import auth_cache
from kasa_cli.errors import NetworkError

MAC_A = "AA:BB:CC:DD:EE:01"
MAC_B = "AA:BB:CC:DD:EE:02"


@pytest.fixture(autouse=True)
def _redirect_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the cache root to ``tmp_path`` for every test.

    Also clears the in-process lock registry so each test starts with a
    fresh per-path threading.Lock — otherwise a previous test's stale entry
    would race against a new lockfile path inside a different ``tmp_path``.
    """
    monkeypatch.setenv(auth_cache.ENV_CONFIG_DIR, str(tmp_path))
    auth_cache._PROCESS_LOCK_REGISTRY.clear()
    return tmp_path


# ---------------------------------------------------------------------------
# cache_dir / cache_path_for_mac
# ---------------------------------------------------------------------------


def test_cache_dir_creates_with_chmod_0700(_redirect_cache_dir: Path) -> None:
    tokens = auth_cache.cache_dir()
    assert tokens.exists()
    assert tokens.is_dir()
    mode = stat.S_IMODE(tokens.stat().st_mode)
    assert mode == 0o700


def test_cache_path_for_mac_normalizes_case_and_separators() -> None:
    p1 = auth_cache.cache_path_for_mac("aa:bb:cc:dd:ee:01")
    p2 = auth_cache.cache_path_for_mac("AA-BB-CC-DD-EE-01")
    p3 = auth_cache.cache_path_for_mac("AABBCCDDEE01")
    assert p1.name == p2.name == p3.name == "AA:BB:CC:DD:EE:01.json"


# ---------------------------------------------------------------------------
# save_session / load_session — atomic write + perms
# ---------------------------------------------------------------------------


def test_save_session_writes_atomically_with_chmod_0600() -> None:
    """Save persists ``seed`` verbatim and stores expiry as wall-clock.

    Per FR-CRED-6, the on-disk form uses ``_session_expire_at_wallclock``
    (UNIX seconds) rather than the in-memory monotonic value, so the cache
    survives process restarts. The non-temporal payload (``seed`` here)
    round-trips byte-for-byte.
    """
    state = {"_session_expire_at": time.monotonic() + 600, "seed": "abc"}
    auth_cache.save_session(MAC_A, state)
    path = auth_cache.cache_path_for_mac(MAC_A)
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    on_disk = json.loads(path.read_text())
    # Non-temporal payload is preserved verbatim.
    assert on_disk["seed"] == "abc"
    # Monotonic key is NOT persisted; wall-clock key replaces it.
    assert auth_cache.EXPIRE_KEY not in on_disk
    assert auth_cache.EXPIRE_KEY_WALLCLOCK in on_disk
    # The wall-clock value is in the future (we saved expiry = now+600s).
    assert on_disk[auth_cache.EXPIRE_KEY_WALLCLOCK] > time.time()


def test_save_session_does_not_leave_tempfiles() -> None:
    state = {"_session_expire_at": time.monotonic() + 600}
    auth_cache.save_session(MAC_A, state)
    # No leftover .tmp siblings.
    siblings = list(auth_cache.cache_dir().iterdir())
    tempfiles = [p for p in siblings if p.suffix == ".tmp"]
    assert tempfiles == []


def test_load_session_returns_state_when_fresh() -> None:
    state = {"_session_expire_at": time.monotonic() + 600, "seed": "z"}
    auth_cache.save_session(MAC_A, state)
    out = auth_cache.load_session(MAC_A)
    assert out is not None
    assert out["seed"] == "z"


def test_load_session_returns_none_when_absent() -> None:
    assert auth_cache.load_session("FF:FF:FF:FF:FF:FF") is None


# ---------------------------------------------------------------------------
# Expiration semantics (FR-CRED-6)
# ---------------------------------------------------------------------------


def test_load_session_expired_reads_as_miss() -> None:
    """A monotonic expire in the past must produce ``None``."""
    state = {"_session_expire_at": time.monotonic() - 5.0, "seed": "stale"}
    auth_cache.save_session(MAC_A, state)
    assert auth_cache.load_session(MAC_A) is None


def test_load_session_no_expire_key_is_returned_as_is() -> None:
    """Missing ``_session_expire_at`` is the wrapper's job — we don't reject."""
    state = {"seed": "no-expire"}
    auth_cache.save_session(MAC_A, state)
    out = auth_cache.load_session(MAC_A)
    assert out is not None
    assert out["seed"] == "no-expire"


def test_load_session_non_numeric_expire_is_miss() -> None:
    auth_cache.save_session(MAC_A, {"_session_expire_at": "not-a-number"})
    assert auth_cache.load_session(MAC_A) is None


def test_load_session_drops_corrupt_file() -> None:
    """Malformed JSON on disk is dropped and ``None`` returned."""
    path = auth_cache.cache_path_for_mac(MAC_A)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert auth_cache.load_session(MAC_A) is None
    assert not path.exists()


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def test_flush_one_removes_specific_entry() -> None:
    auth_cache.save_session(MAC_A, {"_session_expire_at": time.monotonic() + 60})
    auth_cache.save_session(MAC_B, {"_session_expire_at": time.monotonic() + 60})
    assert auth_cache.flush_one(MAC_A) is True
    assert auth_cache.cache_path_for_mac(MAC_A).exists() is False
    assert auth_cache.cache_path_for_mac(MAC_B).exists() is True


def test_flush_one_returns_false_when_absent() -> None:
    assert auth_cache.flush_one("FF:FF:FF:FF:FF:FF") is False


def test_flush_all_removes_every_session() -> None:
    auth_cache.save_session(MAC_A, {"_session_expire_at": time.monotonic() + 60})
    auth_cache.save_session(MAC_B, {"_session_expire_at": time.monotonic() + 60})
    removed = auth_cache.flush_all()
    assert removed == 2
    assert list(auth_cache.cache_dir().glob("*.json")) == []


def test_flush_all_on_empty_dir_returns_zero() -> None:
    assert auth_cache.flush_all() == 0


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def test_list_sessions_reports_metadata_for_each_entry() -> None:
    expire = time.monotonic() + 600
    auth_cache.save_session(MAC_A, {"_session_expire_at": expire})
    auth_cache.save_session(MAC_B, {"_session_expire_at": expire + 100})
    rows = auth_cache.list_sessions()
    assert len(rows) == 2
    by_mac = {row.mac: row for row in rows}
    assert MAC_A in by_mac
    assert MAC_B in by_mac
    row_a = by_mac[MAC_A]
    assert row_a.expires_at_monotonic is not None
    assert abs(row_a.expires_at_monotonic - expire) < 1e-3
    assert row_a.bytes_size > 0
    assert row_a.path == auth_cache.cache_path_for_mac(MAC_A)


def test_list_sessions_skips_lock_files() -> None:
    """Lockfiles created by lock_for_write must not show up in list_sessions."""
    with auth_cache.lock_for_write(MAC_A, timeout=1.0):
        pass
    rows = auth_cache.list_sessions()
    assert rows == []


# ---------------------------------------------------------------------------
# Locking (FR-CRED-10)
# ---------------------------------------------------------------------------


def test_lock_for_write_blocks_concurrent_thread_until_first_releases() -> None:
    """Two threads in the same process must serialize on the per-MAC lock."""
    barrier = threading.Event()
    second_acquired_after = []

    def first() -> None:
        with auth_cache.lock_for_write(MAC_A, timeout=2.0):
            barrier.set()
            time.sleep(0.20)

    def second() -> None:
        barrier.wait()
        start = time.monotonic()
        with auth_cache.lock_for_write(MAC_A, timeout=2.0):
            second_acquired_after.append(time.monotonic() - start)

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert len(second_acquired_after) == 1
    # Second waited at least most of the 0.20s sleep — i.e. the lock held it back.
    assert second_acquired_after[0] >= 0.10


def test_lock_for_write_times_out_with_network_error() -> None:
    """Failure to acquire within timeout SHALL exit code 3."""
    holder_holding = threading.Event()
    holder_release = threading.Event()
    raised: list[BaseException] = []

    def holder() -> None:
        with auth_cache.lock_for_write(MAC_A, timeout=2.0):
            holder_holding.set()
            holder_release.wait(timeout=5)

    def contender() -> None:
        holder_holding.wait()
        try:
            with auth_cache.lock_for_write(MAC_A, timeout=0.1):
                pass
        except BaseException as exc:
            raised.append(exc)

    h = threading.Thread(target=holder)
    c = threading.Thread(target=contender)
    h.start()
    c.start()
    c.join(timeout=5)
    holder_release.set()
    h.join(timeout=5)
    assert len(raised) == 1
    err = raised[0]
    assert isinstance(err, NetworkError)
    assert err.exit_code == 3
    assert err.target == MAC_A


def test_lock_for_write_separate_macs_do_not_block() -> None:
    """Lock A and lock B are independent — concurrency must not be cross-MAC."""
    started_b = threading.Event()
    finished_b = threading.Event()

    def hold_a() -> None:
        with auth_cache.lock_for_write(MAC_A, timeout=2.0):
            started_b.wait(timeout=5)
            time.sleep(0.05)

    def hold_b() -> None:
        with auth_cache.lock_for_write(MAC_B, timeout=2.0):
            started_b.set()
            time.sleep(0.05)
            finished_b.set()

    a = threading.Thread(target=hold_a)
    b = threading.Thread(target=hold_b)
    a.start()
    b.start()
    a.join(timeout=5)
    b.join(timeout=5)
    assert finished_b.is_set()


# ---------------------------------------------------------------------------
# End-to-end: locked write then independent read sees the latest value
# ---------------------------------------------------------------------------


def test_locked_write_then_unlocked_read_round_trips() -> None:
    state = {"_session_expire_at": time.monotonic() + 600, "v": 1}
    with auth_cache.lock_for_write(MAC_A, timeout=1.0):
        auth_cache.save_session(MAC_A, state)
    out = auth_cache.load_session(MAC_A)
    assert out is not None
    assert out["v"] == 1


# ---------------------------------------------------------------------------
# FR-CRED-6 cross-process round-trip
# ---------------------------------------------------------------------------


def test_session_expiry_survives_process_restart(tmp_path: Path) -> None:
    """C4 / FR-CRED-6: a session written by one process is readable by another.

    ``time.monotonic()`` is process-relative — its zero point does not
    survive process restarts. Storing a monotonic timestamp on disk and
    comparing it against a fresh process's ``time.monotonic()`` is
    meaningless. This test runs two separate Python processes against the
    same cache directory: process A writes a session with 60s of remaining
    life, process B reads it and must NOT see it as expired.
    """
    env = {**os.environ, auth_cache.ENV_CONFIG_DIR: str(tmp_path)}

    write_script = (
        "import time\n"
        "from kasa_cli import auth_cache\n"
        "auth_cache.save_session("
        "'AA:BB:CC:DD:EE:FF', "
        "{'_session_expire_at': time.monotonic() + 60, 'data': 'hello'})\n"
    )
    subprocess.run(
        [sys.executable, "-c", write_script],
        check=True,
        env=env,
    )

    read_script = (
        "from kasa_cli import auth_cache\n"
        "s = auth_cache.load_session('AA:BB:CC:DD:EE:FF')\n"
        "print('hit' if s and s.get('data') == 'hello' else 'miss')\n"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", read_script],
        env=env,
        text=True,
    ).strip()
    assert out == "hit"
