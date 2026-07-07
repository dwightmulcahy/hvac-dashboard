#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  HVAC Dashboard — Raspberry Pi 4 Setup Script
#  Run as: bash setup.sh
#  Tested on: Raspberry Pi OS Lite 64-bit (Bookworm)
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

# ── must run as root ────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  err "Run as root: sudo bash setup.sh"
fi

# ── config — edit these before running ─────────────────────────
STATIC_IP="192.168.133.50"       # IP to assign to the Pi
GATEWAY="192.168.133.1"          # Your router IP
DNS="192.168.133.1"              # Usually same as gateway
INTERFACE="eth0"                 # eth0 for wired, wlan0 for WiFi
HOSTNAME="hvac-dashboard"        # Pi hostname on the network
DASHBOARD_FILE="hvac-dashboard.html"  # Must be in same dir as this script
WEBROOT="/var/www/html"
NGINX_CONF="/etc/nginx/sites-available/hvac"
PORT=80
# ───────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}   HVAC Dashboard — Pi Setup${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo ""

# ── check dashboard file exists ─────────────────────────────────
section "Preflight"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SCRIPT_DIR/$DASHBOARD_FILE" ]]; then
  warn "Dashboard file '$DASHBOARD_FILE' not found next to this script."
  warn "You can copy it later with:"
  warn "  scp hvac-dashboard.html pi@$STATIC_IP:$WEBROOT/index.html"
  SKIP_DASHBOARD=true
else
  SKIP_DASHBOARD=false
  ok "Dashboard file found"
fi

# ── update system ───────────────────────────────────────────────
section "System update"
info "Updating package lists..."
apt-get update -qq
ok "Package lists updated"

info "Upgrading installed packages..."
apt-get upgrade -y -qq
ok "System upgraded"

# ── install packages ────────────────────────────────────────────
section "Installing packages"
apt-get install -y -qq nginx curl git unattended-upgrades
ok "nginx, curl, git, unattended-upgrades installed"

# ── set hostname ────────────────────────────────────────────────
section "Hostname"
OLD_HOSTNAME=$(hostname)
if [[ "$OLD_HOSTNAME" != "$HOSTNAME" ]]; then
  hostnamectl set-hostname "$HOSTNAME"
  sed -i "s/$OLD_HOSTNAME/$HOSTNAME/g" /etc/hosts
  ok "Hostname set to '$HOSTNAME'"
else
  ok "Hostname already '$HOSTNAME'"
fi

# ── static IP ───────────────────────────────────────────────────
section "Static IP"
DHCPCD="/etc/dhcpcd.conf"
if grep -q "static ip_address=$STATIC_IP" "$DHCPCD" 2>/dev/null; then
  ok "Static IP already configured ($STATIC_IP)"
else
  # Back up existing config
  cp "$DHCPCD" "$DHCPCD.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  cat >> "$DHCPCD" <<EOF

# HVAC Dashboard static IP — added by setup.sh
interface $INTERFACE
static ip_address=$STATIC_IP/24
static routers=$GATEWAY
static domain_name_servers=$DNS
EOF
  ok "Static IP configured: $STATIC_IP on $INTERFACE"
fi

# ── nginx config ────────────────────────────────────────────────
section "Nginx"

# Remove default site
rm -f /etc/nginx/sites-enabled/default

# Write HVAC site config
cat > "$NGINX_CONF" <<EOF
server {
    listen $PORT default_server;
    listen [::]:$PORT default_server;

    root $WEBROOT;
    index index.html;
    server_name _;

    # Enable compression
    gzip on;
    gzip_types text/html text/css application/javascript;

    # Cache static assets
    location ~* \.(css|js|png|jpg|ico|woff2)$ {
        expires 1d;
        add_header Cache-Control "public";
    }

    # Dashboard — no cache so updates show immediately
    location / {
        try_files \$uri \$uri/ =404;
        add_header Cache-Control "no-store";
        add_header X-Frame-Options "SAMEORIGIN";
    }

    # Health check endpoint
    location /health {
        return 200 'ok';
        add_header Content-Type text/plain;
    }

    # Block access to dot files
    location ~ /\. {
        deny all;
    }
}
EOF

# Enable site
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/hvac

# Test config
nginx -t -q
ok "Nginx config valid"

# Reload nginx
systemctl enable nginx -q
systemctl restart nginx
ok "Nginx enabled and running"

# ── copy dashboard file ─────────────────────────────────────────
section "Dashboard"
if [[ "$SKIP_DASHBOARD" == false ]]; then
  cp "$SCRIPT_DIR/$DASHBOARD_FILE" "$WEBROOT/index.html"
  chown www-data:www-data "$WEBROOT/index.html"
  chmod 644 "$WEBROOT/index.html"
  ok "Dashboard deployed to $WEBROOT/index.html"
else
  warn "Skipped — copy manually when ready:"
  warn "  scp hvac-dashboard.html pi@$STATIC_IP:$WEBROOT/index.html"
fi

# ── auto-updates (security only) ────────────────────────────────
section "Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<EOF
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
ok "Unattended security upgrades enabled"

# ── update script ───────────────────────────────────────────────
section "Update helper"
cat > /usr/local/bin/hvac-update <<'EOF'
#!/usr/bin/env bash
# Usage: sudo hvac-update /path/to/hvac-dashboard.html
# Or from Mac: scp hvac-dashboard.html pi@192.168.133.50:/tmp/ && ssh pi@192.168.133.50 sudo hvac-update /tmp/hvac-dashboard.html

if [[ -z "$1" ]]; then
  echo "Usage: sudo hvac-update <path-to-hvac-dashboard.html>"
  exit 1
fi
cp "$1" /var/www/html/index.html
chown www-data:www-data /var/www/html/index.html
chmod 644 /var/www/html/index.html
echo "✓ Dashboard updated"
EOF
chmod +x /usr/local/bin/hvac-update
ok "Update helper installed at /usr/local/bin/hvac-update"

# ── watchdog — restart nginx if it crashes ──────────────────────
section "Nginx watchdog"
cat > /etc/systemd/system/nginx-watchdog.service <<EOF
[Unit]
Description=Nginx watchdog
After=nginx.service

[Service]
Type=simple
ExecStart=/bin/bash -c 'while true; do curl -sf http://localhost/health > /dev/null || systemctl restart nginx; sleep 60; done'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload -q
systemctl enable nginx-watchdog -q
systemctl start nginx-watchdog
ok "Nginx watchdog service installed and running"

# ── firewall ────────────────────────────────────────────────────
section "Firewall"
if command -v ufw &>/dev/null; then
  ufw allow ssh -q
  ufw allow http -q
  ufw --force enable -q
  ok "UFW firewall: SSH and HTTP allowed"
else
  warn "UFW not installed, skipping firewall setup"
fi

# ── summary ─────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Setup complete!${RESET}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Dashboard URL  : ${BOLD}http://$STATIC_IP${RESET}"
echo -e "  Also reachable : ${BOLD}http://$HOSTNAME.local${RESET}"
echo ""
echo -e "  To update the dashboard from your Mac:"
echo -e "  ${CYAN}scp hvac-dashboard.html pi@$STATIC_IP:/tmp/ && ssh pi@$STATIC_IP sudo hvac-update /tmp/hvac-dashboard.html${RESET}"
echo ""
echo -e "  ${YELLOW}A reboot is recommended to apply the static IP:${RESET}"
echo -e "  ${CYAN}sudo reboot${RESET}"
echo ""
