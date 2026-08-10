"use strict";
/**
 * Unit tests for pure utility functions extracted from
 * hvac-dashboard.html. Run with: node --test tests-js/
 *
 * Uses Node's built-in test runner (node:test) — no npm install, no
 * package.json, no build step, consistent with how this frontend has
 * been syntax-checked throughout the project (plain `node --check`).
 *
 * A fresh sandbox is loaded per test file run (not per-test) since
 * these are pure functions with no shared mutable state between calls
 * — except _tempUnit and rateSettings, which individual tests set
 * explicitly before asserting, so ordering never matters.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { loadDashboardFunctions, readDashboardScript, listAllSentinelRegions, checkAllSentinelsConsumed, REGIONS } = require("./extract.js");

const fns = loadDashboardFunctions();

// ── Extractor self-check ─────────────────────────────────────
// These test the extraction machinery itself, not dashboard logic —
// they exist to catch "someone added a new TESTABLE region and forgot
// to wire it into REGIONS" before it becomes a silent coverage gap.

test("every TESTABLE sentinel region in the dashboard is covered by REGIONS", () => {
  const src = readDashboardScript();
  const present = listAllSentinelRegions(src);
  for (const name of present) {
    assert.ok(REGIONS.includes(name), `TESTABLE:${name} exists in hvac-dashboard.html but isn't in extract.js's REGIONS list`);
  }
});

test("checkAllSentinelsConsumed throws for an unknown region", () => {
  const fakeSource = "// ── TESTABLE:totally-new-region:start ──\ncode\n// ── TESTABLE:totally-new-region:end ──";
  assert.throws(() => checkAllSentinelsConsumed(fakeSource), /totally-new-region/);
});

test("checkAllSentinelsConsumed does not throw when all regions are known", () => {
  const src = readDashboardScript();
  assert.doesNotThrow(() => checkAllSentinelsConsumed(src));
});

// ── seerToEer / maxWatts ─────────────────────────────────────

test("seerToEer converts SEER to EER using the 0.875 factor", () => {
  assert.equal(fns.seerToEer(20), 17.5);
  assert.equal(fns.seerToEer(18), 15.75);
});

test("seerToEer defaults to SEER 20 when given a falsy value", () => {
  assert.equal(fns.seerToEer(null), fns.seerToEer(20));
  assert.equal(fns.seerToEer(0), fns.seerToEer(20));
  assert.equal(fns.seerToEer(undefined), fns.seerToEer(20));
});

test("maxWatts computes BTU / EER, rounded", () => {
  assert.equal(fns.maxWatts(24000, 20), Math.round(24000 / 17.5));
  assert.equal(fns.maxWatts(12000, 18), Math.round(12000 / 15.75));
});

// ── safeFloat ─────────────────────────────────────────────────

test("safeFloat parses valid numeric strings", () => {
  assert.equal(fns.safeFloat("25.5"), 25.5);
  assert.equal(fns.safeFloat("0"), 0);
  assert.equal(fns.safeFloat(30), 30);
});

test("safeFloat returns null for unparseable input", () => {
  assert.equal(fns.safeFloat("abc"), null);
  assert.equal(fns.safeFloat(undefined), null);
  assert.equal(fns.safeFloat(null), null);
});

// ── estWatts ──────────────────────────────────────────────────

test("estWatts returns 0 when mode is OFF", () => {
  assert.equal(fns.estWatts({ mode: "OFF" }, {}, 24000, 20), 0);
});

test("estWatts returns 0 when mode is FAN_ONLY", () => {
  assert.equal(fns.estWatts({ mode: "FAN_ONLY" }, {}, 24000, 20), 0);
});

test("estWatts returns null when btu is missing", () => {
  const w = fns.estWatts({ mode: "COOL", current_temperature: "28", target_temperature: "22" }, {}, null, 20);
  assert.equal(w, null);
});

test("estWatts returns null when indoor/target temps are missing", () => {
  assert.equal(fns.estWatts({ mode: "COOL" }, {}, 24000, 20), null);
});

test("estWatts returns a positive number for a normal cooling scenario", () => {
  const w = fns.estWatts(
    { mode: "COOL", current_temperature: "28", target_temperature: "22" },
    { outdoor_temp: { value: 33 } },
    24000, 20
  );
  assert.ok(w > 0);
  assert.ok(w <= fns.maxWatts(24000, 20)); // never exceeds the unit's rated max
});

test("estWatts load factor increases with larger indoor/target delta", () => {
  const small = fns.estWatts(
    { mode: "COOL", current_temperature: "24", target_temperature: "23" }, {}, 24000, 20
  );
  const large = fns.estWatts(
    { mode: "COOL", current_temperature: "30", target_temperature: "20" }, {}, 24000, 20
  );
  assert.ok(large > small);
});

// ── toF / fmtTemp ─────────────────────────────────────────────

test("toF converts Celsius to Fahrenheit, rounded", () => {
  assert.equal(fns.toF(0), 32);
  assert.equal(fns.toF(100), 212);
  assert.equal(fns.toF(25), 77);
});

test("toF returns null for unparseable input", () => {
  assert.equal(fns.toF("abc"), null);
});

test("fmtTemp shows only Celsius when _tempUnit is 'C'", () => {
  fns._tempUnit = "C";
  assert.equal(fns.fmtTemp(25.5), "25.5°C");
});

test("fmtTemp shows only Fahrenheit when _tempUnit is 'F'", () => {
  fns._tempUnit = "F";
  assert.equal(fns.fmtTemp(25.5), "78°F"); // toF rounds: 25.5*9/5+32 = 77.9 -> 78
});

test("fmtTemp shows both units by default", () => {
  fns._tempUnit = "both";
  const result = fns.fmtTemp(25.5);
  assert.ok(result.includes("25.5°C"));
  assert.ok(result.includes("78°F"));
});

test("fmtTemp returns an em-dash for unparseable input", () => {
  fns._tempUnit = "both";
  assert.equal(fns.fmtTemp("abc"), "—");
});

// ── fmtWifi ───────────────────────────────────────────────────

test("fmtWifi returns null for missing or zero signal", () => {
  assert.equal(fns.fmtWifi(null), null);
  assert.equal(fns.fmtWifi(undefined), null);
  assert.equal(fns.fmtWifi(""), null);
  assert.equal(fns.fmtWifi(0), null);
});

test("fmtWifi buckets signal strength into the right tier", () => {
  assert.equal(fns.fmtWifi(-45).bar, "▂▄▆█"); // >= -50
  assert.equal(fns.fmtWifi(-55).bar, "▂▄▆_"); // >= -60
  assert.equal(fns.fmtWifi(-65).bar, "▂▄__"); // >= -70
  assert.equal(fns.fmtWifi(-80).bar, "▂___"); // < -70
});

test("fmtWifi marks weak signal (< -70) as error color, strong as ok", () => {
  assert.equal(fns.fmtWifi(-45).color, "var(--ok)");
  assert.equal(fns.fmtWifi(-80).color, "var(--err)");
});

// ── fmtOnTime ─────────────────────────────────────────────────

test("fmtOnTime formats minutes into d/h/m, omitting zero units", () => {
  assert.equal(fns.fmtOnTime(0), "0m");
  assert.equal(fns.fmtOnTime(5), "5m");
  assert.equal(fns.fmtOnTime(125), "2h 5m");
  assert.equal(fns.fmtOnTime(1500), "1d 1h"); // 1500min = 25h = 1d1h, 0 leftover minutes
});

test("fmtOnTime treats falsy/negative input as 0m", () => {
  assert.equal(fns.fmtOnTime(null), "0m");
  assert.equal(fns.fmtOnTime(-5), "0m");
});

// ── fmtDays ───────────────────────────────────────────────────

test("fmtDays recognizes 'every day'", () => {
  assert.equal(fns.fmtDays([0, 1, 2, 3, 4, 5, 6]), "every day");
});

test("fmtDays recognizes 'weekdays'", () => {
  assert.equal(fns.fmtDays([1, 2, 3, 4, 5]), "weekdays");
});

test("fmtDays recognizes 'weekends'", () => {
  assert.equal(fns.fmtDays([0, 6]), "weekends");
});

test("fmtDays falls back to a comma-joined day list for anything else", () => {
  assert.equal(fns.fmtDays([1, 3, 5]), "Mo, We, Fr");
});

test("fmtDays sorts unordered input before matching presets", () => {
  assert.equal(fns.fmtDays([5, 4, 3, 2, 1]), "weekdays");
});

// ── fmtActions ────────────────────────────────────────────────

test("fmtActions joins power/mode/temp with middle dots", () => {
  assert.equal(
    fns.fmtActions({ power: "on", mode: "COOL", temp: 24 }),
    "power on · mode cool · temp 24°C"
  );
});

test("fmtActions omits fields that are not set", () => {
  assert.equal(fns.fmtActions({ mode: "COOL" }), "mode cool");
  assert.equal(fns.fmtActions({}), "—");
});

test("fmtActions lowercases the mode", () => {
  assert.equal(fns.fmtActions({ mode: "HEAT_COOL" }), "mode heat_cool");
});

// ── nextFireMs / fmtNext ─────────────────────────────────────

test("nextFireMs returns Infinity when the schedule has no matching days", () => {
  const ms = fns.nextFireMs({ time: "07:00", days: [] });
  assert.equal(ms, Infinity);
});

test("nextFireMs returns a timestamp in the future for an every-day schedule", () => {
  const ms = fns.nextFireMs({ time: "07:00", days: [0, 1, 2, 3, 4, 5, 6] });
  assert.ok(ms > Date.now());
  // must fire within the next 7 days no matter what day "now" is
  assert.ok(ms <= Date.now() + 7 * 24 * 60 * 60 * 1000);
});

test("fmtNext returns empty string when nextFireMs is Infinity", () => {
  assert.equal(fns.fmtNext({ time: "07:00", days: [] }), "");
});

test("fmtNext returns a non-empty 'in Xm/Xh/Xd' style string for an every-day schedule", () => {
  const result = fns.fmtNext({ time: "07:00", days: [0, 1, 2, 3, 4, 5, 6] });
  assert.ok(result.startsWith("in "));
});

// ── fmtHours ──────────────────────────────────────────────────

test("fmtHours formats 0/null as '0h'", () => {
  assert.equal(fns.fmtHours(0), "0h");
  assert.equal(fns.fmtHours(null), "0h");
});

test("fmtHours formats sub-day hours with minutes", () => {
  assert.equal(fns.fmtHours(2.5), "2h 30m");
});

test("fmtHours formats multi-day durations without minutes", () => {
  assert.equal(fns.fmtHours(25), "1d 1h");
});

// ── LOG_LEVELS / matchesFilter ────────────────────────────────

test("matchesFilter 'all' matches everything", () => {
  assert.equal(fns.matchesFilter({ type: "err" }, "all"), true);
  assert.equal(fns.matchesFilter({ type: "info" }, "all"), true);
});

test("matchesFilter exact level matches only that level", () => {
  assert.equal(fns.matchesFilter({ type: "warn" }, "warn"), true);
  assert.equal(fns.matchesFilter({ type: "err" }, "warn"), false);
});

test("matchesFilter 'X+' matches X and anything more severe", () => {
  assert.equal(fns.matchesFilter({ type: "err" }, "warn+"), true);
  assert.equal(fns.matchesFilter({ type: "warn" }, "warn+"), true);
  assert.equal(fns.matchesFilter({ type: "ok" }, "warn+"), false);
  assert.equal(fns.matchesFilter({ type: "info" }, "warn+"), false);
});

// ── Rates / cost estimation ──────────────────────────────────
// These use the REAL RATE_DEFAULTS currently configured in the
// dashboard rather than injected test values, so a change to the
// actual Coopeguanacaste rates in hvac-dashboard.html is reflected
// here automatically instead of silently drifting from reality.

test("effectiveRateUsd matches hand-computed tier math for the current RATE_DEFAULTS", () => {
  const { monthlyKwh, exchangeRate, tiers } = fns.RATE_DEFAULTS;
  const tier = tiers.find(t => monthlyKwh <= t.upTo) || tiers[tiers.length - 1];
  const expected = tier.colonesPerKwh / exchangeRate;
  assert.equal(fns.effectiveRateUsd(), expected);
});

test("estCostDay returns 0 for 0 watts", () => {
  assert.equal(fns.estCostDay(0), 0);
  assert.equal(fns.estCostDay(null), 0);
});

test("estCostDay scales linearly with watts", () => {
  const cost1000 = fns.estCostDay(1000);
  const cost2000 = fns.estCostDay(2000);
  assert.ok(Math.abs(cost2000 - cost1000 * 2) < 1e-9);
});

test("estCostMonth is exactly 30x estCostDay", () => {
  const day = fns.estCostDay(1000);
  const month = fns.estCostMonth(1000);
  assert.ok(Math.abs(month - day * 30) < 1e-9);
});
