#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# ESPHome SLWF-01pro endpoint tester
# Usage: ./test_endpoints.sh <host>
# Example: ./test_endpoints.sh air-conditioner-c44741.lan
#          ./test_endpoints.sh 192.168.1.101
# ─────────────────────────────────────────────────────────────

HOST="${1:-air-conditioner-c44741.lan}"
BASE="http://$HOST"
PASS=0
FAIL=0
SKIP=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
RESET='\033[0m'

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${CYAN}  ESPHome endpoint tester — $HOST${RESET}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

# ── helpers ──────────────────────────────────────────────────

get_test() {
  local label="$1"
  local path="$2"
  local resp
  resp=$(curl -sf --max-time 5 "$BASE/$path" 2>&1)
  local code=$?
  if [ $code -eq 0 ] && [ -n "$resp" ]; then
    local val
    val=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('value','?') if 'value' in d else d.get('state','?'))" 2>/dev/null || echo "$resp" | head -c 80)
    echo -e "  ${GREEN}✓${RESET} GET  /$path"
    echo -e "    ${DIM}→ $val${RESET}"
    ((PASS++))
  else
    echo -e "  ${RED}✗${RESET} GET  /$path ${RED}(failed or empty)${RESET}"
    ((FAIL++))
  fi
}

post_test() {
  local label="$1"
  local path="$2"
  local resp http_code
  http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 -X POST "$BASE/$path" 2>&1)
  if [[ "$http_code" =~ ^2 ]]; then
    echo -e "  ${GREEN}✓${RESET} POST /$path ${DIM}(HTTP $http_code)${RESET}"
    ((PASS++))
  else
    echo -e "  ${RED}✗${RESET} POST /$path ${RED}(HTTP $http_code)${RESET}"
    ((FAIL++))
  fi
}

post_query_test() {
  local label="$1"
  local path="$2"
  local param="$3"
  local http_code
  http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 -X POST "$BASE/$path?$param" 2>&1)
  if [[ "$http_code" =~ ^2 ]]; then
    echo -e "  ${GREEN}✓${RESET} POST /$path?$param ${DIM}(HTTP $http_code)${RESET}"
    ((PASS++))
  else
    echo -e "  ${RED}✗${RESET} POST /$path?$param ${RED}(HTTP $http_code)${RESET}"
    ((FAIL++))
  fi
}

section() {
  echo ""
  echo -e "${CYAN}  ── $1 ──${RESET}"
}

# ── connectivity check ────────────────────────────────────────

echo -e "  Checking connectivity to $HOST..."
if ! curl -sf --max-time 5 "$BASE/" > /dev/null 2>&1; then
  echo -e "  ${RED}✗ Cannot reach $HOST — is it on the network?${RESET}"
  exit 1
fi
echo -e "  ${GREEN}✓ Device reachable${RESET}"

# ── save current state before testing ────────────────────────

section "Saving current state"
CURRENT=$(curl -sf --max-time 5 "$BASE/climate/air_conditioner" 2>/dev/null)
if [ -n "$CURRENT" ]; then
  ORIG_MODE=$(echo "$CURRENT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mode','COOL'))" 2>/dev/null)
  ORIG_TEMP=$(echo "$CURRENT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('target_temperature','24'))" 2>/dev/null)
  ORIG_FAN=$(echo "$CURRENT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('fan_mode','AUTO'))" 2>/dev/null)
  echo -e "  ${DIM}mode=$ORIG_MODE  temp=$ORIG_TEMP  fan=$ORIG_FAN${RESET}"
else
  ORIG_MODE="COOL"
  ORIG_TEMP="24"
  ORIG_FAN="AUTO"
  echo -e "  ${YELLOW}⚠ Could not read current state, will use defaults for restore${RESET}"
fi

# ── sensors (GET) ─────────────────────────────────────────────

section "Sensors"
get_test "Outdoor temperature"  "sensor/air_conditioner_outdoor_temperature"
get_test "Power usage"          "sensor/air_conditioner_power_usage"
get_test "Indoor humidity"      "sensor/air_conditioner_indoor_humidity"
get_test "Wi-Fi signal"         "sensor/air_conditioner_wi_fi_signal"
get_test "Uptime days"          "sensor/air_conditioner_uptime_days"

# ── climate state (GET) ───────────────────────────────────────

section "Climate state"
get_test "Climate state" "climate/air_conditioner"

# ── climate set — modes ───────────────────────────────────────

section "Climate SET — modes"
for mode in COOL HEAT DRY FAN_ONLY HEAT_COOL; do
  post_query_test "Mode $mode" "climate/air_conditioner/set" "mode=$mode"
  sleep 0.5
done

# ── climate set — temperature ─────────────────────────────────

section "Climate SET — temperature"
for temp in 20 22 24 26; do
  post_query_test "Temp ${temp}°C" "climate/air_conditioner/set" "target_temperature=$temp"
  sleep 0.5
done

# ── climate set — fan modes ───────────────────────────────────

section "Climate SET — fan modes"
for fan in AUTO SILENT TURBO; do
  post_query_test "Fan $fan" "climate/air_conditioner/set" "fan_mode=$fan"
  sleep 0.5
done

# ── climate set — presets ─────────────────────────────────────

section "Climate SET — presets"
for preset in ECO BOOST SLEEP FREEZE_PROTECTION NONE; do
  post_query_test "Preset $preset" "climate/air_conditioner/set" "preset=$preset"
  sleep 0.5
done

# ── buttons ───────────────────────────────────────────────────

section "Buttons"
post_test "Display toggle" "button/air_conditioner_display_toggle/press"
sleep 0.5
post_test "Swing step"     "button/air_conditioner_swing_step/press"

# ── switch — beeper ───────────────────────────────────────────

section "Switch — beeper"
get_test  "Beeper state"   "switch/air_conditioner_beeper"
post_test "Beeper turn on" "switch/air_conditioner_beeper/turn_on"
sleep 0.5
post_test "Beeper turn off" "switch/air_conditioner_beeper/turn_off"
sleep 0.5
post_test "Beeper toggle"  "switch/air_conditioner_beeper/toggle"

# ── restore original state ────────────────────────────────────

section "Restoring original state"
curl -sf --max-time 5 -X POST "$BASE/climate/air_conditioner/set?mode=$ORIG_MODE" > /dev/null
sleep 0.3
curl -sf --max-time 5 -X POST "$BASE/climate/air_conditioner/set?target_temperature=$ORIG_TEMP" > /dev/null
sleep 0.3
curl -sf --max-time 5 -X POST "$BASE/climate/air_conditioner/set?fan_mode=$ORIG_FAN" > /dev/null
echo -e "  ${GREEN}✓ Restored: mode=$ORIG_MODE  temp=$ORIG_TEMP  fan=$ORIG_FAN${RESET}"

# ── summary ───────────────────────────────────────────────────

TOTAL=$((PASS + FAIL))
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "  Results: ${GREEN}$PASS passed${RESET} · ${RED}$FAIL failed${RESET} · $TOTAL total"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
