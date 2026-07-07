# HVAC Dashboard

A self-hosted web dashboard for controlling and monitoring **Innovair mini-split AC units** via [SMLIGHT SLWF-01pro](https://smartlight.me) ESPHome dongles running the Midea protocol.

Built for a 6-unit installation in Playa del Coco, Guanacaste, Costa Rica — running 24/7 on a QNAP NAS via Docker.

---

## Features

- **Live control** — mode, temperature, power per unit
- **Real-time stats** — indoor/outdoor temp (°C + °F), estimated watts, on-time, dongle uptime
- **Scheduling** — time + day-of-week schedules per device
- **Max temp guard** — auto-turns on AC if room exceeds a threshold
- **Usage tracking** — kWh and runtime per device, monthly summaries, rolling 30-day bar charts
- **Cost estimation** — tiered Coopeguanacaste rates (₡/kWh), configurable
- **Beeper sync** — persists beeper state across reboots
- **Auto-poll** — every 60 seconds with retry logic
- **Server-side persistence** — FastAPI + JSON on a Docker volume

---

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/hvac-dashboard.git
cd hvac-dashboard
docker-compose up -d
```

Open `http://your-host:8080`

---

## Docker Hub

Pre-built images available at:

```
docker pull YOUR_DOCKERHUB_USERNAME/hvac-dashboard:latest
```

Run directly without cloning:

```bash
docker run -d \
  --name hvac-dashboard \
  --restart always \
  -p 8080:80 \
  -v hvac-data:/data \
  YOUR_DOCKERHUB_USERNAME/hvac-dashboard:latest
```

---

## Configuration

Edit device hostnames/IPs in the **⚙ Devices** panel in the UI — saved to browser localStorage and persisted across sessions.

### Rate settings

Click **⚡ Rates** to configure:
- Provider name
- Exchange rate (₡/USD)
- Estimated monthly kWh (determines which tier applies)
- Tiered or flat rate
- AC daily runtime hours

Default tiers (Coopeguanacaste 2026):

| Block | Rate |
|---|---|
| 0–200 kWh | ₡62/kWh (~$0.117) |
| 201–500 kWh | ₡82/kWh (~$0.155) |
| 500+ kWh | ₡140/kWh (~$0.264) |

---

## API

The usage persistence API runs on port 8081 internally, proxied via nginx at `/api/`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/` | GET | Health check |
| `/api/record` | POST | Record usage snapshot |
| `/api/summary` | GET | Monthly summary (`?month=2026-07`) |
| `/api/rolling30` | GET | Rolling 30-day totals + daily series |
| `/api/history` | GET | Raw daily records (`?days=60&device=host`) |
| `/api/reset` | DELETE | Clear all data |

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

---

## Deployment (QNAP Container Station)

1. Pull the image from Docker Hub
2. Create container with port `8080:80`
3. Mount a volume at `/data` for usage persistence
4. Set restart policy to **Always**

Or use the included `docker-compose.yml`.

---

## Branch Strategy

| Branch | Purpose |
|---|---|
| `main` | Stable development |
| `develop` | Active work |
| `release` | Triggers Docker Hub build + push |

### Releasing

```bash
git checkout release
git merge main
git tag v1.0.0
git push origin release --tags
```

This triggers the GitHub Action which builds multi-arch images (`amd64` + `arm64`) and pushes to Docker Hub with tags `latest`, `1.0.0`, `1.0`, `1`.

---

## GitHub Secrets Required

| Secret | Description |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub access token (not password) |

Set these at: `GitHub repo → Settings → Secrets and variables → Actions`

---

## Hardware

- **AC units**: Innovair Bio-Inverter 24K/18K/12K BTU, 20 SEER, R32
- **Dongles**: SMLIGHT SLWF-01pro (ESP8266, Midea serial protocol)
- **Server**: QNAP NAS running Container Station
- **Network**: Amplifi router, all units on `192.168.133.x`

---

## License

MIT
