# HVAC Dashboard

[![Tests](https://github.com/dwightmulcahy/hvac-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/dwightmulcahy/hvac-dashboard/actions/workflows/tests.yml)
[![CI](https://github.com/dwightmulcahy/hvac-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/dwightmulcahy/hvac-dashboard/actions/workflows/ci.yml)
[![Docker Release](https://github.com/dwightmulcahy/hvac-dashboard/actions/workflows/docker-release.yml/badge.svg)](https://github.com/dwightmulcahy/hvac-dashboard/actions/workflows/docker-release.yml)
[![codecov](https://codecov.io/gh/dwightmulcahy/hvac-dashboard/branch/main/graph/badge.svg)](https://codecov.io/gh/dwightmulcahy/hvac-dashboard)
[![Docker Pulls](https://img.shields.io/docker/pulls/dwightmulcahy/hvac-dashboard)](https://hub.docker.com/r/dwightmulcahy/hvac-dashboard)
[![Docker Image Size](https://img.shields.io/docker/image-size/dwightmulcahy/hvac-dashboard/latest)](https://hub.docker.com/r/dwightmulcahy/hvac-dashboard)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/github/license/dwightmulcahy/hvac-dashboard)](./LICENSE)

Self-hosted dashboard for controlling and monitoring **Innovair mini-split AC units** via [SMLIGHT SLWF-01pro](https://smartlight.me) ESPHome dongles using the Midea serial protocol. Runs 24/7 in Docker on a QNAP NAS. All automation (scheduling, temperature guards, watchdog) executes server-side regardless of whether a browser is open.

See [`ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for how the backend is structured (module split, dependency graph, why it's organized this way), [`TESTING.md`](./docs/TESTING.md) for running the test suite, [`KIOSK.md`](./docs/KIOSK.md) for setting up a wall-mounted Raspberry Pi touchscreen panel, and [`CHANGELOG.md`](./CHANGELOG.md) for what changed in each release (auto-generated from commit history — see [Release Process](#release-process) below).

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
- **Schedules** — time + day-of-week per device, evaluated every minute server-side, resilient to poll-timing drift and missed-while-down recovery
- **Max temp guard** — auto-turns on AC when room exceeds configurable threshold, off when cooled (with hysteresis to prevent rapid cycling; guard hours only block the trigger, never the auto-off recovery)
- **Vacation mode** — turns off all units, sets high temp guard (configurable), pauses all schedules, optional auto-end after N days
- **Beeper sync** — saved beeper state read from device, not pushed to it
- **Reboot detection** — logs when dongle uptime resets
- **Nightly dongle reboot** — configurable time-of-day reboot of all dongles
- **Retry queue** — failed commands queued (capped at 10) and retried on recovery
- **Watchdog** — per-device configurable alert timeout; logs online/offline transitions with consecutive-failure counts
- **Maintenance reminders** — days-based (e.g. "filter change every 90 days") or runtime-hours-based (e.g. "coil clean every 500 hours") per device or whole-house; overdue items are logged and optionally pushed to a webhook, with a full completion history kept per item

### Notifications
- **Generic notification webhook** — set `notification_webhook` (Settings → General) to get a POST for device-offline, maintenance-overdue, and schedule-failure events. Separate from the Watchtower-specific webhook, which only forwards image-update notices.

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

### Kiosk Panel
Wall-mounted, PIN-locked touchscreen control panel — a separate, purpose-built UI (`kiosk.html`), not a cut-down version of the main dashboard. Fixed 800×480 layout, self-contained SVG icons (no CDN dependency), screensaver with idle timeout, role-aware controls (viewers can view devices and maintenance status but can't send commands or mark maintenance items complete — that needs operator or admin). Ships in the same Docker image; nothing extra to install. See [`KIOSK.md`](./docs/KIOSK.md) for the Raspberry Pi hardware setup.

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

Want a wall-mounted touchscreen panel too? Nothing extra to install — `kiosk.html` ships in this same container at `http://your-host:8080/kiosk.html`. See [`KIOSK.md`](./docs/KIOSK.md) for turning a Raspberry Pi + touchscreen into a dedicated panel.

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

### Auth
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/auth/login` | Log in, returns bearer token (rate-limited: 5 failed attempts locks out for 15 min) |
| POST | `/api/auth/logout` | Invalidate current token |
| POST | `/api/auth/change-password` | Change own password |
| POST | `/api/auth/recover` | Reset admin password using the one-time recovery key from Docker logs |
| GET | `/api/auth/me` | Current user info |
| GET/POST | `/api/auth/users` | List or add users (admin only) |
| DELETE | `/api/auth/users/{username}` | Delete user (admin only) |
| PUT | `/api/auth/users/{username}/role` | Change a user's role (admin only) |
| POST | `/api/auth/users/{username}/force-reset` | Force password change on next login (admin only) |

### Devices
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/devices` | List all devices with current state |
| POST | `/api/devices` | Add device |
| PUT | `/api/devices/{host}` | Update device config |
| DELETE | `/api/devices/{host}` | Remove device |
| POST | `/api/devices/reorder` | Persist drag-to-reorder |
| POST | `/api/devices/{host}/poll` | Poll a single device immediately |
| POST | `/api/devices/{host}/cmd` | Send command (queued on failure) |
| POST | `/api/devices/{host}/beeper/{on\|off}` | Set beeper |
| POST | `/api/devices/{host}/beeper/test` | Beep without changing saved state |
| POST | `/api/devices/{host}/lock` | Lock/unlock temp override |
| POST | `/api/devices/{host}/display-toggle` | Toggle unit display (requires IR LED hardware) |
| POST | `/api/devices/{host}/ota-upload` | Flash `.bin` firmware via ESPHome HTTP OTA |
| GET | `/api/devices/{host}/temp-history` | 48h hourly temp readings |
| GET | `/api/devices/{host}/health-history` | Last 50 online/offline/reboot events |
| GET | `/api/discover?subnet=192.168.x.0/24` | Scan network for ESPHome devices |

### Schedules
| Method | Endpoint | Description |
|---|---|---|
| GET/POST | `/api/schedules` | List or create (conflict-checked against overlapping device/time/day) |
| PUT/DELETE | `/api/schedules/{id}` | Update or delete |
| POST | `/api/schedules/{id}/toggle` | Enable/disable |

### Vacation mode
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/vacation` | Current vacation mode status |
| POST | `/api/vacation/{on\|off}` | Enable/disable — turns off all units, pauses schedules |

### Maintenance
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/maintenance` | List reminders with computed status (days or runtime-hours remaining, overdue, due-soon) |
| POST | `/api/maintenance` | Add reminder — days-based or runtime_hours-based, whole-house or device-scoped (admin only) |
| PUT | `/api/maintenance/{id}` | Edit reminder (admin only) |
| DELETE | `/api/maintenance/{id}` | Delete reminder (admin only) |
| POST | `/api/maintenance/{id}/complete` | Mark done — updates last-done date/runtime and appends to service history (operator or admin) |

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
| GET | `/api/health` | Per-device watchdog status, worker staleness |
| GET | `/api/health/push` | Uptime Kuma endpoint (200/503) |
| GET/PUT | `/api/settings` | Rate, poll, and automation settings |
| GET | `/api/exchange-rate` | Current USD/CRC rate (cached daily) |
| GET/DELETE | `/api/logs?level=warn+&limit=100` | Automation log |
| GET | `/api/backup` | Export config JSON (devices, schedules, settings — never users) |
| POST | `/api/restore` | Restore from backup (merges devices/settings, replaces schedules) |
| POST | `/api/watchtower-notify` | Watchtower webhook receiver — logs and forwards image updates |
| DELETE | `/api/reset` | Clear usage data |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATA_FILE` | `/data/hvac_state.json` | Path to the persisted state file |
| `LOG_FILE` | `<DATA_FILE>` with `_log.jsonl` suffix | Path to the JSONL automation log |
| `TZ` | container default | Timezone — affects schedule times, nightly reboot time, log timestamps |
| `CORS_ALLOWED_ORIGINS` | *(unset — CORS disabled)* | Comma-separated list of origins allowed to call the API cross-origin. The packaged deployment (nginx serves the dashboard and proxies `/api/` from the same origin) never needs this — only set it if you're running the frontend and API on genuinely different origins (a custom reverse proxy, a local dev server). Don't also add CORS headers in a reverse proxy in front of this container if you set this; duplicate `Access-Control-Allow-Origin` headers get rejected outright by browsers. |
| `LOG_FORMAT` | `text` | `text` (human-readable, e.g. `2026-08-26 08:53:11 [INFO] ...`) or `json` (one structured JSON object per log line — method/path/status/duration on every request via the access-log middleware, plus every other log call). Set `json` if you're shipping container logs to Loki, ELK, CloudWatch, or similar; leave as `text` for tailing `docker logs` directly. |

`APP_VERSION`, `GIT_SHA`, and `BUILD_DATE` are also read from the
environment (shown in the About modal and `/api/`), but are injected
automatically at build time from the git tag — not meant to be set
manually.

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

## Authentication

The dashboard uses token-based authentication with three role levels:

| Role | Access |
|---|---|
| **viewer** | Read-only — view dashboard, no commands |
| **operator** | Send commands (on/off/mode/temp), mark maintenance items complete, view all |
| **admin** | Full access — settings, devices, schedules, users, maintenance reminder CRUD |

Login is rate-limited: 5 failed attempts for the same username+IP within 15 minutes locks that combination out for 15 minutes (HTTP 429).

### First Login

Default credentials on first run: **`admin` / `admin`**

You will be forced to change the password before accessing the dashboard.

### Managing Users

Open `··· → Users` (admin only) to add, delete, or change roles for users.

### Kiosk PINs

For the wall-mounted touchscreen panel (see [`KIOSK.md`](./docs/KIOSK.md)), assign each user a 4-digit PIN from the same Users screen. PIN login uses the same role permissions and the same lockout rules as password login (5 failed attempts locks that PIN's client IP for 15 minutes) — it's a separate credential on the same account, not a separate access tier. PINs must be exactly 4 digits; the kiosk's on-screen keypad won't accept longer ones even though the API allows up to 6.

### Forgot Password

If you lose the admin password:

**1 — Get the recovery key from Docker logs:**
```bash
docker logs hvac-dashboard 2>&1 | grep RECOVERY
```
Output:
```
WARNING  === RECOVERY KEY: abc123xyz_your_key_here ===
WARNING  === Use POST /api/auth/recover with this key to reset admin password ===
```

**2 — Reset via the dashboard:**

Click **Forgot password?** on the login screen, paste the recovery key, and set a new password.

**Or via curl:**
```bash
curl -X POST http://homenas.local:8080/api/auth/recover \
  -H "Content-Type: application/json" \
  -d '{"recovery_key":"abc123xyz_your_key_here","new_password":"newpassword"}'
```

The recovery key is generated fresh on every container start, is single-use, and is never written to disk — it only exists in memory and in the Docker logs.

---

## Development & Testing

Backend is split across `api.py`, `auth.py`, `state.py`, `models.py`, `worker.py`, `maintenance_logic.py`, `notify.py`, `logging_config.py`, and `routers/*.py` — see [`ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the module map and why it's organized this way.

```bash
pip install -r requirements-dev.txt
pytest
```

454 tests (99% line coverage) covering state persistence, auth, the max-temp guard (including regression tests for real bugs hit during development), schedule logic, maintenance status/overdue detection, CORS configuration, structured/access logging, full HTTP integration, and backup/restore. See [`TESTING.md`](./docs/TESTING.md) for details on running specific tests, coverage, and the isolation pattern the fixtures rely on — including the JavaScript test suites for both `hvac-dashboard.html` and `kiosk.html`.

Three GitHub Actions workflows run on push/PR:
- **`tests.yml`** — pytest suite + ruff (lint) + dashboard/kiosk JS syntax checks + eslint/prettier + JS unit tests
- **`ci.yml`** — builds the real Docker image, boots the container, curls live endpoints, validates nginx config, lints all Python files
- **`docker-release.yml`** — builds and pushes multi-arch images to Docker Hub, triggered only on version tags

---

## Release Process

```bash
git checkout release
git merge main
git tag v1.2.0
git push origin release --tags
```

GitHub Actions builds multi-arch (`amd64` + `arm64`) images and pushes to Docker Hub with tags `latest`, `1.2.0`, `1.2`, `1`. Version is injected from the git tag via `APP_VERSION` build arg.

Once the image is built, scanned, and pushed, the same workflow creates a [GitHub Release](../../releases) for the tag — this is also what makes tags show up as Releases in the sidebar rather than just tags. Its body is auto-generated from commit history by [git-cliff](https://git-cliff.org) (config: `cliff.toml`), grouping commits by type (Features, Bug Fixes, Dependencies, etc.) based on the [conventional commit](https://www.conventionalcommits.org) prefix — `feat:`, `fix:`, `docs:`, and so on — each commit was made with. `CHANGELOG.md` is the same content covering the full project history; it's a static file checked into the repo, not regenerated automatically on every release, so it'll need a manual `git-cliff -o CHANGELOG.md` + commit after tagging if you want it to stay current.

This workflow only ever creates a Release for a *new* tag going forward — it doesn't retroactively backfill Releases for tags that already existed before it was added. For that, `scripts/backfill-releases.sh` is a one-time script that walks every existing tag and creates the matching Release from that tag's own changelog section (requires `gh` and `git-cliff` installed locally, authenticated with `gh auth login`). Safe to re-run — tags that already have a Release are skipped, not duplicated.

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

[MIT](./LICENSE)
