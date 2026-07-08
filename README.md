# HVAC Dashboard

A self-hosted web dashboard for controlling and monitoring **Innovair mini-split AC units** via [SMLIGHT SLWF-01pro](https://smartlight.me) ESPHome dongles running the Midea protocol.

Built for a 6-unit installation in Playa del Coco, Guanacaste, Costa Rica — running 24/7 on a QNAP NAS via Docker.

---

## Features

### Control
- **Live control** — mode (cool/heat/auto/fan/dry), temperature (+/−), power per unit
- **Segmented mode selector** — compact mode bar on each tile
- **Debounced temp control** — rapid +/− clicks batch into one command, confirmed against device after send
- **Override protection** — 🔒 lock the setpoint; if someone uses the physical remote to change it, the next poll auto-reverts

### Monitoring
- **Real-time stats** — indoor/outdoor temp (°C + °F), estimated watts, on-time, dongle uptime, WiFi signal strength
- **Stale/watchdog indicator** — amber border + ⚠ stale badge if device hasn't responded within configurable threshold
- **Retry queue** — failed commands are queued and retried when device recovers (↺N badge on tile)
- **Reboot detection** — logs when dongle uptime resets unexpectedly
- **Last polled indicator** — 📡 Xm ago in tile footer
- **API status badge** — ⬤ API v1.1.0 in header, checked every 30s, turns red if unreachable

### Automation (server-side, 24/7)
- **Scheduling** — time + day-of-week schedules per device, runs in Docker regardless of browser state
- **Max temp guard** — auto-turns on AC if room exceeds a configurable threshold, off when cooled
- **Vacation mode** — 🌴 turns off all units, sets high max-temp guard, pauses schedules
- **Beeper sync** — persists beeper state server-side, synced to device on every poll

### Usage & Costs
- **Per-device cost breakdown** — collapsible table with $/day, $/mo, share % bar per unit
- **Month projection** — projected month-end spend based on current daily average
- **Monthly usage summary** — runtime hours, kWh, peak watts, days active per device
- **Rolling 30-day chart** — daily kWh bars per device
- **CSV export** — ⬇ CSV button downloads monthly usage data
- **Tiered electricity rates** — Coopeguanacaste block rates, configurable exchange rate with live ↻ fetch

### UI
- **Dark/light mode** — ☀️ toggle, preference saved
- **Notification badge** — red/amber badge on Log section when new warnings/errors arrive (collapsed)
- **Collapsible sections** — Cost breakdown, Usage, Schedules, Log
- **About modal** — version, build date, GitHub + Docker Hub links, developer credit

### Device Management
- **Devices panel** — ⚙ button in header, hidden by default
- **Per-device settings** — BTU, SEER, Max°C threshold, watchdog minutes
- **Explicit save** — ✓ button per row, grayed until modified
- **Copy hostname** — ⎘ copies hostname to clipboard
- **Test beep** — 🔔 sends a beep to verify device is responding
- **Reset on-time** — ↺ resets the on-time accumulator per device

### Infrastructure
- **Auto-poll** — every 60 seconds server-side, browser is a thin display
- **Server-side persistence** — FastAPI + JSON on a Docker volume, survives restarts
- **Health endpoint** — `/api/health` with per-device status, last_seen, queue depth
- **Backup/Restore** — `GET /api/backup` exports config JSON, `POST /api/restore` reimports

---

## Quick Start

```bash
git clone https://github.com/dwightmulcahy/hvac-dashboard.git
cd hvac-dashboard
docker-compose up -d
```

Open `http://your-host:8080`

Then add your devices via the API:

```bash
curl -X POST http://your-host:8080/api/devices \
  -H "Content-Type: application/json" \
  -d '{"host":"air-conditioner-c44741.lan","name":"Master BR","btu":24000,"seer":20}'
```

---

## Docker Hub

```
docker pull dwightmulcahy/hvac-dashboard:latest
```

Run directly:

```bash
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

## Configuration

Devices are managed via the **⚙ Devices** panel in the UI and stored server-side — no localStorage dependency.

### Rate settings

Click **⚡ Rates** to configure:
- Provider name
- Exchange rate (₡/USD) — with **↻ Live** button to fetch current rate from ECB
- Estimated monthly kWh (determines which tier applies)
- Tiered or flat rate
- AC daily runtime hours (for cost estimation)

Default tiers (Coopeguanacaste 2026, after 9.34% ARESEP reduction):

| Block | Rate |
|---|---|
| 0–200 kWh | ₡56/kWh (~$0.123) |
| 201–500 kWh | ₡74/kWh (~$0.163) |
| 500+ kWh | ₡127/kWh (~$0.279) |

---

## API

All endpoints are proxied by nginx at `/api/`:

### Devices
| Endpoint | Method | Description |
|---|---|---|
| `/api/devices` | GET | List all devices with current state |
| `/api/devices` | POST | Add a device |
| `/api/devices/{host}` | PUT | Update device config |
| `/api/devices/{host}` | DELETE | Remove device |
| `/api/devices/{host}/cmd` | POST | Send command (queued on failure) |
| `/api/devices/{host}/beeper/{on\|off}` | POST | Set beeper state |
| `/api/devices/{host}/lock` | POST | Lock/unlock temp override protection |

### Schedules
| Endpoint | Method | Description |
|---|---|---|
| `/api/schedules` | GET/POST | List or create schedules |
| `/api/schedules/{id}` | PUT/DELETE | Update or delete schedule |
| `/api/schedules/{id}/toggle` | POST | Enable/disable schedule |

### Usage
| Endpoint | Method | Description |
|---|---|---|
| `/api/usage/summary` | GET | Monthly summary (`?month=2026-07`) |
| `/api/usage/rolling30` | GET | Rolling 30-day totals + daily series |
| `/api/usage/export-csv` | GET | Download monthly CSV (`?month=2026-07`) |

### System
| Endpoint | Method | Description |
|---|---|---|
| `/api/` | GET | Status + version |
| `/api/health` | GET | Per-device watchdog status |
| `/api/settings` | GET/PUT | Rate and poll settings |
| `/api/logs` | GET | Automation log (`?level=warn+&limit=100`) |
| `/api/backup` | GET | Export config as JSON |
| `/api/restore` | POST | Restore from backup JSON |
| `/api/reset` | DELETE | Clear all usage data |

---

## Firmware

The `firmware/` directory contains fixed ESPHome YAML configs for the SMLIGHT SLWF-01pro:

- `slwf01pro24-fixed.yaml` — v2.4 firmware (recommended)
- `slwf-01pro-v21-fixed.yaml` — v2.1 firmware

Both include:
- `api: reboot_timeout: 0s` — prevents random 15-min reboots when no Home Assistant is connected
- `web_server: cors_allowed_origins: ["*"]` — allows dashboard to send commands

Flash via OTA:
```bash
pip install esphome
esphome run firmware/slwf01pro24-fixed.yaml
```

A `secrets.yaml` is required alongside the YAML:
```yaml
wifi_ssid: "YourWiFi"
wifi_password: "YourPassword"
air_conditioner_ota_password: "your-ota-password"
```

---

## Deployment (QNAP Container Station)

1. Pull the image from Docker Hub
2. Create container with port `8080:80`
3. Mount a volume at `/data` for persistence
4. Set `TZ=America/Costa_Rica` and `DATA_FILE=/data/hvac_state.json`
5. Set restart policy to **Always**

Add devices after first run:
```bash
curl -X POST http://your-qnap:8080/api/devices \
  -H "Content-Type: application/json" \
  -d '{"host":"air-conditioner-c44741.lan","name":"Master BR","btu":24000,"seer":20}'
```

---

## Branch Strategy

| Branch | Purpose |
|---|---|
| `main` | Stable development |
| `release` | Triggers Docker Hub build + push |

### Releasing

```bash
git checkout release
git merge main
git tag v1.1.0
git push origin release --tags
```

Builds multi-arch images (`linux/amd64` + `linux/arm64`) and pushes to Docker Hub with tags `latest`, `1.1.0`, `1.1`, `1`.

---

## GitHub Secrets Required

| Secret | Description |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub access token (Settings → Security → New Access Token) |

Set at: `GitHub repo → Settings → Secrets and variables → Actions`

---

## Hardware

- **AC units**: Innovair Bio-Inverter 12K/18K/24K BTU, 20 SEER, R32
- **Dongles**: SMLIGHT SLWF-01pro (ESP8266, Midea serial protocol)
- **Server**: QNAP NAS running Container Station
- **Network**: Amplifi router, all units on `192.168.133.x`
- **Location**: Playa del Coco, Guanacaste, Costa Rica
- **Electricity**: Coopeguanacaste (ARESEP flat tiered rates, no time-of-use)

---

## Developer

Dwight Mulcahy

---

## License

MIT
