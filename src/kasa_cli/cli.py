"""Click-based CLI surface for kasa-cli (SRD §8).

Phase 1 implements:

* ``discover``  — broadcast probe both protocol families
* ``list``       — print configured aliases and groups
* ``info``       — show full state of one target
* ``on``  / ``off`` — power control with multi-socket gating
* ``config show`` / ``config validate`` — wired directly to ``config.effective_toml``
  and ``config.validate_config``
* ``auth status`` / ``auth flush`` — wired directly to ``auth_cache.list_sessions``
  / ``flush_all`` / ``flush_one``

The top-level group:

* maps every :class:`KasaCliError` subclass to its fixed exit code,
* installs SIGINT/SIGTERM handlers (Phase 1: just convert to exit 130/143;
  the full graceful-drain behavior of FR-31c is Phase 3),
* configures stderr JSON-line logging (`-v` → INFO, `-vv` → DEBUG, default WARNING),
* falls back to exit 1 with a generic StructuredError on uncaught exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import click

from kasa_cli import auth_cache
from kasa_cli.config import Config, effective_toml, load_config, validate_config
from kasa_cli.credentials import ENV_PASSWORD, ENV_USERNAME, resolve_credentials
from kasa_cli.errors import (
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    ConfigError,
    KasaCliError,
    StructuredError,
    UsageError,
)
from kasa_cli.output import OutputMode, detect_mode, emit_error, emit_stream
from kasa_cli.wrapper import CredentialBundle

# --- Common error envelopes ---------------------------------------------------


_ERROR_NAME_BY_TYPE: dict[type[KasaCliError], str] = {}


def _err_name(exc: KasaCliError) -> str:
    """Return the closed-enum SRD §11.2 ``error`` string for ``exc``."""
    # Lazy population so subclasses defined elsewhere still work.
    if not _ERROR_NAME_BY_TYPE:
        from kasa_cli.errors import (
            AuthError,
            DeviceError,
            NetworkError,
            NotFoundError,
            UnsupportedFeatureError,
        )
        from kasa_cli.errors import (
            ConfigError as ConfigErr,
        )
        from kasa_cli.errors import (
            UsageError as UsageErr,
        )

        _ERROR_NAME_BY_TYPE.update(
            {
                AuthError: "auth_failed",
                ConfigErr: "config_error",
                DeviceError: "device_error",
                NetworkError: "network_error",
                NotFoundError: "not_found",
                UnsupportedFeatureError: "unsupported_feature",
                UsageErr: "usage_error",
            }
        )
    for cls, name in _ERROR_NAME_BY_TYPE.items():
        if isinstance(exc, cls):
            return name
    return "device_error"


def _to_structured(exc: KasaCliError) -> StructuredError:
    return StructuredError(
        error=_err_name(exc),
        exit_code=exc.exit_code,
        target=exc.target,
        message=exc.message,
        hint=exc.hint,
    )


# --- Async runner with KasaCliError mapping -----------------------------------


def _run_async(
    coro_factory: Callable[[], Coroutine[Any, Any, int]],
    *,
    mode: OutputMode,
) -> int:
    """Run an async coroutine factory, mapping errors to exit codes.

    Signal handling: install handlers that flip a stop flag and re-raise as a
    :class:`KeyboardInterrupt` for SIGINT or :class:`SystemExit(143)` for
    SIGTERM. Phase 1 does not yet do graceful batch/group drain (FR-31c) —
    that is Phase 3. We just need the exit codes to be correct.
    """

    def _handle_sigint(*_args: object) -> None:
        raise KeyboardInterrupt

    def _handle_sigterm(*_args: object) -> None:
        raise SystemExit(EXIT_SIGTERM)

    prior_int = signal.getsignal(signal.SIGINT)
    prior_term = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGINT, _handle_sigint)
        # SIGTERM unsettable on Windows main thread; ignore quietly.
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGTERM, _handle_sigterm)

        try:
            return asyncio.run(coro_factory())
        except KeyboardInterrupt:
            return EXIT_SIGINT
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_SIGTERM
            return int(code)
        except KasaCliError as exc:
            emit_error(_to_structured(exc), mode)
            return exc.exit_code
        except Exception as exc:
            err = StructuredError(
                error="device_error",
                exit_code=1,
                target=None,
                message=f"Unhandled error: {type(exc).__name__}: {exc}",
                hint=None,
            )
            emit_error(err, mode)
            return 1
    finally:
        signal.signal(signal.SIGINT, prior_int)
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGTERM, prior_term)


# --- Config / credentials helpers (lazy A integration) ------------------------


def _resolve_credentials(
    source: str | None,
    config: Config | None = None,
    alias: str | None = None,
) -> CredentialBundle:
    """Resolve a CredentialBundle using ``credentials.resolve_credentials``.

    ``source`` is the value from ``--credential-source``:
    - ``none``: skip resolution; KLAP devices will fail with auth-required errors.
    - ``env``: only honor ``KASA_USERNAME`` / ``KASA_PASSWORD``; both must be set
      (matches A's resolver's "both or neither" invariant — R4).
    - ``file`` or ``None``: walk the full per-target → env → file chain.
    """
    if source == "none":
        return CredentialBundle()

    if source == "env":
        # Strict env-only path: do not consult the file resolver.
        # R4: both-or-neither — partial bundles are not produced.
        u = os.environ.get(ENV_USERNAME)
        p = os.environ.get(ENV_PASSWORD)
        if u and p:
            return CredentialBundle(username=u, password=p)
        return CredentialBundle()

    if config is None:
        try:
            config = load_config(None)
        except KasaCliError:
            raise
        except Exception as exc:
            raise ConfigError(
                f"Credential resolution failed: cannot load config: {exc}",
            ) from exc

    try:
        resolved = resolve_credentials(config, alias=alias)
    except KasaCliError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"Credential resolution failed: {exc}",
            hint=f"Check ~/.config/kasa-cli/credentials and {ENV_USERNAME}/{ENV_PASSWORD}.",
        ) from exc

    if resolved is None:
        return CredentialBundle()
    return CredentialBundle(username=resolved.username, password=resolved.password)


def _load_config(config_path: str | None) -> Config:
    """Load the active Config, converting a string CLI path to ``Path``.

    Click delivers ``--config`` as a string; ``config.load_config`` requires
    ``Path | None`` (it calls ``.exists()``). We coerce at the boundary so
    the entire codebase below works exclusively with ``Path``.
    """
    path: Path | None = Path(config_path).expanduser() if config_path else None
    return load_config(path)


_LOG_FORMAT: str = (
    '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
)


# Sentinel attribute on ``logging.getLogger("kasa_cli")`` recording the path of
# the currently-attached FileHandler. Lets ``_attach_file_logging`` detect a
# repeated invocation against the same path and skip duplicate-attach.
_FILE_HANDLER_SENTINEL: str = "_kasa_cli_file_handler_path"


def _log_formatter() -> logging.Formatter:
    """Return the canonical kasa-cli JSON-line formatter."""
    return logging.Formatter(_LOG_FORMAT)


def _configure_logging(verbose: int) -> None:
    """Wire ``-v`` / ``-vv`` to a stderr JSON-line StreamHandler (FR-39).

    Default is WARNING (silent on success), ``-v`` lifts to INFO, ``-vv`` to
    DEBUG. The handler emits one JSON-shaped line per record so log output
    is machine-parseable. Re-entrant safe — clears prior handlers if
    ``main()`` is invoked twice within the same process (tests do this).
    """
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_log_formatter())
    root = logging.getLogger("kasa_cli")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        # R2: FileHandlers own an open file descriptor — removing them from
        # the logger doesn't close the FD. Without this `h.close()` repeated
        # invocations of ``_configure_logging`` (tests, long-lived processes)
        # leak FDs proportional to the number of reconfigurations. Other
        # handler types are no-ops on close, so an unconditional best-effort
        # close is safe.
        if isinstance(h, logging.FileHandler):
            with contextlib.suppress(Exception):
                h.close()
    # Clear the file-handler sentinel — we just removed any FileHandler that
    # might have been attached previously, so a subsequent
    # ``_attach_file_logging`` call must not short-circuit.
    if hasattr(root, _FILE_HANDLER_SENTINEL):
        delattr(root, _FILE_HANDLER_SENTINEL)
    root.addHandler(handler)
    # Keep propagation enabled so test fixtures (caplog) still receive records.
    # The root logger has no default handler, so propagation is a no-op for
    # ordinary CLI use unless the user has installed one.
    root.propagate = True


def _attach_file_logging(cfg: Config | None) -> None:
    """Tee kasa-cli logs to ``cfg.logging.file`` when set (SRD §7.3).

    Idempotent across re-invocations: if a FileHandler is already attached for
    the same resolved path on the ``kasa_cli`` logger, nothing happens. If the
    path changed (tests re-run with a different ``[logging] file =``), the
    prior FileHandler is removed and a fresh one attached.

    Args:
        cfg: The active :class:`Config`. ``None`` or empty
            ``cfg.logging.file`` is a no-op (file logging disabled).
    """
    if cfg is None:
        return
    file_path_raw = getattr(cfg.logging, "file", None)
    if not file_path_raw:
        return
    path = Path(file_path_raw).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't create the parent dir, fall back to stderr-only logging
        # rather than crash the verb. The failure is loud enough at exit time.
        logging.getLogger("kasa_cli").warning(
            "could not create log directory for %s; file logging disabled",
            path,
        )
        return

    root = logging.getLogger("kasa_cli")
    existing = getattr(root, _FILE_HANDLER_SENTINEL, None)
    if existing == str(path):
        return  # idempotent — already attached for this path

    # Different path or no prior file handler — drop any stale FileHandler we
    # previously attached (StreamHandlers from ``_configure_logging`` are
    # untouched).
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and getattr(h, "_kasa_cli_owned", False):
            root.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()

    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(_log_formatter())
    handler._kasa_cli_owned = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    setattr(root, _FILE_HANDLER_SENTINEL, str(path))
    # SRD §7.3: an INFO line when the tee starts is useful for cron operators
    # tail-ing the file — it confirms the file is the live log destination
    # and stamps the run.
    root.info("file logging enabled at %s", path)


def _make_config_lookup(
    cfg: Config | None,
) -> Callable[[str], tuple[str | None, str | None]]:
    """Build a config_lookup closure expected by wrapper.resolve_target."""

    def lookup(target: str) -> tuple[str | None, str | None]:
        if cfg is None:
            # Degraded: only IPs (and any string the OS will resolve).
            return target, None
        # Try Config-style API first, then dict-style.
        for method in ("resolve_target", "lookup", "get_device"):
            fn = getattr(cfg, method, None)
            if callable(fn):
                result = fn(target)
                if result is None:
                    return target, None
                if isinstance(result, tuple) and len(result) == 2:
                    return result
                if hasattr(result, "ip"):
                    return getattr(result, "ip", None), getattr(result, "alias", None)
        # Plain dict-of-devices fallback. Supports both dict-shaped entries
        # (test fakes) and dataclass-shaped DeviceEntry (production Config).
        devices = getattr(cfg, "devices", None)
        if isinstance(devices, dict) and target in devices:
            entry = devices[target]
            ip = entry.get("ip") if isinstance(entry, dict) else getattr(entry, "ip", None)
            return ip, target
        return target, None

    return lookup


def _devices_section(cfg: Config | None) -> list[dict[str, Any]]:
    """Produce a list-of-dicts shaped {alias, ip, mac} from the Config."""
    if cfg is None:
        return []
    out: list[dict[str, Any]] = []
    for alias, entry in cfg.devices.items():
        out.append({"alias": alias, "ip": entry.ip, "mac": entry.mac})
    return out


# --- Click group --------------------------------------------------------------


@click.group(name="kasa-cli", invoke_without_command=False)
@click.option("--json", "json_flag", is_flag=True, default=False, help="Pretty JSON.")
@click.option("--jsonl", "jsonl_flag", is_flag=True, default=False, help="JSON-lines.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress stdout.")
@click.option("--timeout", "timeout", type=float, default=5.0, show_default=True)
@click.option("--config", "config_path", type=click.Path(dir_okay=False), default=None)
@click.option(
    "--credential-source",
    type=click.Choice(["env", "file", "none"]),
    default=None,
)
@click.option("-v", "verbose", count=True, help="-v / -vv stderr verbosity.")
@click.pass_context
def main(
    ctx: click.Context,
    *,
    json_flag: bool,
    jsonl_flag: bool,
    quiet: bool,
    timeout: float,
    config_path: str | None,
    credential_source: str | None,
    verbose: int,
) -> None:
    """``kasa-cli`` — deterministic local-LAN CLI for TP-Link Kasa devices."""
    if json_flag and jsonl_flag:
        # FR-33 / FR-34 are mutually exclusive in spirit. Fail fast.
        click.echo("error: --json and --jsonl are mutually exclusive", err=True)
        ctx.exit(EXIT_USAGE_ERROR)
    _configure_logging(verbose)
    # SRD §7.3: when ``[logging] file = <path>`` is set, tee the same JSON log
    # lines to that file. Loaded once here so the FileHandler is attached
    # before any verb runs. We deliberately swallow load errors at this stage —
    # verbs re-load and surface a structured error if the config is broken.
    with contextlib.suppress(Exception):
        _maybe_attach_file_logging(config_path)
    mode = detect_mode(json_flag=json_flag, jsonl_flag=jsonl_flag, quiet=quiet)
    ctx.obj = {
        "mode": mode,
        "timeout": timeout,
        "config_path": config_path,
        "credential_source": credential_source,
        "verbose": verbose,
    }


# --- discover -----------------------------------------------------------------


@main.command("discover")
@click.option(
    "--target-network",
    type=str,
    default=None,
    help="Directed-broadcast address (e.g. 192.168.1.255) for multi-NIC hosts.",
)
@click.pass_context
def discover_cmd(ctx: click.Context, *, target_network: str | None) -> None:
    """Broadcast probe both protocol families and print responders."""
    state = ctx.obj
    cfg = _load_config(state["config_path"])
    creds = _resolve_credentials(state["credential_source"], config=cfg)

    from kasa_cli.verbs.discover_cmd import run_discover

    code = _run_async(
        lambda: run_discover(
            timeout=state["timeout"],
            target_network=target_network,
            credentials=creds,
            mode=state["mode"],
        ),
        mode=state["mode"],
    )
    sys.exit(code)


# --- list ---------------------------------------------------------------------


@main.command("list")
@click.option("--probe", is_flag=True, default=False, help="Probe each device.")
@click.option(
    "--online-only",
    is_flag=True,
    default=False,
    help="Implies --probe; filter to live devices.",
)
@click.option("--groups", "groups_flag", is_flag=True, default=False)
@click.option("--concurrency", type=int, default=10, show_default=True)
@click.pass_context
def list_cmd(
    ctx: click.Context,
    *,
    probe: bool,
    online_only: bool,
    groups_flag: bool,
    concurrency: int,
) -> None:
    """Print configured aliases (and optional liveness)."""
    state = ctx.obj
    cfg = _load_config(state["config_path"])
    creds = _resolve_credentials(state["credential_source"], config=cfg)

    if groups_flag:
        # FR-7: groups listing. Phase-1 minimal: print group names + members.
        items: list[dict[str, Any]] = [
            {"name": k, "members": list(v)} for k, v in cfg.groups.items()
        ]

        def _fmt_group(g: object) -> str:
            assert isinstance(g, dict)
            members = g.get("members", [])
            return f"{g.get('name', '')}: " + ", ".join(members)

        emit_stream(items, state["mode"], formatter=_fmt_group)
        sys.exit(EXIT_SUCCESS)

    devices = _devices_section(cfg)

    from kasa_cli.verbs.list_cmd import run_list

    code = _run_async(
        lambda: run_list(
            devices_section=devices,
            probe=probe,
            online_only=online_only,
            credentials=creds,
            timeout=state["timeout"],
            concurrency=concurrency,
            mode=state["mode"],
        ),
        mode=state["mode"],
    )
    sys.exit(code)


# --- info ---------------------------------------------------------------------


@main.command("info")
@click.argument("target", type=str)
@click.pass_context
def info_cmd(ctx: click.Context, *, target: str) -> None:
    """Show full live state of one target."""
    state = ctx.obj
    cfg = _load_config(state["config_path"])
    creds = _resolve_credentials(state["credential_source"], config=cfg, alias=target)

    from kasa_cli.verbs.info_cmd import run_info

    code = _run_async(
        lambda: run_info(
            target=target,
            config_lookup=_make_config_lookup(cfg),
            credentials=creds,
            timeout=state["timeout"],
            mode=state["mode"],
        ),
        mode=state["mode"],
    )
    sys.exit(code)


# --- on / off -----------------------------------------------------------------


def _onoff_command(action: str) -> Callable[..., None]:
    @click.argument("target", type=str)
    @click.option("--socket", "socket_arg", type=str, default=None)
    @click.pass_context
    def _impl(ctx: click.Context, *, target: str, socket_arg: str | None) -> None:
        state = ctx.obj
        cfg = _load_config(state["config_path"])
        creds = _resolve_credentials(state["credential_source"], config=cfg, alias=target)

        from kasa_cli.verbs.onoff import run_onoff

        code = _run_async(
            lambda: run_onoff(
                action=action,  # type: ignore[arg-type]
                target=target,
                socket_arg=socket_arg,
                config_lookup=_make_config_lookup(cfg),
                credentials=creds,
                timeout=state["timeout"],
                mode=state["mode"],
            ),
            mode=state["mode"],
        )
        sys.exit(code)

    return _impl


main.command("on", help="Turn the device on.")(_onoff_command("on"))
main.command("off", help="Turn the device off.")(_onoff_command("off"))


# --- toggle (Phase 2) ---------------------------------------------------------


@main.command("toggle", help="Flip the current on/off state of the device.")
@click.argument("target", type=str)
@click.option(
    "--socket",
    "socket_arg",
    type=str,
    default=None,
    help="Socket index (1-based) or 'all' for multi-socket strips.",
)
@click.pass_context
def toggle_cmd(ctx: click.Context, *, target: str, socket_arg: str | None) -> None:
    state = ctx.obj
    cfg = _load_config(state["config_path"])
    creds = _resolve_credentials(state["credential_source"], config=cfg, alias=target)

    from kasa_cli.verbs.toggle_cmd import run_toggle

    code = _run_async(
        lambda: run_toggle(
            target=target,
            socket_arg=socket_arg,
            config_lookup=_make_config_lookup(cfg),
            credentials=creds,
            timeout=state["timeout"],
            mode=state["mode"],
        ),
        mode=state["mode"],
    )
    sys.exit(code)


# --- set (Phase 2) ------------------------------------------------------------


class _Exit64UsageError(click.UsageError):
    """Click ``UsageError`` subclass that exits with SRD-mandated 64, not 2.

    The base ``click.UsageError`` defaults to ``exit_code = 2``; SRD §11.1
    FR-20 requires usage errors (mutex flag violations, out-of-range values,
    missing required args) to exit ``64`` so they're distinguishable from
    auth failures (exit 2). Both ``CliRunner`` (test path, ``standalone_mode
    =True``) and the production ``__main__`` shim consult ``exc.exit_code``
    when translating the exception, so this single subclass fixes both paths.
    """

    exit_code = 64


def _validate_color_flag_exclusion(
    ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> str | None:
    """Click callback: enforce mutual exclusion of --hsv / --hex / --color.

    Click invokes callbacks left-to-right as flags appear on the command line.
    We stash each color-flag we see in ``ctx.meta`` and raise our own
    :class:`_Exit64UsageError` on the second occurrence so the exit code is the
    SRD-mandated ``64`` (FR-20). The default ``click.UsageError`` would exit 2,
    which collides with the auth-failure exit code.
    """
    if value is None:
        return value
    # Map Python attribute names back to the user-facing CLI flag name.
    # ``--hex`` is bound to ``hex_color`` and ``--color`` to ``color_name``;
    # the rest match (e.g. ``hsv`` ↔ ``--hsv``).
    flag_name_for_attr = {
        "hsv": "hsv",
        "hex_color": "hex",
        "color_name": "color",
    }
    seen = ctx.meta.setdefault("_set_color_flags", [])
    seen.append(param.name)
    if len(seen) > 1:
        first = flag_name_for_attr.get(seen[0], seen[0].replace("_", "-"))
        last = flag_name_for_attr.get(seen[-1], seen[-1].replace("_", "-"))
        raise _Exit64UsageError(
            f"--hsv, --hex, and --color are mutually exclusive; got both --{first} and --{last}"
        )
    return value


def _validate_brightness_range(
    ctx: click.Context,
    param: click.Parameter,
    value: int | None,
) -> int | None:
    """Click callback: bound ``--brightness`` to [0, 100] with exit 64.

    ``click.IntRange`` would reject out-of-range values with exit 2 (Click's
    default ``UsageError``). FR-20 requires exit 64 for usage errors, so we
    accept a plain ``int`` and validate here, raising :class:`_Exit64UsageError`
    on violation. ``ctx`` and ``param`` are unused but required by the Click
    callback signature.
    """
    del ctx, param
    if value is None:
        return None
    if value < 0 or value > 100:
        raise _Exit64UsageError(f"--brightness must be in [0, 100]; got {value}")
    return value


@main.command("set", help="Adjust brightness, color, or color-temperature.")
@click.argument("target", type=str)
@click.option(
    "--brightness",
    type=int,
    default=None,
    callback=_validate_brightness_range,
    help="Brightness percent (0-100). Requires a dimmable device.",
)
@click.option(
    "--color-temp",
    "color_temp",
    type=int,
    default=None,
    help="Color temperature in Kelvin. Requires a tunable-white device.",
)
@click.option(
    "--hsv",
    "hsv",
    type=str,
    default=None,
    callback=_validate_color_flag_exclusion,
    help="Color as 'H,S,V' (e.g. 240,100,100). Mutually exclusive with --hex/--color.",
)
@click.option(
    "--hex",
    "hex_color",
    type=str,
    default=None,
    callback=_validate_color_flag_exclusion,
    help="Color as #rrggbb hex. Mutually exclusive with --hsv/--color.",
)
@click.option(
    "--color",
    "color_name",
    type=str,
    default=None,
    callback=_validate_color_flag_exclusion,
    help="Named color (red, blue, warm-white, ...). Mutually exclusive with --hsv/--hex.",
)
@click.option(
    "--socket",
    "socket_arg",
    type=str,
    default=None,
    help="Socket index (1-based) or 'all' for multi-socket strips.",
)
@click.pass_context
def set_cmd(
    ctx: click.Context,
    *,
    target: str,
    brightness: int | None,
    color_temp: int | None,
    hsv: str | None,
    hex_color: str | None,
    color_name: str | None,
    socket_arg: str | None,
) -> None:
    state = ctx.obj
    cfg = _load_config(state["config_path"])
    creds = _resolve_credentials(state["credential_source"], config=cfg, alias=target)

    from kasa_cli.verbs.set_cmd import run_set

    code = _run_async(
        lambda: run_set(
            target=target,
            brightness=brightness,
            color_temp=color_temp,
            hsv=hsv,
            hex_color=hex_color,
            color_name=color_name,
            socket_arg=socket_arg,
            config_lookup=_make_config_lookup(cfg),
            credentials=creds,
            timeout=state["timeout"],
            mode=state["mode"],
        ),
        mode=state["mode"],
    )
    sys.exit(code)


# --- config show / config validate --------------------------------------------


@main.group("config")
def config_group() -> None:
    """Configuration sub-verbs."""


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Print the effective resolved config in TOML."""
    state = ctx.obj
    try:
        cfg = _load_config(state["config_path"])
    except KasaCliError as exc:
        emit_error(_to_structured(exc), state["mode"])
        sys.exit(exc.exit_code)
    click.echo(effective_toml(cfg), nl=False)
    sys.exit(EXIT_SUCCESS)


@config_group.command("validate")
@click.argument("path", required=False, type=click.Path(dir_okay=False))
@click.pass_context
def config_validate(ctx: click.Context, *, path: str | None) -> None:
    """Validate a config file and exit 0 (ok) or 6 (error)."""
    state = ctx.obj
    candidate = path or state["config_path"]
    if candidate is None:
        err = StructuredError(
            error="usage_error",
            exit_code=EXIT_USAGE_ERROR,
            target=None,
            message="config validate requires a path (positional or --config).",
        )
        emit_error(err, state["mode"])
        sys.exit(EXIT_USAGE_ERROR)
    try:
        validate_config(Path(candidate).expanduser())
        sys.exit(EXIT_SUCCESS)
    except KasaCliError as exc:
        emit_error(_to_structured(exc), state["mode"])
        sys.exit(exc.exit_code)


# --- auth status / auth flush -------------------------------------------------


@main.group("auth")
def auth_group() -> None:
    """Authentication / session-cache sub-verbs."""


def _session_metadata_to_dict(meta: auth_cache.SessionMetadata) -> dict[str, Any]:
    """Project a SessionMetadata row into a JSON-emittable dict."""
    return {
        "mac": meta.mac,
        "path": str(meta.path),
        "mtime_epoch": meta.mtime_epoch,
        "bytes_size": meta.bytes_size,
        "expires_at_monotonic": meta.expires_at_monotonic,
    }


def _session_metadata_to_text(meta: object) -> str:
    """One-line text rendering of a SessionMetadata row."""
    if isinstance(meta, dict):
        return f"{meta.get('mac', '-')}  {meta.get('path', '-')}  {meta.get('bytes_size', 0)}B"
    return str(meta)


@auth_group.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Print cached KLAP session metadata (one line per device)."""
    rows = [_session_metadata_to_dict(m) for m in auth_cache.list_sessions()]
    emit_stream(rows, ctx.obj["mode"], formatter=_session_metadata_to_text)
    sys.exit(EXIT_SUCCESS)


@auth_group.command("flush")
@click.option("--target", type=str, default=None)
@click.pass_context
def auth_flush(ctx: click.Context, *, target: str | None) -> None:
    """Delete all (or one device's) cached KLAP session state."""
    if target is not None:
        deleted = 1 if auth_cache.flush_one(target) else 0
    else:
        deleted = auth_cache.flush_all()
    click.echo(f"flushed {deleted} session file(s)")
    sys.exit(EXIT_SUCCESS)


# --- Phase 2 Engineer B additions ---------------------------------------------
#
# This block adds the ``energy`` verb, ``schedule list`` verb, and the
# ``_maybe_attach_file_logging`` helper for the runtime log-file tee. It is
# intentionally appended in a delimited section so the PM merge with Engineer
# A's color/light-control verbs is mechanical.


def _maybe_attach_file_logging(config_path: str | None) -> None:
    """Load config (best-effort) and call :func:`_attach_file_logging`.

    Used by ``main()`` to wire the SRD §7.3 file-logging tee before any verb
    runs. A broken config here is suppressed — verbs themselves load the
    config and surface structured errors. We just want the FileHandler in
    place if the config loads cleanly.
    """
    try:
        cfg = _load_config(config_path)
    except KasaCliError:
        return
    _attach_file_logging(cfg)


# --- energy -------------------------------------------------------------------


@main.command("energy")
@click.argument("target", type=str)
@click.option(
    "--socket",
    "socket_arg",
    type=int,
    default=None,
    help="1-indexed socket on a multi-socket strip (HS300).",
)
@click.option(
    "--watch",
    "watch_seconds",
    type=float,
    default=None,
    help=(
        "Seconds between ticks. JSONL stream of Reading objects. Sub-second "
        "values supported (e.g. --watch 0.5)."
    ),
)
@click.option(
    "--cumulative/--no-cumulative",
    "cumulative_flag",
    default=None,
    help=(
        "Include today_kwh / month_kwh in the Reading. Default: omit under "
        "--watch (FR-22), include for single-shot reads."
    ),
)
@click.pass_context
def energy_cmd(
    ctx: click.Context,
    *,
    target: str,
    socket_arg: int | None,
    watch_seconds: float | None,
    cumulative_flag: bool | None,
) -> None:
    """Emit a Reading (FR-21) or a JSONL stream (--watch, FR-22)."""
    state = ctx.obj
    cfg = _load_config(state["config_path"])
    creds = _resolve_credentials(state["credential_source"], config=cfg, alias=target)

    # FR-22 default: --watch implies --no-cumulative; single-shot includes.
    cumulative = (watch_seconds is None) if cumulative_flag is None else cumulative_flag

    from kasa_cli.verbs.energy_cmd import run_energy

    code = _run_async(
        lambda: run_energy(
            target=target,
            watch_seconds=watch_seconds,
            cumulative=cumulative,
            socket=socket_arg,
            config_lookup=_make_config_lookup(cfg),
            credentials=creds,
            timeout=state["timeout"],
            mode=state["mode"],
        ),
        mode=state["mode"],
    )
    sys.exit(code)


# --- schedule list ------------------------------------------------------------


@main.group("schedule")
def schedule_group() -> None:
    """Read-only schedule sub-verbs (legacy IOT only).

    Only ``list`` is exposed in v1. ``add`` / ``remove`` / ``edit`` are out of
    scope FOREVER per SRD §5.7 FR-25 — schedules belong in cron / systemd /
    launchd, not in the device. Smart/KLAP devices return exit 5 (FR-24a).
    """


@schedule_group.command("list")
@click.argument("target", type=str)
@click.pass_context
def schedule_list_cmd(ctx: click.Context, *, target: str) -> None:
    """List device-stored schedule rules (legacy IOT only)."""
    state = ctx.obj
    cfg = _load_config(state["config_path"])
    creds = _resolve_credentials(state["credential_source"], config=cfg, alias=target)

    from kasa_cli.verbs.schedule_cmd import run_schedule_list

    code = _run_async(
        lambda: run_schedule_list(
            target=target,
            config_lookup=_make_config_lookup(cfg),
            credentials=creds,
            timeout=state["timeout"],
            mode=state["mode"],
        ),
        mode=state["mode"],
    )
    sys.exit(code)


__all__ = ["UsageError", "main"]
