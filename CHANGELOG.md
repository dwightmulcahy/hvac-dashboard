# Changelog
All notable changes to this project are documented here, generated
from conventional commit messages via [git-cliff](https://git-cliff.org).
## [Unreleased]

### 🐛 Bug Fixes

- 3 minutes → 10 minutes kiosk timeout

## [1.14.0] - 2026-08-16

### 🚀 Features

- Added maintance reminders for devices based on time/day usage

## [1.13.1] - 2026-08-16

### 🐛 Bug Fixes

- On-time was cumlative since the device was added, made it daily now

- Failing test


### ✨ Improvements

- *(kiosk)* Visual improvements for readablity


### 🧪 Testing

- *(kiosk)* Expanded testing for all the kiosk code


### ⚙️ Chores

- Fixed the package.lock missing

## [1.13.0] - 2026-08-14

### 🚀 Features

- Added a kiosk dashboard for use with a 800 x 480 raspberry pi touchscreen


### 🐛 Bug Fixes

- *(test)* One of the auth tests had a stray import


### ✨ Improvements

- *(kiosk)* Cleaned up UI for kiosk for better usage

- *(kiosk)* Added username in header and debounce logic to buttons

## [1.12.0] - 2026-08-14

### 🚀 Features

- Added if the dongle has the IR emitter on it


### 🐛 Bug Fixes

- Using .trivyignore to ignore files with transitive dependency issues

- Each field now shows its value converted to the currently selected unit (°C/°F)

- Column headings for the device dialog were not lining up

- Tests/ regressed.

## [1.11.5] - 2026-08-13

### 🐛 Bug Fixes

- Settings UI — visible unit selection, live temp re-render, sticky save, backup date, watchtower tooltip

- A bunch of setting dialog fix/improvements

- Updated extract.js to latest

- Updated dependency versions that had reported vulnerablity


### ⚙️ Chores

- *(trivy)* Added ignore-unfixed and comments to avoid unfixed issues stopping ci


### 📦 Dependencies

- *(deps)* Update pytest-mock requirement from >=3.14.0 to >=3.15.1

- *(deps)* Update uvicorn requirement from >=0.29.0 to >=0.52.1

- *(deps)* Update pytest-asyncio requirement from >=0.24.0 to >=1.4.0 (#9)

- *(ci)* Bump docker/metadata-action from 5 to 6 (#1)

- *(docker)* Bump python from 3.12-slim to 3.14-slim (#2)

- *(ci)* Bump actions/setup-python from 5 to 7 (#4)

- *(ci)* Bump actions/checkout from 4 to 7 (#5)

- *(deps)* Update pydantic requirement from >=2.6.0 to >=2.13.4 (#6)

- *(deps)* Update python-multipart requirement (#8)

- *(ci)* Bump docker/setup-buildx-action from 3 to 4 (#7)

## [1.11.4] - 2026-08-11

### ⚙️ Chores

- Workflows now correctly use aquasecurity/trivy-action@v0.36.0

- *(trivy)* Added ignore-unfixed to avoid unfixed issues stopping ci

## [1.11.3] - 2026-08-11

### 🐛 Bug Fixes

- Signal.signal(SIGTERM, ...) was crashing test runner, deprecated .dict() -> .model_dump()

- Ci.yml now lints all the python files in the repo


### 🚜 Refactor

- Split large files into managable structure, added tests


### 📚 Documentation

- Improved readme files for future-me


### 🧪 Testing

- Add tests for the backup/restore logic

- Add js tests for all the pure utility functions

- *(js)* Made the extraction of functions to test more robust

- *(secutiry)* Scan for security issues

- *(coverage)* Add test suites to get coverage above 80%

- *(coverage)* Add more tests to get coverage to 100% for most .py


### ⚙️ Chores

- *(security)* Added depentabot config file

- Added pip caching to decrease ci run time

## [1.11.2] - 2026-08-05

### 🐛 Bug Fixes

- Remove leftover client-side max-temp monitor that duplicated and conflicted with server-side guard

- Define missing fetchState(), remove ~13 dead functions/constants, fix duplicate CSS selector

- Prevent worker task GC and UnboundLocalError crash, deprecate-proof datetime.utcnow, narrow bare excepts, declare pydantic dependency

- Eliminate duplicate schedule-command logic via shared helper, add USER_NOT_FOUND constant, use log.exception for real errors, catch remaining bare excepts

- Remove duplicate || operand bug, clean up 4 unused variables, remove redundant trendColor assignment

## [1.11.1] - 2026-08-04

### 🐛 Bug Fixes

- Eliminate duplicate mode-change log entries, debounce discovery scans

- Missed schedule detection now also covers end times, not just start times

## [1.11.0] - 2026-08-03

### 🚀 Features

- Read-only role tightening, discovery smart naming + subnet persistence, config export/import, test-all devices

- Login rate limiting, toast notifications, fix vacation TZ bug, export reminder

## [1.10.1] - 2026-08-03

### 🐛 Bug Fixes

- Device save uses PUT, renders tiles immediately after name change

- Close schedule modal on section/settings open, remove duplicate closeSchModal

- OpenUserManagement reference error, disable all tile controls for viewer role

- Hide temp/power/lock/beeper controls entirely for read-only role instead of just disabling

- Hide schedule edit/delete/toggle controls and + Schedule button for read-only role

## [1.10.0] - 2026-08-03

### 🚀 Features

- Log source of mode changes (by user vs detected externally vs scheduled vs auto)

- Nightly dongle reboot at configurable time, default 03:00

- ESPHome device discovery — scan subnet for Midea climate devices


### 🐛 Bug Fixes

- Clear retry queue and device state when host changes

- Connection test uses dedicated poll endpoint, DOM-based rendering avoids parser issues

- Discovery resolves hostnames to IPs for accurate already_configured check

## [1.9.0] - 2026-07-31

### 🚀 Features

- Forgot password recovery via one-time key printed to Docker logs on startup


### 🐛 Bug Fixes

- Overnight schedule end time day check, max temp auto-off not blocked by guard hours:

- Schedule modal loads devices first, inline add-user errors, force pw change for new users


### 📚 Documentation

- *(password)* Updated README.md with password reset process

## [1.8.0] - 2026-07-30

### 🚀 Features

- User authentication with admin/operator/viewer roles, force password change on first login

## [1.7.0] - 2026-07-30

### 🚀 Features

- Log API start/stop events, save state on SIGTERM

- Persist max_temp_active state, detect missed schedules on startup, backup rotation

- Retry queue cap, consecutive failure tracking, max temp hysteresis

- Poll jitter, vacation time limit, schedule conflict detection, improved health endpoint

- Persistent log file survives restarts, outdoor temp trend indicator on tiles


### 🐛 Bug Fixes

- Log unreachable only once on first detection, remove noisy client-side log entries

- Retry failed schedule commands via device retry queue

- Null-guard all getElementById calls in openScheduleModal to prevent silent failure

- Log all mode changes including startup state, robust ISO sort, 500 entry server log buffer

- Infer max temp guard active state on API restart to prevent stuck auto-cool

## [1.6.2] - 2026-07-29

### 📚 Documentation

- *(readme)* Added my email addr


### ⚙️ Chores

- *(release)* Only build on tag pushes, not branch pushes

## [1.6.1] - 2026-07-27

### 🚀 Features

- Auto-update banner when API version differs from dashboard version


### 🐛 Bug Fixes

- Retry API status check on init to clear stale offline badge

- Log entries store ISO timestamp for correct cross-midnight sort ordering

## [1.6.0] - 2026-07-25

### 🚀 Features

- Health history, firmware version, actual watts, watchtower webhook, temp unit preference


### 🐛 Bug Fixes

- Remove log spam - silent vacation init, remove poll/API status noise

- Broken catch block with // comment consuming closing brace, causing JS crash

- Beeper sync reads from device instead of pushing saved state, fixes stale display


### 🚜 Refactor

- Clean general settings layout, time guard hours, API-based device test, compact device row buttons

## [1.5.1] - 2026-07-24

### 🚀 Features

- Reboot dongle button in connection test modal

- Vacation mode and schedules server-side, verbose logging toggle, 2min default poll


### 🐛 Bug Fixes

- Clear log also clears server-side logs via DELETE /api/logs

## [1.5.0] - 2026-07-16

### 🚀 Features

- Max temp guard restores previous mode and setpoint after auto-cool

- Startup retry backoff, time-based max temp guard, schedule end time, OTA firmware flash

- Add ESPHome version text sensor to YAML, display fw version in tile footer


### 🐛 Bug Fixes

- Raise schedule modal z-index above section modal so it appears on top

- Max temp guard always sets COOL mode + target temp, handles FAN/HEAT/DRY modes

## [1.4.0] - 2026-07-14

### 🚀 Features

- Redesign stats bar — combined temps tile, next schedule, clickable cards

- Est load tile shows amps and active units, section modal centered over page

- Voltage setting in General (120V/240V), amps in Est load tile uses selected voltage

- Add tap hints to Est cost and Next schedule stat tiles


### 🚜 Refactor

- Simplify next schedule tile when no schedules exist

- Remove redundant 'no schedules' text from next schedule tile

## [1.3.0] - 2026-07-14

### 🚀 Features

- Poll button in header, hardware links, fix SEER bug, global watchdog, theme in settings

- Use device step/min/max for temp control, support 0.5°C increments


### 🐛 Bug Fixes

- Align device list column headers by removing auto buttons column from header grid


### ⚡ Performance

- Patch tiles in-place on poll to eliminate UI flash


### 🚜 Refactor

- Remove subtitle from header, add section shortcuts to ··· menu

- Update links in About dialog

- Remove duplicate + Schedule from ··· menu

## [1.2.0] - 2026-07-11

### 🚀 Features

- Uptime kuma push endpoint, auto exchange rate, fix live rate CORS, debounce focus refresh

- Grid layout for devices tab, version from git tag via build args

- Hourly temp history with sparkline per tile, exchange rate auto-update


### 🐛 Bug Fixes

- Call renderAll() in pollAll so tiles update on every poll

- Persist device order via /api/devices/reorder endpoint, survives poll


### 🚜 Refactor

- Consolidate settings into single menu item, reorder tabs to General/Devices/Rates

- Fix device grid alignment, consolidate cost/projection card, update README

- Tighten tile spacing, simplify est cost card to two-line style

- Cleaned up the est cost tile, removed duplicate info

## [1.1.2] - 2026-07-11

### 🚀 Features

- Drum-roll time picker, refresh on focus, post-schedule tile refresh, last_run display fix


### ⚙️ Chores

- *(release)* Rethought dockerhub tagging strategy

## [1.1.1] - 2026-07-10

### 🚀 Features

- Add column headers to devices panel

- Drag-to-reorder devices, remove copy button, placeholder styling for new devices


### 🐛 Bug Fixes

- Schedule last_run stores date+time to allow daily re-firing

- Test beep uses dedicated endpoint that restores saved beeper state

- Replace non-functional test beep with connection latency test


### ✨ Improvements

- Moved the watchdog label into the input field


### 🚜 Refactor

- Only show last_seen indicator when device is outside watchdog threshold


### 📚 Documentation

- *(readme)* Updated with all new features

## [1.1.0] - 2026-07-08

### 🚀 Features

- Add About modal with version, links, and developer info

- *(reliability)* Watchdog per device, retry queue, health endpoint, backup/restore

- Add WiFi signal strength to device tile footer

- *(rates)* Now gets the current exchange rates and updated rate tiers

- *(quick-wins)* Reset on-time, copy hostname, test beep, last polled indicator

- Override protection per device with lock/unlock button


### 🐛 Bug Fixes

- Corrected text in the About dialog


### 📚 Documentation

- *(fireware)* Added the stock firmware yaml

## [1.0.2] - 2026-07-07

### 🐛 Bug Fixes

- Add requirements.txt with httpx dependency

- Resolve pyflakes warnings in api.py

## [1.0.1] - 2026-07-07

### 🐛 Bug Fixes

- Use lifespan instead of deprecated on_event, fix schedule toggle endpoint

## [1.0.0] - 2026-07-07

### 🐛 Bug Fixes

- Remove unused imports and variables (pyflakes)

- Remove dockerhub-description step, causes Forbidden error with access tokens


### ✨ Improvements

- Moved all api calls into api.py and dashboard calls it


