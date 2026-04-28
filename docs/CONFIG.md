# kasa-cli — Configuration Reference

## File locations

| File | Purpose | Mode |
|---|---|---|
| `~/.config/kasa-cli/config.toml` | Aliases, groups, defaults, credentials pointer | `0644` ok |
| `~/.config/kasa-cli/credentials` | TP-Link cloud credentials (JSON) | `0600` enforced |
| `~/.config/kasa-cli/.tokens/<MAC>.json` | Per-device KLAP session cache (auto-managed) | `0600` files in `0700` dir |
| `~/.local/state/kasa-cli/log` | Optional file logging output (when `[logging] file` is set) | operator-managed |

### Resolution precedence

1. `--config <path>` CLI flag (per-invocation override)
2. `KASA_CLI_CONFIG` environment variable
3. `~/.config/kasa-cli/config.toml` (default)
4. Built-in defaults (no config file required for basic discovery)

If `--config` or `KASA_CLI_CONFIG` is set and the file does not exist or cannot be read, kasa-cli exits **6** (config error). If only the default location is consulted and it doesn't exist, kasa-cli runs with built-in defaults and emits a single INFO log line on stderr (`"no config file found, using defaults"`).

---

## `config.toml` schema

| Section | Field | Type | Default | Purpose |
|---|---|---|---|---|
| `[defaults]` | `timeout_seconds` | int | `5` | Per-operation timeout (seconds; floats allowed where the verb supports sub-second timeouts) |
| `[defaults]` | `concurrency` | int | `10` | Max parallel device ops in `@group` fanout and `batch` |
| `[defaults]` | `output_format` | string | `"auto"` | `"auto"` (text on tty, JSONL on pipe), `"text"`, `"json"`, `"jsonl"` |
| `[credentials]` | `file_path` | string | `~/.config/kasa-cli/credentials` | Default credentials file (chmod 0600 enforced) |
| `[logging]` | `file` | string | (unset) | Optional path; when set, JSON log lines are tee'd here in addition to stderr |
| `[devices.<alias>]` | `ip` | string | — | Static IP (skips discovery) |
| `[devices.<alias>]` | `mac` | string | — | MAC for stable identification across IP renumbering |
| `[devices.<alias>]` | `credential_file` | string | — | Per-device credentials file override (e.g. for a guest account) |
| `[groups]` | `<name>` | `string[]` | — | Array of alias names |

`<alias>` and `<name>` are user-chosen identifiers. Aliases must match `[a-z0-9-]+` after normalization. Group names follow the same convention.

---

## Complete annotated example

```toml
# ~/.config/kasa-cli/config.toml
#
# kasa-cli config file. Hand-edited; v1 has no `kasa-cli config edit`
# sub-verb. Aliases here are the names you type at the CLI:
#     kasa-cli on kitchen-lamp
#
# Groups expand at command time:
#     kasa-cli on @bedroom-lights
#
# IP + MAC pairs let kasa-cli skip the broadcast discovery step. MAC is
# the more durable identifier — survives DHCP lease renumbering. If you
# only know the IP, omit MAC and rely on `kasa-cli list --probe` to
# verify reachability.

[defaults]
timeout_seconds = 5            # Per-operation timeout. 5s is fine for LAN; raise for slow Wi-Fi.
concurrency = 10               # Default for @group fanout and batch.
output_format = "auto"         # text on tty, jsonl on pipe.

[credentials]
file_path = "~/.config/kasa-cli/credentials"

[logging]
# Uncomment to tee structured JSON log lines to a file (in addition to
# stderr). Useful for cron jobs without redirect plumbing.
# file = "~/.local/state/kasa-cli/log"

# --- Devices ----------------------------------------------------------

[devices.kitchen-lamp]
ip  = "192.168.1.42"
mac = "AA:BB:CC:DD:EE:01"

[devices.office-strip]
ip  = "192.168.1.51"
mac = "AA:BB:CC:DD:EE:02"

[devices.patio-plug]
ip  = "192.168.1.78"
mac = "AA:BB:CC:DD:EE:03"
# This guest device uses a different TP-Link account.
credential_file = "~/.config/kasa-cli/credentials.guest"

[devices.bedroom-lamp]
ip  = "192.168.1.91"
mac = "AA:BB:CC:DD:EE:04"

[devices.hallway-strip]
ip  = "192.168.1.92"
mac = "AA:BB:CC:DD:EE:05"

# --- Groups -----------------------------------------------------------
#
# Each group is an array of alias names defined above. Mismatched names
# are caught at config-validate time (`kasa-cli config validate`).

[groups]
bedroom-lights = ["bedroom-lamp", "hallway-strip"]
outdoor        = ["patio-plug"]
night-off      = ["kitchen-lamp", "office-strip", "patio-plug"]
```

---

## Credentials file format

Path: `~/.config/kasa-cli/credentials` (or per-device `[devices.<alias>] credential_file`).

```json
{
  "version": 1,
  "username": "you@example.com",
  "password": "..."
}
```

**Constraints:**

- File mode **must** be `0600` (owner read/write, no group/world bits). kasa-cli refuses to load files with any group or world bits set, exiting **2** with the offending mode in the error message and a `chmod 600 <path>` hint.
- The file **must not** be a symlink (defense against credential-file substitution attacks).
- `version` is an integer; v1 is the only version. Unknown additional keys exit **6**.
- Missing `version` field is treated as v1 with a one-time deprecation warning on stderr (per process per file path).

**Set permissions correctly:**

```bash
$ install -m 0600 /dev/null ~/.config/kasa-cli/credentials
$ cat > ~/.config/kasa-cli/credentials <<'EOF'
{"version": 1, "username": "you@example.com", "password": "..."}
EOF
$ ls -la ~/.config/kasa-cli/credentials
> -rw------- 1 you staff 75 Apr 27 22:00 ~/.config/kasa-cli/credentials
```

### Credential resolution order

For each device operation, kasa-cli walks (in order):

1. **Per-device override**: `[devices.<alias>] credential_file` — if the alias's config entry points at a credentials file, use it.
2. **Environment variables**: `KASA_USERNAME` and `KASA_PASSWORD`. Both must be set; "username only" is treated as no env credentials (matches the file format's all-or-nothing rule).
3. **Default credentials file**: `[credentials] file_path` (default `~/.config/kasa-cli/credentials`).
4. **No credentials**: legacy-protocol path only. KLAP devices fail with exit **2** and an actionable hint.

### `--credential-source env|file|none` flag

| Value | Behavior |
|---|---|
| (unset, default) | Walk the full chain above (per-device → env → file → none) |
| `env` | Use only env vars; do not read any file |
| `file` | Use only files (per-device override or default); do not read env |
| `none` | Skip credentials entirely; KLAP-protocol devices will exit 2 |

### Vault integration (1Password, pass, bw, etc.)

External credential managers are **not** integrated into v1. Wrap kasa-cli in a script that materializes the file before invocation:

```bash
#!/usr/bin/env bash
# Pull TP-Link credentials from 1Password into the credentials file
# before running kasa-cli. The file is rewritten each invocation, so
# rotated passwords take effect immediately.
set -euo pipefail
op read "op://Personal/TP-Link Kasa/credential" \
    | jq -n --arg pw "$(cat)" --arg user "you@example.com" \
        '{version: 1, username: $user, password: $pw}' \
    > ~/.config/kasa-cli/credentials
chmod 600 ~/.config/kasa-cli/credentials
exec kasa-cli "$@"
```

Save as `~/bin/kasa` and use it instead of `kasa-cli` for vault-backed runs.

---

## KLAP session cache (`~/.config/kasa-cli/.tokens/`)

kasa-cli persists each successful KLAP authentication so subsequent invocations skip the handshake (~3 round-trips at ~50 ms each). One file per device:

```
~/.config/kasa-cli/.tokens/AA-BB-CC-DD-EE-03.json
```

Auto-managed; you should never edit these files by hand. The session expiry comes from the TIMEOUT cookie returned by the KLAP handshake (24-hour default) minus a 20-minute safety buffer. Stored as a wall-clock timestamp so it survives process restarts.

If a cached session is rejected by the device (firmware reboot, password rotation), kasa-cli auto-retries once with a fresh handshake. To force re-auth manually:

```bash
$ kasa-cli auth flush                  # all devices
$ kasa-cli auth flush --target patio-plug  # one device
```

---

## Validating your config

```bash
$ kasa-cli config validate
# exit 0 if valid; exit 6 with a structured stderr error if not

$ kasa-cli config validate /tmp/new-config.toml
# Lint a candidate without activating it
```

Common validation failures:

- **Unknown keys** (e.g., a typo'd `[defaults] timout_seconds`): exit 6 listing the unknown key.
- **Dangling group references** (a group lists an alias that isn't in `[devices]`): exit 6 naming the group and the missing alias.
- **Malformed TOML syntax**: exit 6 with the parser's line/column.
- **Default credentials file with permissive mode** (`0644`, `0660`, etc.): exit 2 (auth error) at first KLAP operation. Fix with `chmod 600`.

---

## Effective config dump

```bash
$ kasa-cli config show
# Prints the resolved config (after --config / KASA_CLI_CONFIG / default
# precedence + built-in defaults applied) in TOML format. Round-trips
# back through config.toml without loss — useful to verify what kasa-cli
# is actually loading.

$ diff ~/.config/kasa-cli/config.toml <(kasa-cli config show)
# Sanity-check defaults you didn't write are coming from where you expect
```

---

## See also

- [docs/USAGE.md](USAGE.md) — every verb with examples
- [docs/EXAMPLES.md](EXAMPLES.md) — cron / systemd / shell-pipeline patterns
- [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) — auth / config gotchas
- [docs/SRD-kasa-cli.md](SRD-kasa-cli.md) §6 (Authentication), §9 (Configuration File), §11 (Error Model)
