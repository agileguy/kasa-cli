"""Tests for the SRD §7.3 ``[logging] file`` runtime tee (Phase 2 Engineer B).

Covers:

- A FileHandler is attached when ``cfg.logging.file`` is set.
- Running ``kasa-cli`` end-to-end with a config that defines ``[logging] file``
  writes JSON log lines to that file in addition to stderr.
- The attach helper is idempotent across repeated invocations (no
  duplicate handlers, no duplicated lines).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner


def _read_log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_attach_file_logging_attaches_filehandler(tmp_path: Path) -> None:
    """Direct unit test: ``_attach_file_logging`` adds a FileHandler."""
    from kasa_cli.cli import _attach_file_logging
    from kasa_cli.config import Config, LoggingConfig

    log_file = tmp_path / "kasa-cli.log"
    cfg = Config(logging=LoggingConfig(file=str(log_file)))

    # Reset any prior file handler so the test is hermetic.
    root = logging.getLogger("kasa_cli")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            h.close()
    if hasattr(root, "_kasa_cli_file_handler_path"):
        delattr(root, "_kasa_cli_file_handler_path")

    _attach_file_logging(cfg)

    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == log_file


def test_attach_file_logging_is_idempotent(tmp_path: Path) -> None:
    """Calling twice with the same path leaves exactly one FileHandler."""
    from kasa_cli.cli import _attach_file_logging
    from kasa_cli.config import Config, LoggingConfig

    log_file = tmp_path / "kasa-cli.log"
    cfg = Config(logging=LoggingConfig(file=str(log_file)))

    root = logging.getLogger("kasa_cli")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            h.close()
    if hasattr(root, "_kasa_cli_file_handler_path"):
        delattr(root, "_kasa_cli_file_handler_path")

    _attach_file_logging(cfg)
    _attach_file_logging(cfg)
    _attach_file_logging(cfg)

    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_cli_with_logging_file_writes_json_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: invoking the CLI with ``[logging] file`` writes a JSON line.

    We use ``-v --json list`` because ``list`` against an empty config triggers
    a single INFO line ("no config file found, using defaults") which the
    FileHandler tees to disk.
    """
    log_file = tmp_path / "kasa-cli.log"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f"""[defaults]
timeout_seconds = 1

[logging]
file = "{log_file}"
""",
        encoding="utf-8",
    )

    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    # Reset file-handler state on the kasa_cli logger so we don't pick up a
    # stale handler from a sibling test in the same process.
    root = logging.getLogger("kasa_cli")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            h.close()
    if hasattr(root, "_kasa_cli_file_handler_path"):
        delattr(root, "_kasa_cli_file_handler_path")

    from kasa_cli.cli import main as cli_main

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--config", str(cfg_path), "-v", "--json", "list"])
    assert result.exit_code == 0, f"stderr: {result.stderr}"

    # Force the file handler to flush — pytest may not have closed it.
    for h in root.handlers:
        if isinstance(h, logging.FileHandler):
            h.flush()

    lines = _read_log_lines(log_file)
    assert lines, f"expected at least one JSON log line in {log_file}"
    # Every line must be valid JSON with the expected shape.
    parsed = [json.loads(line) for line in lines]
    info_lines = [p for p in parsed if p.get("level") == "INFO"]
    assert info_lines, f"expected at least one INFO entry; got {parsed!r}"


def test_cli_without_logging_file_does_not_create_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``[logging] file = …`` → no FileHandler is attached, no file written."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[defaults]\ntimeout_seconds = 1\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("KASA_CLI_CONFIG", raising=False)

    root = logging.getLogger("kasa_cli")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            h.close()
    if hasattr(root, "_kasa_cli_file_handler_path"):
        delattr(root, "_kasa_cli_file_handler_path")

    from kasa_cli.cli import main as cli_main

    runner = CliRunner()
    result: Any = runner.invoke(cli_main, ["--config", str(cfg_path), "-v", "--json", "list"])
    assert result.exit_code == 0
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers == []
