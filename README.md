# HVAC Dashboard

Self-hosted dashboard for controlling and monitoring **Innovair mini-split AC units** via [SMLIGHT SLWF-01pro](https://smartlight.me) ESPHome dongles using the Midea serial protocol. Runs 24/7 in Docker on a QNAP NAS. All automation (scheduling, temperature guards, watchdog) executes server-side regardless of whether a browser is open.

---

## Features

### Unit Control
- Mode selector (cool / heat / auto / fan / dry) as a compact segmented bar
- Temperature +/− with debounce — rapid clicks batch into one command, confirmed against device after send
- Power toggle per unit
- Beeper toggle (persisted server-side, synced on every poll)
- 🔒 Temp lock — override protection: if someone changes the setpoint on the physical remote, the next poll auto-reverts to the locked value

### Tile Info
- Indoor / outdoor temp (°C + °F)
- Estimated watts (~W)
- On-time accumulator with reset button
- WiFi signal strength (▂▄▆█ bars with dBm tooltip)
- 48-hour indoor/outdoor temperature sparkline
- ⚠ Stale badge + amber border when device hasn't responded within watchdog threshold
- ↺N retry queue badge when commands are queued for failed devices
- 📡 overdue indicator (only shown when outside watchdog window)

### Automation (server-side, 24/7)
- **Schedules** — time + day-of-week per device, evaluated every minute server-side
- **Max temp guard** — auto-turns on AC when room exceeds configurable threshold, off when cooled
- **Vacation mode** — turns off all units, sets high temp guard (configurable), pauses all schedules
- **Beeper sync** — saved beeper state pushed to device on every poll
- **Reboot detection** — logs when dongle uptime resets
- **Retry queue** — failed commands queued and retried on recovery
- **Watchdog** — per-device configurable alert timeout; logs online/offline transitions

### Monitoring
- API status badge in header (⬤ green/red, checked every 30s)
- Per-device last_seen, queue depth, stale status via `GET /api/health`
- Uptime Kuma push endpoint: `GET /api/health/push` — returns 200/503
- Server-side automation log — filterable by level, searchable
- Notification badge on Log section when new warnings/errors arrive

### Usage & Costs
- Hourly temperature history per device (48h rolling)
- Daily runtime hours and estimated kWh per device
- Monthly usage summary with per-device breakdown
- Rolling 30-day chart (daily kWh bars)
- Per-device cost table with $/day, $/mo, share % bar
- Projected month-end spend shown in Est cost card
- CSV export: `GET /api/usage/export-csv?month=YYYY-MM`
- Tiered Coopeguanacaste rates, exchange rate auto-updated daily from `frankfurter.app`

### UI
- Dark / light mode (☀️ / 🌙 toggle in ··· menu)
- Collapsible sections: Cost breakdown, Usage, Schedules, Log
- Drag-to-reorder devices in Settings → Devices
- Custom drum-roll time picker in schedule modal
- Connection test per device (⚡) — shows latency in a modal
- Refresh on browser focus / tab visibility change (debounced 30s)
- About modal with live version from API

### Settings (··· menu → Settings)
- **General** — poll interval, default SEER, default watchdog, vacation max temp
- **Devices** — add/edit/reorder/delete, BTU, SEER, Max°C guard, alert timeout, save/test/poll per row
- **Rates** — provider, exchange rate (↻ Live fetch), monthly kWh, runtime hrs, tiered or flat rate

---

## Quick Start

```bash
git clone https://github.com/dwightmulcahy/hvac-dashboard.git
cd hvac-dashboard
docker-compose up -d
```

Open `http://your-host:8080`, then add your devices:

```bash
curl -X POST http://your-host:8080/api/devices \
  -H "Content-Type: application/json" \
  -d '{"host":"air-conditioner-c44741.lan","name":"Master BR","btu":24000,"seer":20}'
```

---

## Docker Hub

```bash
docker pull dwightmulcahy/hvac-dashboard:latest

docker run -d \
  --name hvac-dashboard \
  --restart always \
  -p 8080:80 \
  -v hvac-data:/data \
  -e DATA_FILE=/data/hvac_state.json \
  -e TZ=America/Costa_Rica \
  dwightmulcahy/hvac-dashboard:latest
```

---

## docker-compose.yml (QNAP)

```yaml
services:
  hvac-dashboard:
    image: dwightmulcahy/hvac-dashboard:latest
    container_name: hvac-dashboard
    restart: unless-stopped
    ports:
      - "8080:80"
    environment:
      - TZ=America/Costa_Rica
      - DATA_FILE=/data/hvac_state.json
    volumes:
      - /Share/Data/config/hvac-dashboard:/data
    labels:
      - "com.centurylinklabs.watchtower.enable=true"
      - "homepage.group=Home Dashboards"
      - "homepage.href=http://homenas.lan:8080"
      - "homepage.description=HVAC Dashboard"
```

---

## API Reference

### Devices
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/devices` | List all devices with current state |
| POST | `/api/devices` | Add device |
| PUT | `/api/devices/{host}` | Update device config |
| DELETE | `/api/devices/{host}` | Remove device |
| POST | `/api/devices/{host}/cmd` | Send command (queued on failure) |
| POST | `/api/devices/{host}/beeper/{on\|off}` | Set beeper |
| POST | `/api/devices/{host}/beeper/test` | Test connection |
| POST | `/api/devices/{host}/lock` | Lock/unlock temp override |
| GET | `/api/devices/{host}/temp-history` | 48h hourly temp readings |

### Schedules
| Method | Endpoint | Description |
|---|---|---|
| GET/POST | `/api/schedules` | List or create |
| PUT/DELETE | `/api/schedules/{id}` | Update or delete |
| POST | `/api/schedules/{id}/toggle` | Enable/disable |

### Usage
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/usage/summary?month=YYYY-MM` | Monthly summary |
| GET | `/api/usage/rolling30` | Rolling 30-day + daily series |
| GET | `/api/usage/export-csv?month=YYYY-MM` | Download CSV |

### System
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/` | Status, version, build date |
| GET | `/api/health` | Per-device watchdog status |
| GET | `/api/health/push` | Uptime Kuma endpoint (200/503) |
| GET/PUT | `/api/settings` | Rate and poll settings |
| GET | `/api/exchange-rate` | Current USD/CRC rate (cached daily) |
| GET | `/api/logs?level=warn+&limit=100` | Automation log |
| GET | `/api/backup` | Export config JSON |
| POST | `/api/restore` | Restore from backup |
| DELETE | `/api/reset` | Clear usage data |

---

## Firmware

`firmware/` contains fixed ESPHome YAML for the SMLIGHT SLWF-01pro:

- `slwf01pro24-fixed.yaml` — v2.4 (recommended)
- `slwf-01pro-v21-fixed.yaml` — v2.1

Both fixes applied:
- `api: reboot_timeout: 0s` — prevents 15-min reboot when no Home Assistant connected
- `web_server: cors_allowed_origins: ["*"]` — allows dashboard commands

Flash via OTA:
```bash
pip install esphome
esphome run firmware/slwf01pro24-fixed.yaml
```

`secrets.yaml` required alongside:
```yaml
wifi_ssid: "YourWiFi"
wifi_password: "YourPassword"
air_conditioner_ota_password: "your-ota-password"
```

---

## Uptime Kuma

Add an **HTTP(s)** monitor pointing to:
```
http://your-qnap:8080/api/health/push
```
Returns `200` when all devices healthy, `503` when any are stale.

---

## Release Process

```bash
git checkout release
git merge main
git tag v1.2.0
git push origin release --tags
```

GitHub Actions builds multi-arch (`amd64` + `arm64`) images and pushes to Docker Hub with tags `latest`, `1.2.0`, `1.2`, `1`. Version is injected from the git tag via `APP_VERSION` build arg.

### Required Secrets
| Secret | Description |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub access token |

---

## Hardware

- **AC units** — Innovair Bio-Inverter 12K/18K/24K BTU, 20 SEER, R32
- **Dongles** — SMLIGHT SLWF-01pro (ESP8266, Midea serial)
- **Server** — QNAP NAS, Container Station
- **Network** — Amplifi router, `192.168.133.x`
- **Location** — Playa del Coco, Guanacaste, Costa Rica
- **Electricity** — Coopeguanacaste, ARESEP tiered rates, no time-of-use

---

## Developer

Dwight Mulcahy

## License

MIT
