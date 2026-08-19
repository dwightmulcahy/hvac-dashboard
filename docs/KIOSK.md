# Kiosk Touchscreen Setup

Wall-mounted, PIN-locked control panel for a subset of the dashboard — status and quick control for all 6 units, sized for an 800×480 touchscreen. See [`kiosk.html`](../frontend/kiosk.html) for what it does and doesn't cover; this doc is about the Raspberry Pi side: turning a Pi + touchscreen into a dedicated panel that always shows it. If you're looking for the automated test suite that exercises `kiosk.html` itself rather than the physical hardware setup, see [`TESTING.md`](./TESTING.md).

There's no separate container or install for the kiosk itself. `kiosk.html` ships in the same Docker image as the main dashboard (see [`ARCHITECTURE.md`](./ARCHITECTURE.md)) and is served from the same origin — the Pi's only job is running a browser in kiosk mode, pointed at a URL. Nothing here needs updating when a new dashboard release ships; the Pi just keeps loading whatever's live on the server.

---

## Prerequisite

The main container needs to already be running and reachable on your network — e.g. `http://homenas.lan:8080`. If you haven't set that up yet, start with the main [`README.md`](../README.md).

---

## Hardware

- **Raspberry Pi 4 (2GB+)** or newer. A Pi Zero 2 W can technically run Chromium but feels sluggish for anything beyond a static page; a 4 is the comfortable floor.
- **An 800×480 touchscreen.** The layout is fixed at this resolution — the official 7" Raspberry Pi touchscreen matches it, as do most generic HDMI+USB-touch panels sold at this size. Larger panels will letterbox rather than stretch (see [Troubleshooting](#troubleshooting)).
- microSD card (16GB+), 5V/3A USB-C power supply.

---

## 1. Flash the OS

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and choose **Raspberry Pi OS (64-bit), with desktop** — not the Lite variant. Lite has no GUI at all, which means no way to run a browser; you'd have to hand-install and configure X11 and a window manager yourself for no real benefit on hardware that can comfortably run the full desktop image.

In the Imager's advanced options (gear icon) before writing, set:
- Hostname (e.g. `hvac-kiosk`)
- Enable SSH, set a password — you won't have a keyboard attached day-to-day
- Wi-Fi SSID/password, if not wired

Boot the Pi, SSH in, and update it:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## 2. Install Chromium and a couple of small utilities

```bash
sudo apt install -y chromium-browser unclutter
```

`unclutter` hides the mouse cursor when idle — most USB touch panels enumerate as a mouse, and a visible cursor floating over a wall panel looks unfinished.

---

## 3. Auto-launch the kiosk on boot

Create an XDG autostart entry — this works the same way across both window-manager variants current Raspberry Pi OS ships with (X11/LXDE on older installs, labwc/Wayland on current Bookworm-based ones), so it doesn't matter which one you end up on:

```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/kiosk.desktop
```

```ini
[Desktop Entry]
Type=Application
Name=HVAC Kiosk
Exec=/usr/bin/chromium-browser --kiosk --app=http://homenas.lan:8080/kiosk.html --noerrdialogs --disable-infobars --disable-session-crashed-bubble --disable-pinch --overscroll-history-navigation=0 --check-for-update-interval=31536000
X-GNOME-Autostart-enabled=true
```

Replace the URL with your actual server address. What each flag is doing:

| Flag | Why |
|---|---|
| `--kiosk` | Fullscreen, no address bar, tabs, or window chrome |
| `--app=URL` | Opens directly to the kiosk, not a blank new-tab page |
| `--noerrdialogs` | Suppresses the "Chromium didn't shut down correctly" prompt after a power cut — kiosks get power-cycled, this dialog would otherwise block the screen every time |
| `--disable-infobars` | Removes the "Chrome is being controlled by automated software" bar |
| `--disable-session-crashed-bubble` | Same idea as `--noerrdialogs`, belt-and-suspenders |
| `--disable-pinch` | Blocks pinch-zoom — the layout is fixed-size, zooming breaks it |
| `--overscroll-history-navigation=0` | Blocks the swipe-left/right "back/forward" gesture, which a touch panel triggers easily by accident |
| `--check-for-update-interval` | Set absurdly high — no reason for Chromium to phone home checking for its own updates on an offline-ish kiosk |

Add `unclutter -idle 2 &` on its own line (or its own autostart entry) if you installed it above.

---

## 4. Stop the Pi's screen from blanking

Raspberry Pi OS blanks the display after a period of inactivity by default. `kiosk.html` already has its own screensaver (see the main project docs) — you don't want the OS *also* trying to sleep the display, since the two won't agree on timing and you'll end up with either a truly-off screen the touch panel can't wake, or fighting behavior.

```bash
sudo raspi-config
```
Under **Display Options → Screen Blanking**, disable it. Reboot to confirm it stuck — some Pi OS versions need a `xset s off` / `xset -dpms` line added to the autostart entry as well if `raspi-config`'s toggle doesn't fully take, in which case add that as its own line before the `chromium-browser` line in `kiosk.desktop`.

---

## 5. Touchscreen orientation

If the panel is mounted upside-down or sideways relative to how it reports itself, add the appropriate line to `/boot/firmware/config.txt` (or `/boot/config.txt` on older OS versions):

```
# 180° mount:
lcd_rotate=2
```

Reboot after editing. If touch input ends up mismatched from the (correctly rotated) display after this — taps landing in the wrong place — that's a separate `xinput` touch-calibration issue, not a display one; specifics vary by panel, so check your touchscreen's documentation for its calibration tool.

---

## 6. Assign PINs

This is the one step that happens on the *dashboard* side, not the Pi. The kiosk has no PINs configured out of the box — go to **Settings → Users** on the main dashboard and set a 4-digit PIN for each household member who should have kiosk access.

**Must be exactly 4 digits.** The backend accepts 4-6 digit PINs for flexibility, but the kiosk's on-screen keypad only ever submits 4 — a longer PIN would silently never work here.

---

## 7. Verify

Reboot the Pi and confirm, in order:
1. It boots straight to the PIN entry screen, no desktop/taskbar visible anywhere
2. A wrong PIN shows an error and doesn't crash anything
3. A correct PIN unlocks into the grid, and the header badge shows a version number and your role
4. Tapping a tile opens its detail view; the back header returns you to the grid
5. Leaving a detail view untouched for ~15s auto-returns to the grid
6. Leaving the whole thing untouched for 10 minutes drops it into the dim screensaver; tapping that goes back to the PIN screen, not straight into the grid

If step 1 fails and you land on the desktop instead, double check `~/.config/autostart/kiosk.desktop`'s syntax — a malformed `.desktop` file fails silently rather than erroring visibly.

---

## Keeping Chromium itself running

Chromium is generally stable, but for a device nobody's going to babysit, it's worth having something notice if it ever exits and relaunch it rather than leaving the panel dark. A small systemd user service works:

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/kiosk-watchdog.service
```

```ini
[Unit]
Description=Restart Chromium kiosk if it exits
After=graphical-session.target

[Service]
Type=simple
ExecStart=/bin/bash -c 'while true; do pgrep -x chromium-browser || /usr/bin/chromium-browser --kiosk --app=http://homenas.lan:8080/kiosk.html --noerrdialogs --disable-infobars --disable-session-crashed-bubble --disable-pinch --overscroll-history-navigation=0; sleep 5; done'
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now kiosk-watchdog.service
```

This is optional — skip it if you'd rather keep the setup minimal and just deal with a rare crash by power-cycling.

---

## Updates

Nothing to do here. `kiosk.html` is served live from the same container as the main dashboard — the moment you deploy a new release, every kiosk pointed at that server picks it up on its next page load (or the next time it's manually reloaded, if you want it sooner). There's no separate kiosk build, image, or version to keep in sync.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Boots to desktop, not the kiosk | `kiosk.desktop` syntax error, or it's in the wrong directory (`~/.config/autostart/`, not `/etc/xdg/autostart/` unless you specifically want it system-wide) |
| Screen goes black and stays black | OS-level screen blanking wasn't actually disabled — see [step 4](#4-stop-the-pis-screen-from-blanking) |
| "Can't reach the server — retrying…" banner permanently | Wrong URL/IP in `kiosk.desktop`, server container isn't running, or the Pi and server aren't on the same network/VLAN |
| Layout is letterboxed or cropped | Panel isn't actually 800×480 — check its native resolution; the layout doesn't reflow to other sizes |
| Taps land in the wrong place | Touch calibration, not display rotation — see [step 5](#5-touchscreen-orientation) |
| Visible mouse cursor sitting on screen | `unclutter` not installed, or not added to the autostart entry |
| PIN entry does nothing after 4 digits | Confirm the PIN was set as exactly 4 digits in Settings → Users, not more |
