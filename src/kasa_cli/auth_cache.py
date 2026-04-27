"""KLAP session-state cache (SRD §6.4).

Persists the per-device KLAP session state derived by python-kasa
(``KlapTransport``) so subsequent invocations can skip the handshake when
the session is still inside its ``_session_expire_at`` window.

Per-FR contracts honored here:

- **FR-CRED-4** Cache files at ``~/.config/kasa-cli/.tokens/<mac>.json`` with
  chmod 0600. The directory is created with chmod 0700 on first use.
- **FR-CRED-5** State is opaque dict; the wrapper layer (Engineer B) is
  responsible for shaping it back into ``KlapTransport``.
- **FR-CRED-6** A cache entry whose ``_session_expire_at`` is in the past
  reads as a miss — we never return stale state.
- **FR-CRED-7** ``flush_one`` exists so the auth retry path can invalidate
  exactly one device's cache.
- **FR-CRED-8** ``flush_all`` / ``flush_one`` back the ``auth flush`` verb.
- **FR-CRED-10** Per-device advisory lock via ``flock``. Reads do NOT take
  the lock; only writes and the auth-renew path do. Atomic writes via
  tmpfile + ``fsync`` + rename.
- **FR-CRED-11** ``list_sessions`` exposes per-cache metadata for ``auth
  status``. Alias resolution is the caller's job.

Test override: set ``KASA_CLI_CONFIG_DIR`` to redirect the cache root to a
tmp path. This is intentionally a documented hatch — :func:`cache_dir`
honours it before falling back to ``~/.config/kasa-cli``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from kasa_cli.errors import ConfigError, NetworkError

logger = logging.getLogger("kasa_cli")


ENV_CONFIG_DIR: Final[str] = "KASA_CLI_CONFIG_DIR"

CONFIG_DIR_DEFAULT: Final[Path] = Path("~/.config/kasa-cli").expanduser()
TOKENS_SUBDIR: Final[str] = ".tokens"

# Mode bits we enforce.
DIR_MODE: Final[int] = 0o700
FILE_MODE: Final[int] = 0o600

# In-memory key (monotonic clock) — what python-kasa's KlapTransport carries
# in its session state. Callers (the wrapper layer) read/write this value.
EXPIRE_KEY: Final[str] = "_session_expire_at"

# On-disk key (wall-clock seconds since UNIX epoch). ``time.monotonic()`` is
# process-relative — its zero point does not survive process restarts — so we
# CANNOT persist a monotonic value across CLI invocations. ``save_session``
# translates monotonic→wall-clock on write; ``load_session`` translates back
# to a fresh monotonic on read so the wrapper layer keeps using the same key.
# (FR-CRED-6.)
EXPIRE_KEY_WALLCLOCK: Final[str] = "_session_expire_at_wallclock"

# In-process advisory mutex for tests on platforms where fcntl.flock against
# a file held by *the same process* will not block. The OS-level flock still
# protects against external processes; this just makes "two threads in the
# same Python process race for the lock" deterministic.
_PROCESS_LOCK_REGISTRY: dict[Path, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    """One row of ``auth status`` output (FR-CRED-11).

    Alias resolution is the caller's responsibility — we expose only what
    the cache layer can know without touching the config.
    """

    mac: str
    path: Path
    mtime_epoch: float
    bytes_size: int
    expires_at_monotonic: float | None
    """Raw monotonic-clock value as stored. Caller translates to wall-clock."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """Honour ``KASA_CLI_CONFIG_DIR`` for tests; fall back to ``~/.config/kasa-cli``."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    return CONFIG_DIR_DEFAULT


def cache_dir() -> Path:
    """Return the token cache directory, creating it with chmod 0700 if needed.

    Idempotent. Re-tightens the mode if the directory already exists with
    permissive bits.
    """
    base = _config_dir()
    tokens = base / TOKENS_SUBDIR

    base.mkdir(parents=True, exist_ok=True)
    tokens.mkdir(parents=True, exist_ok=True)

    # Tighten mode whether we created it or not — defensive against operator
    # error or shared filesystems.
    try:
        os.chmod(tokens, DIR_MODE)
    except OSError as exc:
        # On filesystems that ignore chmod (rare, e.g., some FUSE setups),
        # keep going — the file-level chmod still narrows access.
        logger.debug("could not chmod %s to 0700: %s", tokens, exc)

    return tokens


def cache_path_for_mac(mac: str) -> Path:
    """Return the canonical cache file path for a MAC.

    Normalizes the MAC to uppercase colon-form so casing variations don't
    produce duplicate cache entries.
    """
    normalized = _normalize_mac(mac)
    return cache_dir() / f"{normalized}.json"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def save_session(mac: str, state: dict[str, Any]) -> None:
    """Persist ``state`` to the per-MAC cache file atomically.

    Writes to a sibling tempfile, fsyncs, then renames into place. The final
    file gets ``0o600``. Caller MUST hold :func:`lock_for_write` for the same
    MAC for the duration of the surrounding read-modify-write transaction
    (FR-CRED-10).

    On-disk schema: any incoming ``_session_expire_at`` (monotonic seconds —
    python-kasa's native form) is translated to a wall-clock UNIX timestamp
    stored under :data:`EXPIRE_KEY_WALLCLOCK`. The monotonic key is removed
    from the persisted form. ``load_session`` reverses the translation so
    callers continue to receive the monotonic key they expect.
    """
    target = cache_path_for_mac(mac)
    target.parent.mkdir(parents=True, exist_ok=True)

    on_disk = _to_disk_form(dict(state))

    tmp_fd, tmp_path_str = _make_tempfile(target)
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(on_disk, fh, separators=(",", ":"), sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, FILE_MODE)
        os.replace(tmp_path, target)
    except BaseException:
        # Best-effort cleanup of the tempfile if anything went wrong before rename.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def load_session(mac: str) -> dict[str, Any] | None:
    """Return the cached session dict, or ``None`` for absent/expired entries.

    Reads the wall-clock expiry from disk and treats the entry as a miss when
    the wall-clock has already passed. The returned dict carries a fresh
    monotonic ``_session_expire_at`` value (computed from ``time.monotonic()``
    plus the remaining wall-clock lifetime) so the wrapper layer can hand it
    to ``KlapTransport`` unchanged. Reads do NOT take the per-device lock.
    """
    path = cache_path_for_mac(mac)
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("auth_cache: read failed for %s: %s", path, exc)
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("auth_cache: dropping malformed cache file %s: %s", path, exc)
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    if not isinstance(payload, dict):
        logger.warning("auth_cache: dropping non-object cache file %s", path)
        with contextlib.suppress(OSError):
            path.unlink()
        return None

    return _from_disk_form(payload, path)


def _to_disk_form(state: dict[str, Any]) -> dict[str, Any]:
    """Translate an in-memory session dict to its on-disk form.

    Removes the monotonic ``_session_expire_at`` key and replaces it with
    a wall-clock ``_session_expire_at_wallclock`` (UNIX seconds float). A
    non-numeric monotonic value is preserved as an already-expired wall-clock
    sentinel (``0.0``) so the on-disk file deterministically reads as a miss
    and round-trip semantics are maintained.
    """
    mono = state.pop(EXPIRE_KEY, None)
    if mono is not None:
        try:
            mono_f = float(mono)
        except (TypeError, ValueError):
            # Already-expired sentinel — treat the cache entry as a miss on
            # next read rather than silently dropping the (broken) expiry.
            state[EXPIRE_KEY_WALLCLOCK] = 0.0
        else:
            wall = time.time() + (mono_f - time.monotonic())
            state[EXPIRE_KEY_WALLCLOCK] = wall
    return state


def _from_disk_form(payload: dict[str, Any], path: Path) -> dict[str, Any] | None:
    """Translate an on-disk session dict to its in-memory form.

    Reads :data:`EXPIRE_KEY_WALLCLOCK`; if expired (wall <= ``time.time()``),
    returns ``None``. Otherwise returns the payload with a freshly-computed
    monotonic ``_session_expire_at`` so the wrapper sees the key it expects.

    A payload that lacks both the wall-clock and the legacy monotonic key
    is returned as-is (the wrapper-only test fixtures rely on this).
    """
    if EXPIRE_KEY_WALLCLOCK in payload:
        wall_raw = payload.get(EXPIRE_KEY_WALLCLOCK)
        try:
            wall = float(wall_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning(
                "auth_cache: %s has non-numeric %s=%r; treating as miss",
                path,
                EXPIRE_KEY_WALLCLOCK,
                wall_raw,
            )
            return None
        now_wall = time.time()
        if wall <= now_wall:
            logger.debug(
                "auth_cache: %s expired wallclock=%.2f now=%.2f",
                path,
                wall,
                now_wall,
            )
            return None
        # Translate back to a fresh monotonic value the wrapper expects.
        out = {k: v for k, v in payload.items() if k != EXPIRE_KEY_WALLCLOCK}
        out[EXPIRE_KEY] = time.monotonic() + (wall - now_wall)
        return out

    # Legacy / no-expiry path: still honor a stored monotonic value if a
    # caller writes one directly (some tests do). Same semantics as before.
    expire = payload.get(EXPIRE_KEY)
    if expire is not None:
        try:
            expire_f = float(expire)
        except (TypeError, ValueError):
            logger.warning(
                "auth_cache: %s has non-numeric %s=%r; treating as miss",
                path,
                EXPIRE_KEY,
                expire,
            )
            return None
        if expire_f <= time.monotonic():
            logger.debug(
                "auth_cache: %s expired (%.2f <= now=%.2f)",
                path,
                expire_f,
                time.monotonic(),
            )
            return None

    return payload


# ---------------------------------------------------------------------------
# Flush / list
# ---------------------------------------------------------------------------


def flush_all() -> int:
    """Delete every cached session. Returns count of files removed."""
    base = cache_dir()
    removed = 0
    for entry in base.iterdir():
        if entry.is_file() and entry.suffix == ".json":
            try:
                entry.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("auth_cache: could not unlink %s: %s", entry, exc)
    return removed


def flush_one(mac: str) -> bool:
    """Delete exactly one device's cached session. Returns True on hit."""
    path = cache_path_for_mac(mac)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("auth_cache: could not unlink %s: %s", path, exc)
        return False


def list_sessions() -> list[SessionMetadata]:
    """Enumerate every cache file as :class:`SessionMetadata`.

    Files that fail to parse are silently skipped — ``auth status`` should
    never crash on a corrupted cache entry. The
    :attr:`SessionMetadata.expires_at_monotonic` field is computed from the
    on-disk wall-clock value so callers continue to see a monotonic-shaped
    timestamp consistent with what :func:`load_session` returns.
    """
    base = cache_dir()
    out: list[SessionMetadata] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        mac = entry.stem
        try:
            stat_info = entry.stat()
        except OSError as exc:
            logger.debug("auth_cache: stat failed on %s: %s", entry, exc)
            continue
        expire: float | None = None
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                if EXPIRE_KEY_WALLCLOCK in payload:
                    with contextlib.suppress(TypeError, ValueError):
                        wall = float(payload[EXPIRE_KEY_WALLCLOCK])
                        # Translate wall-clock back to a monotonic-shaped
                        # value: now_mono + (wall - now_wall) preserves the
                        # original monotonic delta within process scheduling
                        # jitter (microseconds — well under the 1ms test
                        # tolerance).
                        expire = time.monotonic() + (wall - time.time())
                elif EXPIRE_KEY in payload:
                    # Legacy on-disk monotonic value (test fixtures, or older
                    # cache files written before the wall-clock migration).
                    with contextlib.suppress(TypeError, ValueError):
                        expire = float(payload[EXPIRE_KEY])
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("auth_cache: could not parse %s: %s", entry, exc)
        out.append(
            SessionMetadata(
                mac=mac,
                path=entry,
                mtime_epoch=stat_info.st_mtime,
                bytes_size=stat_info.st_size,
                expires_at_monotonic=expire,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Per-device lock (FR-CRED-10)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def lock_for_write(mac: str, timeout: float) -> Iterator[None]:
    """Acquire an advisory write lock for one device.

    Implemented with ``fcntl.flock`` on a sibling lockfile so we don't have
    to truncate the actual cache file just to lock it. A polling loop
    enforces the ``timeout`` (``flock`` itself has no portable timeout).

    A second concurrent invocation that fails to acquire within ``timeout``
    raises :class:`NetworkError` with exit code 3 per FR-CRED-10.

    Reads do NOT take this lock — only writes and the auth-renew path do.
    """
    if timeout < 0:
        raise ConfigError(
            f"lock_for_write timeout must be >= 0, got {timeout}",
        )

    cache = cache_dir()
    lock_path = cache / f"{_normalize_mac(mac)}.lock"

    # Per-process registry mutex first, so two threads in the same process
    # serialize cleanly on platforms where flock against the same file
    # descriptor in the same process is a no-op.
    proc_lock = _get_proc_lock(lock_path)
    proc_lock_acquired = proc_lock.acquire(timeout=timeout if timeout > 0 else 0.0001)
    if not proc_lock_acquired:
        raise NetworkError(
            f"timed out waiting for cache lock on {mac} after {timeout:.2f}s",
            hint="Another kasa-cli invocation is mid-handshake. Retry shortly.",
            target=mac,
            extra={"mac": mac, "timeout_seconds": timeout},
        )

    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with contextlib.suppress(OSError):
            os.chmod(lock_path, FILE_MODE)

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise NetworkError(
                        f"timed out waiting for cache lock on {mac} after {timeout:.2f}s",
                        hint=("Another kasa-cli invocation is mid-handshake. Retry shortly."),
                        target=mac,
                        extra={"mac": mac, "timeout_seconds": timeout},
                    ) from exc
                time.sleep(0.05)

        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        proc_lock.release()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_mac(mac: str) -> str:
    """Uppercase colon-form. Accepts ``aa-bb-cc-dd-ee-ff`` or ``aabbccddeeff`` too.

    Does not validate beyond shape — invalid MACs become invalid filenames,
    which the FS rejects.
    """
    cleaned = mac.replace("-", "").replace(":", "").strip().upper()
    if len(cleaned) != 12:
        # Don't raise — let the caller produce the typed error. We just
        # return the sanitized form so the caller's error message has a
        # predictable target.
        return cleaned
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def _make_tempfile(target: Path) -> tuple[int, str]:
    """Create a same-directory tempfile for atomic rename. Returns (fd, path)."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    # Some platforms (notably tmpfs in containers) reject fchmod —
    # we'll chmod by path after fdopen close in save_session.
    with contextlib.suppress(OSError):
        os.fchmod(fd, FILE_MODE)
    return fd, path


def _get_proc_lock(path: Path) -> threading.Lock:
    """Return a process-local mutex unique to this lockfile path."""
    with _REGISTRY_LOCK:
        if path not in _PROCESS_LOCK_REGISTRY:
            _PROCESS_LOCK_REGISTRY[path] = threading.Lock()
        return _PROCESS_LOCK_REGISTRY[path]


def _current_dir_mode(path: Path) -> int:
    """For tests — return the current permission bits on a directory."""
    return stat.S_IMODE(path.stat().st_mode)


__all__ = [
    "DIR_MODE",
    "ENV_CONFIG_DIR",
    "EXPIRE_KEY",
    "EXPIRE_KEY_WALLCLOCK",
    "FILE_MODE",
    "TOKENS_SUBDIR",
    "SessionMetadata",
    "cache_dir",
    "cache_path_for_mac",
    "flush_all",
    "flush_one",
    "list_sessions",
    "load_session",
    "lock_for_write",
    "save_session",
]
