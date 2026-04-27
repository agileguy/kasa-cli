"""``kasa-cli groups list`` (SRD §5.8, FR-26..29b).

Read-only enumeration of the ``[groups]`` table from the active config. Per
FR-29b v1 explicitly does NOT support ``groups add`` / ``groups remove``
sub-verbs — group mutation is by hand-editing the TOML. The reasoning lives
in the SRD: comment-preserving TOML round-trip is non-trivial, and v1 doesn't
write user config files.

Output shape (one entry per group):

    {"name": "<group-name>", "members": ["<alias-1>", "<alias-2>", ...]}

In text mode each group renders as a single line: ``<name>: alias-1, alias-2``.
In JSON mode the entire collection is a top-level array. In JSONL mode each
group is a single line. An empty ``[groups]`` section (or a config file with no
``[groups]`` table at all) emits ``[]`` in JSON mode and an empty stdout in
JSONL/TEXT modes.

Ungrouped device aliases (devices defined under ``[devices.<alias>]`` but not
referenced by any group) are NEVER included — this verb is about groups, not
devices. Use ``kasa-cli list`` for the full alias inventory.
"""

from __future__ import annotations

from typing import Any

from kasa_cli.config import Config
from kasa_cli.errors import EXIT_SUCCESS
from kasa_cli.output import OutputMode, emit_stream


def _group_to_text(group: object) -> str:
    """One-line text rendering of a ``{name, members}`` dict.

    Stable format: ``<name>: alias-1, alias-2``. An empty group renders as
    ``<name>:`` with no members listed (an unusual but legal TOML state).
    """
    if not isinstance(group, dict):
        return str(group)
    name = group.get("name", "")
    members = group.get("members", [])
    if not isinstance(members, list):
        members = []
    if members:
        return f"{name}: " + ", ".join(str(m) for m in members)
    return f"{name}:"


def collect_groups(config: Config) -> list[dict[str, Any]]:
    """Project ``config.groups`` into the public list-of-dicts shape.

    Pure helper — no I/O, no side effects. Tests use this directly to assert
    the projection without going through the CLI dispatcher. Order is the
    iteration order of ``config.groups`` (which is insertion order in
    Python 3.7+ dict semantics; for a freshly-parsed TOML file that's the
    on-disk order).
    """
    out: list[dict[str, Any]] = []
    for name, members in config.groups.items():
        # Defensive copy of the member list so the caller can't mutate the
        # config dict by accident.
        out.append({"name": name, "members": list(members)})
    return out


async def run_groups_list(
    *,
    config: Config,
    mode: OutputMode,
) -> int:
    """Execute the ``groups list`` verb.

    Args:
        config: The active :class:`Config`. We read ``config.groups`` directly;
            no I/O is performed against the network.
        mode: Output mode for stdout. JSON emits a single top-level array;
            JSONL emits one group per line; TEXT emits a one-line summary per
            group; QUIET emits nothing.

    Returns:
        ``EXIT_SUCCESS`` (0) — listing groups is always successful, even when
        the config has no ``[groups]`` table (empty list is a valid result,
        not an error). FR-26..29 do not define a non-zero exit for this verb.
    """
    groups = collect_groups(config)
    emit_stream(groups, mode, formatter=_group_to_text)
    return EXIT_SUCCESS


__all__ = ["collect_groups", "run_groups_list"]
