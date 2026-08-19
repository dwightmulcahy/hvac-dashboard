#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  HVAC Dashboard — Kiosk Panel Setup (Raspberry Pi + touchscreen)
#
#  Automates steps 2–5 and 8 of docs/KIOSK.md: installs Chromium,
#  sets it to auto-launch fullscreen against your dashboard server,
#  disables screen blanking, optionally rotates the display, and
#  installs a watchdog to relaunch Chromium if it ever exits.
#
#  Steps 1 (flash the OS), 6 (assign PINs — that's dashboard-side,
#  not Pi-side), and 7 (verify) still happen outside this script —
#  see docs/KIOSK.md for those.
#
#  Usage:
#    DASHBOARD_URL=http://homenas.lan:8080/kiosk.html \
#      curl -sSL https://raw.githubusercontent.com/dwightmulcahy/hvac-dashboard/main/scripts/kiosk-setup.sh | bash
#
#  Optional:
#    ROTATE=2   # 0=normal 1=90° 2=180° 3=270°, if the panel is mounted rotated
#
#  Run as your normal desktop-login user (e.g. "pi"), NOT with sudo/root —
#  this needs to write into *your* home directory (~/.config/autostart,
#  ~/.config/systemd/user), and shells out to sudo itself for the few
#  steps that actually need root (package install, /boot config edit).
#  Running the whole thing as root would instead write those files into
#  /root's home, where the desktop session never looks for them — a
#  kiosk that silently boots to the normal desktop instead of Chromium
#  is the single most common failure mode this design avoids.
#
#  Safe to re-run: every step checks before writing, so running this
#  again (e.g. to pick up a changed DASHBOARD_URL) won't duplicate
#  autostart entries or config lines.
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── colours ────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[0;33m'
RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✓${RESET} $1"; }
info() { echo -e "${CYAN}→${RESET} $1"; }
warn() { echo -e "${YELLOW}⚠${RESET} $1"; }
err()  { echo -e "${RED}✗${RESET} $1"; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}── $1 ──${RESET}"; }

# ── must NOT run as root — see usage note above ──────────────────
if [[ $EUID -eq 0 ]]; then
  err "Don't run this with sudo/as root — run it as your normal login user (e.g. 'pi'). It calls sudo itself for the parts that need it."
fi

# ── config ─────────────────────────────────────────────────────
DASHBOARD_URL="${DASHBOARD_URL:-}"
ROTATE="${ROTATE:-}"
AUTOSTART_DIR="$HOME/.config/autostart"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}   HVAC Dashboard — Kiosk Panel Setup${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo ""

section "Preflight"

if [[ -z "$DASHBOARD_URL" ]]; then
  err "DASHBOARD_URL is required. Example:\n  DASHBOARD_URL=http://homenas.lan:8080/kiosk.html curl -sSL <script-url> | bash"
fi
if [[ "$DASHBOARD_URL" != http* ]]; then
  err "DASHBOARD_URL doesn't look like a URL: '$DASHBOARD_URL'"
fi
ok "Target: $DASHBOARD_URL"

if [[ -n "$ROTATE" && ! "$ROTATE" =~ ^[0-3]$ ]]; then
  err "ROTATE must be 0, 1, 2, or 3 (got '$ROTATE')"
fi

if ! command -v sudo &>/dev/null; then
  err "sudo not found — install it first (apt install sudo), or run each step from docs/KIOSK.md manually"
fi
# confirm sudo actually works before we're 10 steps into a partial
# install — better to fail here than after apt-get has already run
if ! sudo -n true 2>/dev/null && ! sudo -v; then
  err "This user needs sudo access (with or without a password prompt)"
fi
ok "sudo access confirmed"

# ── system update ────────────────────────────────────────────────
section "System update"
info "Updating and upgrading (this can take a few minutes on first run)..."
sudo apt-get update -qq
sudo apt-get full-upgrade -y -qq
ok "System up to date"

# ── install Chromium and unclutter ────────────────────────────────
section "Installing packages"
# package name varies by OS release — try chromium-browser first
# (what docs/KIOSK.md documents), fall back to chromium if that's
# not found, rather than hard-failing on a naming difference between
# Pi OS versions
if apt-cache show chromium-browser &>/dev/null; then
  CHROMIUM_PKG="chromium-browser"
  CHROMIUM_BIN="/usr/bin/chromium-browser"
elif apt-cache show chromium &>/dev/null; then
  CHROMIUM_PKG="chromium"
  CHROMIUM_BIN="/usr/bin/chromium"
else
  err "Neither 'chromium-browser' nor 'chromium' found in apt — check your Pi OS release"
fi
sudo apt-get install -y -qq "$CHROMIUM_PKG" unclutter
ok "$CHROMIUM_PKG and unclutter installed"

# ── auto-launch on boot ───────────────────────────────────────────
section "Kiosk autostart"
mkdir -p "$AUTOSTART_DIR"

# A separate launcher script, rather than putting the xset commands
# directly in the .desktop file's Exec= line — .desktop files have
# their own field-quoting rules (distinct from shell quoting), and
# embedding a compound "xset ...; exec chromium ..." command straight
# into Exec= is a known source of parser-specific breakage across
# different desktop environments. A plain script path in Exec= sidesteps
# that ambiguity entirely: the .desktop file only ever needs to quote
# one thing (a file path), and everything shell-flavored lives in a
# real shell script where normal shell quoting rules actually apply.
cat > "$AUTOSTART_DIR/kiosk-launch.sh" <<EOF
#!/usr/bin/env bash
# Written by kiosk-setup.sh — safe to edit, will be overwritten on re-run.
xset s off
xset -dpms
xset s noblank
exec $CHROMIUM_BIN --kiosk --app=$DASHBOARD_URL --noerrdialogs --disable-infobars --disable-session-crashed-bubble --disable-pinch --overscroll-history-navigation=0 --check-for-update-interval=31536000
EOF
chmod +x "$AUTOSTART_DIR/kiosk-launch.sh"

cat > "$AUTOSTART_DIR/kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=HVAC Kiosk
Exec=$AUTOSTART_DIR/kiosk-launch.sh
X-GNOME-Autostart-enabled=true
EOF
ok "Kiosk autostart entry written ($AUTOSTART_DIR/kiosk.desktop)"

cat > "$AUTOSTART_DIR/unclutter.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Unclutter
Exec=unclutter -idle 2
X-GNOME-Autostart-enabled=true
EOF
ok "Cursor-hiding autostart entry written"

# ── screen blanking ────────────────────────────────────────────────
section "Screen blanking"
# raspi-config's own screen-blanking toggle has been inconsistent
# across Pi OS releases and window-manager variants (X11/LXDE vs the
# newer Wayland/labwc default) in this project's own experience — see
# docs/KIOSK.md's step 4. The xset lines already written into
# kiosk-launch.sh above are the reliable fix docs/KIOSK.md documents
# for when raspi-config's toggle doesn't fully take, so this is
# already handled — raspi-config's own toggle is attempted too, purely
# as a best-effort extra layer, and its failure is not treated as fatal.
sudo raspi-config nonint do_blanking 1 2>/dev/null || true
ok "Screen blanking disabled (via xset in kiosk-launch.sh, plus raspi-config best-effort)"

# ── touchscreen rotation ───────────────────────────────────────────
if [[ -n "$ROTATE" ]]; then
  section "Touchscreen rotation"
  if [[ -f /boot/firmware/config.txt ]]; then
    BOOT_CONFIG=/boot/firmware/config.txt
  elif [[ -f /boot/config.txt ]]; then
    BOOT_CONFIG=/boot/config.txt
  else
    err "Couldn't find /boot/firmware/config.txt or /boot/config.txt"
  fi
  if grep -q "^lcd_rotate=" "$BOOT_CONFIG" 2>/dev/null; then
    sudo sed -i "s/^lcd_rotate=.*/lcd_rotate=$ROTATE/" "$BOOT_CONFIG"
    ok "Updated existing lcd_rotate=$ROTATE in $BOOT_CONFIG"
  else
    echo "" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    echo "# HVAC Dashboard kiosk rotation — added by kiosk-setup.sh" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    echo "lcd_rotate=$ROTATE" | sudo tee -a "$BOOT_CONFIG" >/dev/null
    ok "Added lcd_rotate=$ROTATE to $BOOT_CONFIG"
  fi
  warn "Reboot required for rotation to take effect. If touch input ends up mismatched from the display after that, that's a separate touch-calibration issue — see docs/KIOSK.md's step 5."
else
  info "No ROTATE set — skipping (panel assumed mounted right-side up)"
fi

# ── Chromium watchdog ──────────────────────────────────────────────
section "Chromium watchdog"
mkdir -p "$SYSTEMD_USER_DIR"
cat > "$SYSTEMD_USER_DIR/kiosk-watchdog.service" <<EOF
[Unit]
Description=Restart Chromium kiosk if it exits
After=graphical-session.target

[Service]
Type=simple
ExecStart=/bin/bash -c 'while true; do pgrep -x $(basename "$CHROMIUM_BIN") || $AUTOSTART_DIR/kiosk-launch.sh; sleep 5; done'
Restart=always

[Install]
WantedBy=default.target
EOF
# lets this user service keep running across logout/reboot without
# needing an active interactive session — without this, a systemd
# --user service can silently fail to start on boot
sudo loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
if systemctl --user enable --now kiosk-watchdog.service 2>/dev/null; then
  ok "Watchdog service installed and running"
else
  warn "Watchdog service installed but couldn't start now (needs an active session) — it'll start on next login/reboot"
fi

# ── summary ─────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Kiosk setup complete!${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Kiosk target : ${BOLD}$DASHBOARD_URL${RESET}"
echo ""
echo -e "  ${YELLOW}Still needed, outside this script:${RESET}"
echo -e "  1. On the dashboard itself: ${BOLD}Settings → Users${RESET} → set a 4-digit PIN for each household member"
echo -e "     (must be exactly 4 digits — the kiosk keypad only ever submits 4)"
echo -e "  2. Reboot this Pi to apply everything: ${CYAN}sudo reboot${RESET}"
echo ""
echo -e "  After reboot, confirm it boots straight to the PIN screen with no desktop/taskbar visible."
echo -e "  If it doesn't, see the Troubleshooting table in docs/KIOSK.md."
echo ""
