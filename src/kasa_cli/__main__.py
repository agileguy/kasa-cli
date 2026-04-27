"""Entry point for the `kasa-cli` console script.

Phase 1 implementation lands real verbs (discover, list, info, on, off);
this stub exists so `pyproject.toml`'s entry point resolves.
"""

from __future__ import annotations

import sys


def main() -> int:
    sys.stderr.write(
        "kasa-cli: pre-alpha scaffold — no verbs implemented yet. "
        "See docs/SRD-kasa-cli.md.\n"
    )
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
