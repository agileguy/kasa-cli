"""Entry point for the ``kasa-cli`` console script.

Phase 1 Part B (Engineer B): real verbs are wired up here. The async event
loop is started inside :mod:`kasa_cli.cli` per-command via ``asyncio.run`` so
short-lived invocations don't pay the cost of a long-running loop. This
module is a thin shim that converts Click's ``SystemExit`` (raised by
``ctx.exit(...)`` and ``sys.exit(...)`` inside command handlers) into a
process exit code.
"""

from __future__ import annotations

import click

from kasa_cli.cli import main as _cli_main
from kasa_cli.errors import EXIT_USAGE, StructuredError
from kasa_cli.output import OutputMode, emit_error


def main() -> int:
    """Run the CLI and return the desired process exit code.

    With ``standalone_mode=False`` Click consumes ``click.exceptions.Exit``
    and returns the requested exit code as the call's return value, while
    propagating ``UsageError`` and other ``ClickException`` types as
    exceptions for us to translate. ``sys.exit`` from inside command
    callbacks still surfaces as :class:`SystemExit` and we honor that too.
    """
    try:
        result = _cli_main(standalone_mode=False)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        return int(code)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except click.UsageError as exc:
        # Click raises UsageError when standalone_mode=False; translate to a
        # SRD-shaped structured error on stderr.
        err = StructuredError(
            error="usage_error",
            exit_code=EXIT_USAGE,
            target=None,
            message=str(exc),
            hint="Run with --help for usage.",
        )
        emit_error(err, OutputMode.JSONL)
        return EXIT_USAGE
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    if isinstance(result, int):
        return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
