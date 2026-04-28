# kasa-cli — Examples

Cron-style automation, shell-pipeline composition, and integrating kasa-cli with the rest of your toolbelt. Each section is a self-contained, copy-pastable pattern.

## Contents

1. [Daily routines (cron)](#daily-routines-cron)
2. [Daily routines (systemd timers)](#daily-routines-systemd-timers)
3. [Daily routines (launchd, macOS)](#daily-routines-launchd-macos)
4. [Energy monitoring → InfluxDB / Prometheus](#energy-monitoring--influxdb--prometheus)
5. [Group control with shell loops](#group-control-with-shell-loops)
6. [Conditional logic with exit codes](#conditional-logic-with-exit-codes)
7. [JSON pipelines with `jq`](#json-pipelines-with-jq)
8. [Vault integration via 1Password CLI](#vault-integration-via-1password-cli)
9. [Discovering new devices and adding them to config](#discovering-new-devices-and-adding-them-to-config)

---

## Daily routines (cron)

```cron
# m h dom mon dow command

# Every weeknight at 22:00, dim bedroom + turn off the rest of the house
0 22 * * 1-5  kasa-cli batch --file ~/.config/kasa-cli/routines/weeknight.batch >> ~/.local/state/kasa-cli/cron.log 2>&1

# Saturdays at 23:30 — same routine, but later
30 23 * * 6   kasa-cli batch --file ~/.config/kasa-cli/routines/weekend.batch  >> ~/.local/state/kasa-cli/cron.log 2>&1

# Every morning at 06:30, ramp the bedroom lamp up gently
30 6  * * 1-5 kasa-cli set bedroom-lamp --brightness 5  --color-temp 2200
35 6  * * 1-5 kasa-cli set bedroom-lamp --brightness 30 --color-temp 2700
40 6  * * 1-5 kasa-cli set bedroom-lamp --brightness 80 --color-temp 4000
```

Sample `weeknight.batch`:

```bash
# Comments and blank lines are skipped. Each line dispatches one verb.
off @living-room
off @kitchen
off office-strip --socket 1
off office-strip --socket 2
off office-strip --socket 3
# Bedroom stays dim — no off here
set bedroom-lamp --brightness 5 --color-temp 2200
```

Cron's environment is sparse — `$HOME` may be set, but `$PATH` typically isn't. Either:

- Use absolute paths: `/Users/you/.local/bin/kasa-cli ...`
- Set `PATH=...` at the top of your crontab
- Or run via `bash -lc 'kasa-cli ...'` to source your login shell

For graceful Ctrl-C semantics during long batches (e.g., a 50-line batch interrupted by reboot), kasa-cli always emits the `{"event":"interrupted",...}` summary line on SIGTERM and exits **143** — your cron log will show clean structured output even on partial runs.

---

## Daily routines (systemd timers)

`~/.config/systemd/user/kasa-night.service`:

```ini
[Unit]
Description=Run kasa-cli night routine

[Service]
Type=oneshot
ExecStart=/home/you/.local/bin/kasa-cli batch --file %h/.config/kasa-cli/routines/night.batch
StandardOutput=append:%h/.local/state/kasa-cli/night.log
StandardError=inherit
```

`~/.config/systemd/user/kasa-night.timer`:

```ini
[Unit]
Description=Trigger the kasa-cli night routine

[Timer]
OnCalendar=Mon..Fri 22:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
$ systemctl --user daemon-reload
$ systemctl --user enable --now kasa-night.timer
$ systemctl --user list-timers --all | grep kasa
> Mon 2026-04-28 22:00:00 EDT 16h Mon 2026-04-27 22:00:01 EDT 7h ago kasa-night.timer kasa-night.service
```

If the systemd unit produces non-zero exit codes, `journalctl --user -u kasa-night.service` shows the structured stderr error (per FR-35a) — exit 7 means partial failure with detail in the log.

---

## Daily routines (launchd, macOS)

`~/Library/LaunchAgents/com.you.kasa-night.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.you.kasa-night</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/you/.local/bin/kasa-cli</string>
        <string>batch</string>
        <string>--file</string>
        <string>/Users/you/.config/kasa-cli/routines/night.batch</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>StandardOutPath</key>
    <string>/Users/you/.local/state/kasa-cli/night.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/you/.local/state/kasa-cli/night.err</string>
</dict>
</plist>
```

Load it:

```bash
$ launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.kasa-night.plist
$ launchctl print gui/$(id -u)/com.you.kasa-night | head
```

---

## Energy monitoring → InfluxDB / Prometheus

```bash
#!/usr/bin/env bash
# energy-to-influx.sh — stream HS300 per-socket energy readings to InfluxDB
set -euo pipefail
TARGET="${1:-office-strip}"
SOCKET="${2:-1}"
INFLUX_URL="http://localhost:8086/api/v2/write?org=home&bucket=energy&precision=s"
INFLUX_TOKEN="$(op read 'op://Personal/InfluxDB/token')"

kasa-cli --jsonl energy "$TARGET" --socket "$SOCKET" --watch 5 \
  | jq -c --unbuffered '{
        measurement: "kasa",
        tags: {alias: .alias, socket: (.socket|tostring)},
        fields: {power_w: .current_power_w, voltage_v: .voltage_v, current_a: .current_a},
        time: (.ts | fromdateiso8601)
    }' \
  | while read -r line; do
        echo "$line" | jq -r '.tags as $t | .fields as $f | "kasa,alias=\($t.alias),socket=\($t.socket) power_w=\($f.power_w),voltage_v=\($f.voltage_v),current_a=\($f.current_a) \(.time)"' \
        | curl -sS -X POST "$INFLUX_URL" \
            -H "Authorization: Token $INFLUX_TOKEN" \
            -H "Content-Type: text/plain; charset=utf-8" \
            --data-binary @-
    done
```

Run as a systemd service for continuous logging. The `--jsonl` + `jq -c --unbuffered` combo is the canonical "tail-and-transform" pipeline shape kasa-cli was designed for.

For Prometheus, use a `node_exporter` textfile collector:

```bash
$ kasa-cli --json energy office-strip --socket 1 \
  | jq -r '"# HELP kasa_power_watts Current power draw in watts\n# TYPE kasa_power_watts gauge\nkasa_power_watts{alias=\"\(.alias)\",socket=\"\(.socket)\"} \(.current_power_w)"' \
  > /var/lib/node_exporter/textfile_collector/kasa.prom
```

---

## Group control with shell loops

```bash
# Per-socket query: kasa-cli rejects --socket on @group, so loop in shell
$ for alias in $(kasa-cli --json list --groups | jq -r '.[] | select(.name == "strips") | .members[]'); do
    for socket in 1 2 3 4 5; do
        kasa-cli --json energy "$alias" --socket "$socket" \
          | jq -c '. + {target: "\(.alias):\(.socket)"}'
    done
  done
```

```bash
# Toggle a group while honoring per-device socket conventions
$ for alias in $(kasa-cli --json groups list | jq -r '.[] | select(.name == "tv-stack") | .members[]'); do
    case "$alias" in
        # The TV strip has the AVR on socket 1, console on 2, etc.
        tv-strip) kasa-cli toggle tv-strip --socket 1 ;;
        *)        kasa-cli toggle "$alias" ;;
    esac
  done
```

```bash
# Sequential off (no concurrency) — useful when devices share one breaker
$ for alias in kitchen-lamp office-strip patio-plug; do
    kasa-cli off "$alias"
    sleep 0.5
  done
```

---

## Conditional logic with exit codes

```bash
# Only ramp the lamp if it's currently off (idempotent on already-on)
$ if [ "$(kasa-cli --json info bedroom-lamp | jq -r .state)" = "off" ]; then
    kasa-cli on bedroom-lamp
    kasa-cli set bedroom-lamp --brightness 5 --color-temp 2200
  fi
```

```bash
# Distinguish failure modes by exit code
$ if ! kasa-cli on @bedroom-lights; then
    case $? in
        2) notify-send "kasa: auth failure — check credentials" ;;
        3) notify-send "kasa: network failure — devices unreachable" ;;
        4) notify-send "kasa: device not found — check config" ;;
        7) notify-send "kasa: partial result — check log for which devices failed" ;;
        *) notify-send "kasa: unexpected exit $? — see log" ;;
    esac
  fi
```

```bash
# Retry with backoff on transient network errors only
$ for attempt in 1 2 3; do
    kasa-cli on patio-plug && break
    case $? in
        3) sleep $((attempt * 2)); continue ;;  # network — retry
        *) break ;;                              # everything else — bail
    esac
  done
```

---

## JSON pipelines with `jq`

```bash
# List every device's online status (parallel probe)
$ kasa-cli --json list --probe \
  | jq -r '.[] | "\(.alias)\t\(.online)"'
> kitchen-lamp    true
> office-strip    true
> patio-plug      false

# Find every multi-socket strip and dump its socket layout
$ for alias in $(kasa-cli --json list | jq -r '.[].alias'); do
    kasa-cli --json info "$alias" 2>/dev/null \
      | jq -r 'select(.sockets) | "\(.alias):\n\(.sockets | map("  \(.index): \(.alias) (\(.state))") | join("\n"))"'
  done

# Aggregate today's kWh across every energy-monitored device
$ kasa-cli --jsonl batch --file <(printf 'energy %s\n' kitchen-plug office-strip patio-plug) \
  | jq -s 'map(.output.today_kwh // 0) | add'

# Pretty-print failures from a batch run
$ kasa-cli --jsonl batch --file routine.batch \
  | jq 'select(.success == false)'
```

---

## Vault integration via 1Password CLI

kasa-cli does not integrate with 1Password directly (per SRD §15 row 1, Decision 1: plain credentials file only in v1). Wrap with a 5-line script:

```bash
#!/usr/bin/env bash
# ~/bin/kasa — kasa-cli with credentials materialized from 1Password
set -euo pipefail
EMAIL="$(op read 'op://Personal/TP-Link Kasa/username')"
PASS="$(op read 'op://Personal/TP-Link Kasa/password')"
jq -n --arg u "$EMAIL" --arg p "$PASS" '{version:1, username:$u, password:$p}' \
    > ~/.config/kasa-cli/credentials
chmod 600 ~/.config/kasa-cli/credentials
exec kasa-cli "$@"
```

Use `~/bin/kasa` instead of `kasa-cli` whenever fresh creds are needed. For long-running batches, materialize once and let kasa-cli reuse the file across the batch.

For `pass`:

```bash
USER="$(pass kasa/email)"
PASS="$(pass kasa/password)"
```

For `bw` (Bitwarden CLI):

```bash
ITEM="$(bw get item kasa --session "$BW_SESSION")"
USER="$(jq -r .login.username <<< "$ITEM")"
PASS="$(jq -r .login.password <<< "$ITEM")"
```

---

## Discovering new devices and adding them to config

When you plug in a new Kasa device:

```bash
# 1. Find it on the LAN
$ kasa-cli --json discover
> [
>   ...
>   {"alias": "Living Room Lamp", "ip": "192.168.1.99", "mac": "AA:BB:CC:DD:EE:99", "model": "KL130", "protocol": "klap", "state": "off"}
> ]

# 2. Test it works (KLAP-protocol devices need credentials configured)
$ kasa-cli on 192.168.1.99
$ kasa-cli set 192.168.1.99 --color blue
$ kasa-cli off 192.168.1.99

# 3. Add it to your config
$ cat >> ~/.config/kasa-cli/config.toml <<'EOF'

[devices.living-room-lamp]
ip  = "192.168.1.99"
mac = "AA:BB:CC:DD:EE:99"
EOF

# 4. Verify
$ kasa-cli config validate
$ kasa-cli on living-room-lamp
```

For multi-NIC hosts (Wi-Fi + Tailscale + Docker bridges), use `--target-network` to constrain the broadcast:

```bash
$ kasa-cli discover --target-network 192.168.1.0/24
```

See [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md#discovery-finds-zero-devices) for more on multi-NIC discovery.

---

## See also

- [docs/USAGE.md](USAGE.md) — every verb's reference
- [docs/CONFIG.md](CONFIG.md) — full config schema
- [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) — common issues
