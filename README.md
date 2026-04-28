# kasa-cli

[![CI](https://github.com/agileguy/kasa-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/agileguy/kasa-cli/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Deterministic, scriptable command-line tool for discovering, querying, and controlling TP-Link Kasa smart devices on the local LAN. Wraps [`python-kasa`](https://github.com/python-kasa/python-kasa).

**Status:** v0.3.0 — full SRD §16 phase plan shipped. Ready for personal use; see [CHANGELOG.md](CHANGELOG.md).

## What it is

A single CLI binary that takes a verb, a target, and flags, performs one operation against one or more Kasa devices over the local network, prints a result on stdout, and exits with a meaningful status code. The leaf node in a shell pipeline or cron job — nothing more.

It is **not** a HomeKit bridge, not a cloud daemon, not an MQTT broker, not a rules engine, and not a GUI dashboard.

## Highlights

- **Deterministic exit codes** (per SRD §11.1) — `0` success, `2` auth, `3` network, `4` not-found, `5` unsupported, `6` config, `7` partial-failure, `64` usage error, `130`/`143` SIGINT/SIGTERM
- **Local LAN only** — never contacts TP-Link cloud servers (KLAP auth uses cloud credentials but performs the handshake against the device on the LAN)
- **Output formats** — text on tty, JSONL on pipe, with `--json` / `--jsonl` / `--quiet` overrides
- **Group fanout** — `kasa-cli on @bedroom-lights` runs in parallel across configured group members
- **Batch mode** — `kasa-cli batch --file commands.txt` for cron-friendly sequenced operations with graceful Ctrl-C drain
- **Per-device session caching** — KLAP sessions persist to disk so repeat invocations skip the handshake

## Install

```bash
uv tool install git+ssh://git@github.com/agileguy/kasa-cli@v0.3.0
```

Updates: `uv tool upgrade kasa-cli`.

Requires Python 3.11+. Tested on macOS 13+ and Linux x86_64/arm64. Windows is not supported (use WSL).

## Quick start

```bash
# Discover everything on the LAN
kasa-cli discover

# Print every alias defined in your config
kasa-cli list

# Show full live state of one device
kasa-cli info kitchen-lamp

# Turn devices on/off (idempotent)
kasa-cli on kitchen-lamp
kasa-cli off office-strip --socket 2

# Set bulb state
kasa-cli set bedroom-lamp --brightness 30
kasa-cli set bedroom-lamp --color warm-white
kasa-cli set bedroom-lamp --color-temp 2700
kasa-cli set bedroom-lamp --hsv 240,100,50

# Stream live energy readings (HS300, KP115, etc.)
kasa-cli energy office-strip --socket 2 --watch 5

# Group fanout
kasa-cli on @bedroom-lights
kasa-cli energy @energy-monitored

# Batch
kasa-cli batch --file night-routine.txt
echo "off @living-room" | kasa-cli batch --stdin
```

See [docs/USAGE.md](docs/USAGE.md) for every verb, every flag, and worked examples.

## Configuration

`~/.config/kasa-cli/config.toml` — TOML with aliases, groups, and a credentials-file pointer. Example:

```toml
[defaults]
timeout_seconds = 5
concurrency = 10

[credentials]
file_path = "~/.config/kasa-cli/credentials"

[devices.kitchen-lamp]
ip = "192.168.1.42"
mac = "AA:BB:CC:DD:EE:01"

[devices.office-strip]
ip = "192.168.1.51"
mac = "AA:BB:CC:DD:EE:02"

[groups]
bedroom-lights = ["bedroom-lamp", "hallway-strip"]
night-off      = ["kitchen-lamp", "office-strip"]
```

Credentials file (`~/.config/kasa-cli/credentials`, chmod 0600) for KLAP-era devices:

```json
{"version": 1, "username": "you@example.com", "password": "..."}
```

See [docs/CONFIG.md](docs/CONFIG.md) for the full schema.

## Documentation

| Doc | Purpose |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | Every verb with worked examples |
| [docs/CONFIG.md](docs/CONFIG.md) | TOML config schema + credentials file format |
| [docs/EXAMPLES.md](docs/EXAMPLES.md) | Cron / systemd / shell-pipeline patterns |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | KLAP auth, multi-NIC discovery, EP40M, etc. |
| [docs/SRD-kasa-cli.md](docs/SRD-kasa-cli.md) | Full specification (FRs, error model, decisions) |
| [CHANGELOG.md](CHANGELOG.md) | Per-version delta |

## What it doesn't do (and why)

| | Why |
|---|---|
| GUI dashboard | This is a CLI; visualizations consume the JSON output |
| Scheduling daemon | cron, systemd timers, and launchd already exist |
| Cloud relay / remote control | Local LAN only; no outbound to TP-Link servers |
| Automation rules engine | Use Home Assistant or shell scripts |
| Matter or Thread | Different protocol stack; different tool |
| TP-Link Tapo line | Brand-adjacent but protocol-divergent; out of scope at all phases of this SRD |
| Device-side schedule editing | Read-only listing only (FR-25); cron owns scheduling |
| `groups add` / `groups remove` | v1 keeps group config hand-edited; comment-preserving TOML round-trip is a non-trivial side quest (FR-29b) |
| Tapo cameras / doorbells / vacuums | Out of scope at all phases — these are media surfaces, not switches |

## Supported devices

| Family | Examples | Notes |
|---|---|---|
| HS-series | HS100, HS103, HS105, HS107, HS110, HS200, HS210, HS220, HS300 | Plugs, switches, dimmers; HS300 is the multi-socket strip |
| KP-series | KP100, KP105, KP115, KP125, KP125M, KP200, KP303, KP400, KP401, KP405 | Plugs, power strips |
| KL-series | KL50, KL60, KL110, KL110B, KL120, KL125, KL130, KL135, KL400L5/L10, KL420L5, KL430 | Bulbs, light strips |
| EP-series | EP10, EP25, EP40, EP40M | Outdoor plugs, strips. **EP40M lacks emeter** despite being supported |
| KS-series | KS200, KS200M, KS205, KS220, KS220M, KS225, KS230, KS240 | Wall switches |
| ES-series | ES20M | Wall switches |
| KH-series | KH100 hub + KE100 hub-attached | Discoverable in v1; child enumeration deferred |

Energy monitoring: HS110, **HS300 (per-socket)**, KP115, KP125, KP125M, EP10, EP25, EP40. EP40M does not have a hardware emeter.

KLAP/Smart authentication is required for post-2022 firmware on most plugs and strips. Provide your TP-Link cloud credentials in the credentials file — kasa-cli authenticates against the device on the LAN, never against TP-Link servers.

## Stack

- Python 3.11+, `uv` for packaging
- [`python-kasa`](https://github.com/python-kasa/python-kasa) 0.10.2 for protocol
- [Click](https://palletsprojects.com/projects/click/) for the CLI surface
- `tomllib` (stdlib) for config; `pytest` + `mypy` + `ruff` for quality

## License

MIT — see [LICENSE](LICENSE).
