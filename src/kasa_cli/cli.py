"""Click-based CLI surface for kasa-cli (SRD §8).

Phase 1 implements:

* ``discover``  — broadcast probe both protocol families
* ``list``       — print configured aliases and groups
* ``info``       — show full state of one target
* ``on``  / ``off`` — power control with multi-socket gating
* ``config show`` / ``config validate`` — stubs that delegate to Engineer A's
  ``config.py`` (lazy-imported); if A's module is not present we exit 64
  with a clear message
* ``auth status`` / ``auth flush`` — stubs that delegate to Engineer A's
  ``auth_cache.py``; same lazy-import behavior

The top-level group:

* maps every :class:`KasaCliError` subclass to its fixed exit code,
* installs SIGINT/SIGTERM handlers (Phase 1: just convert to exit 130/143;
  the full graceful-drain behavior of FR-31c is Phase 3),
* falls back to exit 1 with a generic StructuredError on uncaught exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import signal
import sys
from collections.abc import Callable, Coroutine
from typing import Any

import click

from kasa_cli.errors import (
    EXIT_OK,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_USAGE,
    ConfigError,
    KasaCliError,
    StructuredError,
    UsageError,
)
from kasa_cli.output import OutputMode, detect_mode, emit_error
from kasa_cli.wrapper import CredentialBundle

# --- Lazy imports of Engineer A's modules -------------------------------------


def _import_optional(name: str) -> Any | None:
    """Return ``importlib.import_module`` result, or None if missing.

    Used so this CLI can compile and run smoke tests before Engineer A's
    branch is merged. Production runs should always have these modules
    present; the merge-time PM step asserts it.
    """
    try:
        return importlib.import_module(f"kasa_cli.{name}")
    except ImportError:
        return None


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
            UnsupportedError,
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
                UnsupportedError: "unsupported_feature",
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


def _resolve_credentials(source: str | None) -> CredentialBundle:
    """Resolve a CredentialBundle using Engineer A's resolver if present.

    ``source`` is the value from ``--credential-source`` (env / file / none).
    When Engineer A's module is not yet merged, we honor only ``env`` and
    ``none`` so the CLI remains usable for the legacy-protocol path.
    """
    if source == "none":
        return CredentialBundle()
    creds_mod = _import_optional("credentials")
    if creds_mod is not None and hasattr(creds_mod, "resolve_credentials"):
        try:
            resolved = creds_mod.resolve_credentials(source=source)
        except Exception as exc:
            raise ConfigError(
                f"Credential resolution failed: {exc}",
                hint="Check ~/.config/kasa-cli/credentials and KASA_USERNAME/KASA_PASSWORD.",
            ) from exc
        if isinstance(resolved, CredentialBundle):
            return resolved
        if isinstance(resolved, dict):
            return CredentialBundle(
                username=resolved.get("username"),
                password=resolved.get("password"),
            )
        if hasattr(resolved, "username") and hasattr(resolved, "password"):
            return CredentialBundle(
                username=getattr(resolved, "username", None),
                password=getattr(resolved, "password", None),
            )
        raise ConfigError(
            "credentials.resolve_credentials returned an unexpected shape",
        )

    # Fallback: env vars only.
    import os

    return CredentialBundle(
        username=os.environ.get("KASA_USERNAME"),
        password=os.environ.get("KASA_PASSWORD"),
    )


def _load_config(config_path: str | None) -> Any | None:
    """Load Engineer A's Config object, or return None.

    The CLI keeps working in degraded mode (no aliases, no groups) when the
    config module is absent; targets must then be IPs.
    """
    cfg_mod = _import_optional("config")
    if cfg_mod is None:
        return None
    if hasattr(cfg_mod, "load_config"):
        return cfg_mod.load_config(config_path)
    if hasattr(cfg_mod, "Config") and hasattr(cfg_mod.Config, "load"):
        return cfg_mod.Config.load(config_path)
    return None


def _make_config_lookup(
    cfg: Any | None,
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
        # Plain dict-of-devices fallback.
        devices = getattr(cfg, "devices", None)
        if isinstance(devices, dict) and target in devices:
            entry = devices[target]
            return entry.get("ip"), target
        return target, None

    return lookup


def _devices_section(cfg: Any | None) -> list[dict[str, Any]]:
    """Produce a list-of-dicts shaped {alias, ip, mac} from the Config."""
    if cfg is None:
        return []
    devices = getattr(cfg, "devices", None)
    if isinstance(devices, dict):
        out: list[dict[str, Any]] = []
        for alias, entry in devices.items():
            if isinstance(entry, dict):
                out.append(
                    {
                        "alias": alias,
                        "ip": entry.get("ip"),
                        "mac": entry.get("mac"),
                    }
                )
            else:
                out.append(
                    {
                        "alias": alias,
                        "ip": getattr(entry, "ip", None),
                        "mac": getattr(entry, "mac", None),
                    }
                )
        return out
    return []


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
        ctx.exit(EXIT_USAGE)
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
    creds = _resolve_credentials(state["credential_source"])

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
    creds = _resolve_credentials(state["credential_source"])

    if groups_flag:
        # FR-7: groups listing. Phase-1 minimal: print group names + members.
        groups = getattr(cfg, "groups", None) or {}
        from kasa_cli.output import emit_stream as _emit_stream

        items: list[dict[str, Any]] = [{"name": k, "members": list(v)} for k, v in groups.items()]

        def _fmt_group(g: object) -> str:
            assert isinstance(g, dict)
            members = g.get("members", [])
            return f"{g.get('name', '')}: " + ", ".join(members)

        _emit_stream(items, state["mode"], formatter=_fmt_group)
        sys.exit(EXIT_OK)

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
    creds = _resolve_credentials(state["credential_source"])

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
        creds = _resolve_credentials(state["credential_source"])

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


# --- config / auth sub-verb helpers -------------------------------------------


def _require_engineer_a_attr(
    ctx: click.Context,
    *,
    module_name: str,
    attr_name: str,
    sub_verb: str,
) -> Any:
    """Lazy-load Engineer A's module and return ``attr_name``.

    If the module isn't merged yet (or doesn't yet expose the attribute), emit
    a structured ``unsupported_feature`` error and exit 64. Centralizing this
    keeps the four sub-verbs (config show/validate, auth status/flush)
    consistent and shrinks the surface area for stale messages.
    """
    state = ctx.obj
    mod = _import_optional(module_name)
    fn = getattr(mod, attr_name, None) if mod is not None else None
    if fn is None:
        err = StructuredError(
            error="unsupported_feature",
            exit_code=EXIT_USAGE,
            target=None,
            message=(
                f"{sub_verb} is not yet wired up — Engineer A's "
                f"{module_name}.py must expose {attr_name}()."
            ),
            hint="Re-run after the Phase 1 merge.",
        )
        emit_error(err, state["mode"])
        sys.exit(EXIT_USAGE)
    return fn


# --- config show / config validate --------------------------------------------


@main.group("config")
def config_group() -> None:
    """Configuration sub-verbs."""


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Print the effective resolved config in TOML."""
    fn = _require_engineer_a_attr(
        ctx,
        module_name="config",
        attr_name="render_effective_toml",
        sub_verb="config show",
    )
    click.echo(fn(ctx.obj["config_path"]))
    sys.exit(EXIT_OK)


@config_group.command("validate")
@click.argument("path", required=False, type=click.Path(dir_okay=False))
@click.pass_context
def config_validate(ctx: click.Context, *, path: str | None) -> None:
    """Validate a config file and exit 0 (ok) or 6 (error)."""
    fn = _require_engineer_a_attr(
        ctx,
        module_name="config",
        attr_name="validate_config",
        sub_verb="config validate",
    )
    state = ctx.obj
    try:
        fn(path or state["config_path"])
        sys.exit(EXIT_OK)
    except KasaCliError as exc:
        emit_error(_to_structured(exc), state["mode"])
        sys.exit(exc.exit_code)


# --- auth status / auth flush -------------------------------------------------


@main.group("auth")
def auth_group() -> None:
    """Authentication / session-cache sub-verbs."""


@auth_group.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Print cached KLAP session metadata (one line per device)."""
    fn = _require_engineer_a_attr(
        ctx,
        module_name="auth_cache",
        attr_name="auth_status",
        sub_verb="auth status",
    )
    from kasa_cli.output import emit_stream

    emit_stream(list(fn()), ctx.obj["mode"], formatter=str)
    sys.exit(EXIT_OK)


@auth_group.command("flush")
@click.option("--target", type=str, default=None)
@click.pass_context
def auth_flush(ctx: click.Context, *, target: str | None) -> None:
    """Delete all (or one device's) cached KLAP session state."""
    fn = _require_engineer_a_attr(
        ctx,
        module_name="auth_cache",
        attr_name="flush_sessions",
        sub_verb="auth flush",
    )
    deleted = fn(target=target)
    click.echo(f"flushed {deleted} session file(s)")
    sys.exit(EXIT_OK)


__all__ = ["UsageError", "main"]
