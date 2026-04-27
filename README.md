# kasa-cli

Deterministic, scriptable command-line tool for discovering, querying, and controlling TP-Link Kasa smart devices on the local LAN.

**Status:** Pre-alpha — Phase 1 scaffolding only. See [docs/SRD-kasa-cli.md](docs/SRD-kasa-cli.md) for the full specification.

## What it is

A single CLI binary that takes a verb, a target, and flags, performs one operation against one or more Kasa devices over the local network, prints a result on stdout, and exits with a meaningful status code. The leaf node in a shell pipeline or cron job — nothing more.

It is **not** a HomeKit bridge, not a cloud daemon, not an MQTT broker, not a rules engine, and not a GUI dashboard.

## What it isn't

- No GUI
- No scheduling daemon (cron, systemd, and launchd handle scheduling)
- No cloud relay — local LAN only, no outbound to TP-Link servers
- No automation rules engine
- No Matter or Thread
- No Tapo support in v1

## Install

```bash
uv tool install git+ssh://git@github.com/agileguy/kasa-cli
```

Updates: `uv tool upgrade kasa-cli`.

## Quick start

```bash
# Discover everything on the LAN
kasa-cli discover

# Turn off a configured alias
kasa-cli off kitchen-lamp

# Stream live energy readings
kasa-cli energy office-strip --socket 2 --watch 5
```

## Documentation

- **Full spec:** [docs/SRD-kasa-cli.md](docs/SRD-kasa-cli.md)
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE).
