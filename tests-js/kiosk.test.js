"use strict";
/**
 * Functional test suite for kiosk.html — the wall-mounted touchscreen
 * UI. Unlike dashboard-functions.test.js (which extracts pure,
 * DOM-independent functions via sentinel comments), kiosk.html's PIN
 * lock screen, grid rendering, and screen navigation are inherently
 * DOM-driven, so this suite runs the *actual* file in a real DOM
 * (jsdom) with a mocked backend, then drives it exactly the way a
 * finger on the touchscreen would: tap digits, tap tiles, tap
 * buttons, and assert on what actually rendered.
 *
 * This is the one test file in the project that isn't zero-dependency
 * — it requires jsdom (see package.json). Every other JS test here
 * has no npm dependencies at all; this one genuinely needs a DOM
 * implementation to exercise PIN entry, screen transitions, and
 * rendered output, which sentinel-based pure-function extraction
 * can't reach.
 *
 * The whole flow shares one jsdom window/mock backend across many
 * nested t.test() subtests rather than re-doing the (relatively
 * expensive) unlock-and-navigate setup 60+ times — each subtest still
 * gets its own pass/fail line in CI output, it just isn't isolated
 * from the ones before it in the same outer test(). Where a specific
 * regression is being guarded against, the subtest name and a code
 * comment explain what actually broke and why.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "kiosk.html"), "utf8");

const MOCK_DEVICES = {
  devices: [
    {
      host: "ac1.local",
      name: "Main LR",
      btu: 24000,
      seer: 20,
      max_temp: 30,
      beeper: "ON",
      lock_temp: false,
      locked_target_temp: null,
      _stale: false,
      _max_temp_active: false,
      _on_time_minutes: 42,
      _firmware_version: "2026.7.0",
      state: {
        mode: "COOL",
        current_temperature: "24",
        target_temperature: "22",
        outdoor_temp: 31,
        wifi_signal: -48,
        actual_power_watts: 900,
      },
    },
    {
      host: "ac2.local",
      name: "Kitchen",
      btu: 18000,
      seer: 20,
      max_temp: null,
      beeper: "OFF",
      lock_temp: false,
      locked_target_temp: null,
      _stale: true,
      _max_temp_active: false,
      _on_time_minutes: 0,
      _firmware_version: null,
      state: {},
    },
    {
      host: "ac3.local",
      name: "Master BR",
      btu: 24000,
      seer: 20,
      max_temp: null,
      beeper: "OFF",
      lock_temp: true,
      locked_target_temp: 21,
      _stale: false,
      _max_temp_active: false,
      _on_time_minutes: 10,
      _firmware_version: "2026.7.0",
      state: {
        mode: "COOL",
        current_temperature: "23",
        target_temperature: "21",
        outdoor_temp: 31,
        wifi_signal: -55,
        actual_power_watts: 500,
      },
    },
  ],
};
const MOCK_SETTINGS = {
  exchangeRate: 455,
  monthlyKwh: 400,
  dailyHrs: 8,
  tiered: true,
  tiers: [{ upTo: 999999, colonesPerKwh: 70 }],
};
const fmtHM = (d) =>
  `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;

// Schedules are computed fresh each time /api/schedules is actually
// fetched, not once at file load — an earlier version of this test
// captured "now"/"today" once up front and used it several seconds
// later, and it broke for real: enough wall-clock time passed
// between two separate tool calls while building this test suite
// that the actual calendar day rolled over, so the frozen
// days:[...] stopped matching "today" by the time the real render
// logic computed its own fresh new Date(). Not a kiosk.html bug —
// a flakiness bug in the test's own fixture construction.
const STATIC_SCHEDULES = [
  {
    device_name: "Kitchen",
    time: "21:00",
    days: [0, 1, 2, 3, 4, 5, 6],
    enabled: true,
    mode: "COOL",
    temp: 22,
    power: "on",
  },
  {
    device_name: "Main LR",
    time: "07:00",
    days: [1, 2, 3, 4, 5],
    enabled: true,
    mode: null,
    temp: null,
    power: "off",
  },
  { device_name: "Disabled One", time: "05:00", days: [0], enabled: false },
];
function buildMockSchedules() {
  const now = new Date();
  const activeStart = fmtHM(new Date(now.getTime() - 3600 * 1000));
  const activeEnd = fmtHM(new Date(now.getTime() + 3600 * 1000));
  return {
    schedules: [
      ...STATIC_SCHEDULES,
      // days is every-day here deliberately — this schedule exists to
      // test the starts-vs-ends countdown label logic, not day-of-week
      // matching (already covered separately below), and every-day
      // sidesteps the exact same real-time day-boundary race described
      // above rather than reintroducing it.
      {
        device_name: "Currently Active",
        time: activeStart,
        end_time: activeEnd,
        days: [0, 1, 2, 3, 4, 5, 6],
        enabled: true,
        mode: "COOL",
        temp: 21,
        power: "on",
      },
    ],
  };
}
const MOCK_ROOT = { status: "ok", version: "v1.13.0", git_sha: "abc1234" };

// A realistic mix: one device-scoped item overdue on Main LR (the
// device most other tests already tap into as the first tile), one
// whole-house item due soon (no device_host — shouldn't attach to
// any tile badge), and one clean item to prove sort order actually
// pushes overdue/due-soon ahead of it rather than just listing as-is.
const MOCK_MAINTENANCE = [
  {
    id: "m-clean",
    name: "Coil check",
    device_host: "ac3.local",
    device_name: "Master BR",
    trigger_type: "runtime_hours",
    interval_hours: 500,
    status: { overdue: false, due_soon: false, hours_remaining: 200 },
  },
  {
    id: "m-overdue",
    name: "Clean filters",
    device_host: "ac1.local",
    device_name: "Main LR",
    trigger_type: "days",
    interval_days: 90,
    status: { overdue: true, due_soon: false, days_remaining: -5 },
  },
  {
    id: "m-soon",
    name: "Annual service",
    device_host: null,
    device_name: null,
    trigger_type: "days",
    interval_days: 365,
    status: { overdue: false, due_soon: true, days_remaining: 3 },
  },
];

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test("kiosk.html end-to-end functional behavior", async (t) => {
  const beeperCalls = [];
  let cmdCalls = [];
  let vacationCalls = [];
  const usageSummaryCalls = [];
  const maintenanceCompleteCalls = [];
  let simulateNetworkDown = false;

  const mockFetch = async (url, opts) => {
    if (simulateNetworkDown) throw new Error("simulated network failure");
    const u = String(url);
    if (u.endsWith("/api/")) return { ok: true, status: 200, json: async () => MOCK_ROOT };
    if (u.includes("/api/auth/login-pin")) {
      const body = JSON.parse(opts.body);
      if (body.pin === "4821")
        return {
          ok: true,
          status: 200,
          json: async () => ({
            ok: true,
            token: "tok",
            username: "dwight",
            role: "operator",
            must_change_password: false,
          }),
        };
      if (body.pin === "1357")
        return {
          ok: true,
          status: 200,
          json: async () => ({
            ok: true,
            token: "tok-viewer",
            username: "guest",
            role: "viewer",
            must_change_password: false,
          }),
        };
      return { ok: false, status: 401, json: async () => ({ detail: "Incorrect PIN" }) };
    }
    if (u.includes("/api/auth/logout"))
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    if (u.includes("/vacation/")) {
      vacationCalls.push(u);
      MOCK_SETTINGS.vacation_mode = u.includes("/vacation/on");
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true, vacation_mode: MOCK_SETTINGS.vacation_mode }),
      };
    }
    if (u.includes("/beeper/")) {
      beeperCalls.push(u);
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (u.includes("/cmd")) {
      const body = JSON.parse(opts.body);
      cmdCalls.push({ url: u, body });
      const dev = MOCK_DEVICES.devices.find((d) => u.includes(d.host));
      if (dev) Object.assign(dev.state, body.params);
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (u.includes("/maintenance/") && u.includes("/complete")) {
      maintenanceCompleteCalls.push(u);
      const id = u.split("/maintenance/")[1].split("/complete")[0];
      const item = MOCK_MAINTENANCE.find((m) => m.id === id);
      if (item) item.status = { ...item.status, overdue: false, due_soon: false };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (u.includes("/api/maintenance"))
      return { ok: true, status: 200, json: async () => ({ maintenance: MOCK_MAINTENANCE }) };
    if (u.includes("/api/devices"))
      return { ok: true, status: 200, json: async () => MOCK_DEVICES };
    if (u.includes("/api/settings"))
      return { ok: true, status: 200, json: async () => MOCK_SETTINGS };
    if (u.includes("/api/schedules"))
      return { ok: true, status: 200, json: async () => buildMockSchedules() };
    if (u.includes("/api/usage/summary")) {
      usageSummaryCalls.push(u);
      // Deliberately different from Main LR's cumulative
      // _on_time_minutes fixture (42) — if the kiosk were still
      // showing the all-time cumulative field instead of fetching
      // today's actual runtime, this test would see 42m, not 15m.
      return {
        ok: true,
        status: 200,
        json: async () => ({
          month: "today",
          devices: [
            {
              host: "ac1.local",
              name: "Main LR",
              runtime_hours: 0.25,
              est_kwh: 0.1,
              peak_watts: 900,
              days_active: 1,
            },
          ],
        }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    url: "http://localhost/kiosk.html",
    beforeParse(window) {
      window.fetch = mockFetch;
    },
  });
  const { window } = dom;
  await wait(50);

  const $ = (sel) => window.document.querySelector(sel);
  const tap = (d) =>
    [...window.document.querySelectorAll("[data-key]")]
      .find((b) => b.dataset.key === d)
      .dispatchEvent(new window.Event("click", { bubbles: true }));

  // ── PIN entry / unlock ─────────────────────────────────────

  "4821".split("").forEach(tap);
  await wait(100);

  await t.test("unlocks successfully with the correct PIN", () => {
    assert.ok(
      window.getComputedStyle($("#main-screen")).display !== "none" ||
        $("#main-screen").classList.contains("active"),
    );
  });

  await t.test("header badge shows username alongside role, not just role", () => {
    const text = $("#version-badge").textContent;
    assert.match(text, /dwight/);
    assert.match(text, /Operator/);
  });

  await t.test("no external CDN dependency remains — icons are self-contained SVG", () => {
    // The kiosk previously depended on a live fetch to
    // cdnjs.cloudflare.com for every icon; on a device that may sit on
    // a restricted or offline home network, that CDN call can silently
    // fail and every icon in the UI just doesn't render.
    assert.ok(!html.includes("<link") || !html.includes("cdnjs.cloudflare.com"));
    assert.match($("#relock-btn").innerHTML, /<svg/);
  });

  // ── Grid overview ────────────────────────────────────────────

  await t.test("grid tile shows compact C/F dual temperature format", () => {
    const tileText = $(".tile").textContent;
    assert.match(tileText, /\d+°\/\d+°/);
  });

  await t.test("grid tile temp font size is readably large (>22px)", () => {
    const tileTemp = [...$(".tile").querySelectorAll("span")].find((s) =>
      /\d+°\/\d+°/.test(s.textContent),
    );
    assert.ok(parseInt(window.getComputedStyle(tileTemp).fontSize) > 22);
  });

  await t.test("grid tile on-time shows today's runtime, not the all-time cumulative total", () => {
    // Regression test: the ON-TIME figure used to show
    // device._on_time_minutes, which accumulates forever since the
    // device was first added (weeks of runtime) rather than resetting
    // daily — genuinely not what "on-time" reads as at a glance on a
    // wall panel. It now comes from GET /usage/summary scoped to
    // today's date specifically.
    const mainLrTile = [...window.document.querySelectorAll(".tile")].find((tile) =>
      tile.textContent.includes("Main LR"),
    );
    assert.ok(
      mainLrTile.textContent.includes("15m"),
      `expected today's 15m runtime, not the cumulative 42m (tile text: "${mainLrTile.textContent.replace(/\s+/g, " ")}")`,
    );
    assert.ok(!mainLrTile.textContent.includes("42m"));
  });

  // ── Device detail view ───────────────────────────────────────

  $(".tile").dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("detail hero temp shows both °C and °F explicitly", () => {
    const detailHtml = $("#grid-detail").innerHTML;
    assert.match(detailHtml, /°C/);
    assert.match(detailHtml, /°F/);
  });

  await t.test("all four modes (OFF/COOL/HEAT/AUTO) render as buttons", () => {
    const detailHtml = $("#grid-detail").innerHTML;
    for (const m of ["OFF", "COOL", "HEAT", "AUTO"]) {
      assert.ok(detailHtml.includes(`>${m}<`), `missing ${m} button`);
    }
  });

  await t.test("beeper is a control in the main row, not a separate labeled row", () => {
    const detailHtml = $("#grid-detail").innerHTML;
    assert.ok(detailHtml.includes('data-act="beeper"'));
    assert.match(detailHtml, /Beeper is on/); // aria-label reflects state; icon-only, no visible text
  });

  await t.test("wifi stat shows the actual dBm meter and value, not just a word", () => {
    const detailHtml = $("#grid-detail").innerHTML;
    assert.match(detailHtml, /dBm/);
    const wifiBars = [...$("#grid-detail").querySelectorAll("span")].filter(
      (s) => s.style.width === "5px",
    );
    assert.equal(wifiBars.length, 4);
  });

  await t.test(
    "detail ON TODAY stat shows today's runtime, correctly labeled, calls usage/summary with today's date",
    () => {
      const detailHtml = $("#grid-detail").innerHTML;
      assert.ok(
        detailHtml.includes("ON TODAY"),
        "label should say ON TODAY, not the ambiguous old ON-TIME",
      );
      assert.ok(detailHtml.includes("15m") && !detailHtml.includes("42m"));
      const today = `${new Date().getFullYear()}-${String(new Date().getMonth() + 1).padStart(2, "0")}-${String(new Date().getDate()).padStart(2, "0")}`;
      assert.ok(
        usageSummaryCalls.some((u) => u.includes(`month=${today}`)),
        `usage/summary should be called with today's local date (calls: ${JSON.stringify(usageSummaryCalls)})`,
      );
    },
  );

  await t.test("hero temp font size is prominent (>34px)", () => {
    const heroTemp = [...$("#grid-detail").querySelectorAll("span")].find((s) =>
      s.textContent.includes("°C"),
    );
    assert.ok(parseInt(window.getComputedStyle(heroTemp).fontSize) > 34);
  });

  await t.test("stat cards use a row layout (icon left, 2 lines of text right)", () => {
    const firstCard = $(".stat-card");
    assert.equal(window.getComputedStyle(firstCard).flexDirection, "row");
    assert.ok(firstCard.querySelector("svg") !== null);
  });

  await t.test("stat cards align in a real CSS grid, not centered flex-wrap", () => {
    // flex-wrap + justify-content:center centers whatever's left on a
    // second row independently of the row above — the actual bug that
    // prompted this: a trailing 2-card row floating unaligned under
    // nothing in particular from the row above it.
    const statContainer = $(".stat-card").parentElement;
    assert.equal(window.getComputedStyle(statContainer).display, "grid");
  });

  // ── Temp adjustment: step, debounce, coalescing ──────────────

  MOCK_DEVICES.devices[0].state.step = "0.5";
  MOCK_DEVICES.devices[0].state.min_temp = "17";
  MOCK_DEVICES.devices[0].state.max_temp = "30";
  const upBtn = $('[data-act="up"]');

  upBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(30);

  await t.test("tapping + updates the display instantly, before any network round trip", () => {
    assert.ok($("#grid-detail").textContent.includes("22.5°C"));
    assert.equal(
      cmdCalls.length,
      0,
      "command should not have been sent yet — still inside the debounce window",
    );
  });

  await wait(650);

  await t.test(
    "debounced send fires after the window closes, using the real 0.5°C step from device state",
    () => {
      // Regression coverage: this used to be hardcoded to a 1°C step
      // regardless of what the dongle actually reported.
      assert.equal(cmdCalls.length, 1);
      assert.equal(cmdCalls[0].body.params.target_temperature, 22.5);
    },
  );

  cmdCalls = [];
  upBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(30);
  upBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(30);
  upBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(650);

  await t.test("three rapid taps coalesce into one command with the final value, not three", () => {
    assert.equal(cmdCalls.length, 1);
    assert.equal(cmdCalls[0].body.params.target_temperature, 24);
  });

  await t.test("detail back-header renders a real SVG chevron and reads 'All units'", () => {
    assert.match($("#sub-header-back-icon").innerHTML, /<svg/);
    assert.ok($("#sub-header").textContent.includes("All units"));
  });

  const beeperBtn = $('[data-act="beeper"]');
  beeperBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("beeper toggle calls the correct on/off endpoint for the right device", () => {
    assert.ok(beeperCalls.some((u) => u.includes("ac1.local/beeper/off")));
  });

  $("#sub-header").dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  // ── Schedules view ───────────────────────────────────────────

  const nextCell = [...window.document.querySelectorAll("[data-action]")].find(
    (c) => c.dataset.action === "schedules",
  );
  await t.test("NEXT footer stat is tappable", () => {
    assert.ok(nextCell !== undefined);
  });
  nextCell.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("tapping NEXT opens the schedules view listing enabled schedules only", () => {
    assert.equal(window.getComputedStyle($("#schedules-view")).display, "block");
    const schedText = $("#schedules-view").textContent;
    assert.ok(schedText.includes("Kitchen"));
    assert.ok(!schedText.includes("Disabled One"));
  });

  await t.test("days are formatted as a readable summary, not a raw array", () => {
    const schedText = $("#schedules-view").textContent;
    assert.ok(schedText.includes("Every day"));
    assert.ok(schedText.includes("Weekdays"));
  });

  await t.test("a schedule with an end_time shows the full start-to-end time range", () => {
    const schedText = $("#schedules-view").textContent;
    assert.match(schedText, /\d{2}:\d{2} → \d{2}:\d{2}/);
  });

  const activeRow = [...$("#schedules-view").querySelectorAll("div")]
    .find((d) => d.textContent === "Currently Active")
    ?.closest('div[style*="justify-content:space-between"]');

  await t.test(
    "a currently-active schedule (started, not yet ended) shows 'ends in', never 'starts in'",
    () => {
      // Showing "starts in 8h" for something already mid-run would be
      // straightforwardly wrong, not just incomplete.
      assert.ok(activeRow !== undefined);
      assert.ok(activeRow.textContent.includes("ends in"));
      assert.ok(!activeRow.textContent.includes("starts in"));
    },
  );

  await t.test(
    "overnight end_time countdown is correct (~1h), not thrown a week off by day-matching",
    () => {
      // Regression test for a real bug: an overnight end_time crossing
      // midnight was matched against the wrong day-of-week, sending the
      // computed countdown a full week into the future (145h instead of
      // ~1h) for a schedule that was actually about to end shortly after
      // midnight.
      assert.match(activeRow.textContent, /ends in (\d+m|[01]h)/);
    },
  );

  await t.test(
    "schedule row text is readably large (device name >=16px) and sub-header retitles to 'Schedules'",
    () => {
      const scheduleNameEl = [...$("#schedules-view").querySelectorAll("div")].find(
        (d) => d.textContent.trim() === "Kitchen",
      );
      assert.ok(scheduleNameEl && parseInt(window.getComputedStyle(scheduleNameEl).fontSize) >= 16);
      assert.ok($("#sub-header").textContent.includes("Schedules"));
    },
  );

  // ── Devices view ─────────────────────────────────────────────

  $("#sub-header").dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);
  const onlineCell = [...window.document.querySelectorAll("[data-action]")].find(
    (c) => c.dataset.action === "devices",
  );
  await t.test("ONLINE footer stat is tappable", () => {
    assert.ok(onlineCell !== undefined);
  });
  onlineCell.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test(
    "tapping ONLINE opens the devices view listing all devices, online and offline",
    () => {
      assert.equal(window.getComputedStyle($("#devices-view")).display, "block");
      const devText = $("#devices-view").textContent;
      assert.ok(devText.includes("Main LR") && devText.includes("Kitchen"));
      assert.ok(devText.includes("OFFLINE"));
    },
  );

  // ── Screensaver: burn-in mitigation ──────────────────────────

  $("#sub-header").dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);
  window.showScreensaver();
  await wait(30);
  const pos1 = { left: $("#saver-content").style.left, top: $("#saver-content").style.top };

  await t.test(
    "showing the screensaver immediately positions the clock, not left at a default spot",
    () => {
      assert.ok(pos1.left !== "" && pos1.top !== "");
    },
  );

  window.moveSaverContent();
  const pos2 = { left: $("#saver-content").style.left, top: $("#saver-content").style.top };

  await t.test(
    "the screensaver clock moves to a new position on each move call, for burn-in mitigation",
    () => {
      // Note: jsdom has no real layout engine, so getBoundingClientRect()
      // always returns zero-sized rects — this can verify the position
      // genuinely changes, but not that it stays within real on-screen
      // bounds against actual rendered text dimensions. That part relies
      // on code review of the bounds math, not this test.
      assert.ok(pos1.left !== pos2.left || pos1.top !== pos2.top);
    },
  );

  // Verifying the move interval is actually cleared on relock (not
  // left running invisibly in the background) isn't practically
  // testable here without either a real 20s wait or test-only hooks
  // in production code — confirmed by code review instead
  // (relock() calls clearInterval on it).
  window.relock();

  // ── Vacation mode toggle ─────────────────────────────────────

  "4821".split("").forEach(tap);
  await wait(100);
  const vacBtn = $("#vacation-btn");

  await t.test("vacation toggle is visible for a non-viewer (operator) role and starts off", () => {
    assert.notEqual(window.getComputedStyle(vacBtn).display, "none");
    assert.match(vacBtn.getAttribute("aria-label"), /off/);
  });

  vacBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("tapping the vacation toggle calls vacation/on and reflects the new state", () => {
    assert.ok(vacationCalls.some((u) => u.includes("/vacation/on")));
    assert.match(vacBtn.getAttribute("aria-label"), /on/);
  });

  vacationCalls = [];
  vacBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("tapping it again correctly calls vacation/off, not vacation/on again", () => {
    assert.ok(vacationCalls.some((u) => u.includes("/vacation/off")));
  });

  // ── Maintenance reminders ────────────────────────────────────

  window.showSub("grid");
  await wait(20);
  const maintBtn = $("#maintenance-btn");

  await t.test("maintenance icon is visible and red when an overdue item exists", () => {
    assert.notEqual(window.getComputedStyle(maintBtn).display, "none");
    assert.match(maintBtn.getAttribute("aria-label"), /overdue/);
  });

  maintBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("tapping the maintenance icon opens the maintenance view", () => {
    assert.equal(window.getComputedStyle($("#maintenance-view")).display, "block");
    assert.ok($("#sub-header").textContent.includes("Maintenance"));
  });

  await t.test("items are sorted overdue-first, ahead of a due-soon or clean item", () => {
    const rowNames = [...$("#maintenance-view").querySelectorAll("div")]
      .map((d) => d.textContent.trim())
      .filter((t) => t === "Clean filters" || t === "Coil check" || t === "Annual service");
    const overdueIdx = rowNames.indexOf("Clean filters");
    const cleanIdx = rowNames.indexOf("Coil check");
    assert.ok(
      overdueIdx !== -1 && cleanIdx !== -1 && overdueIdx < cleanIdx,
      `expected overdue item before the clean item (order: ${rowNames.join(", ")})`,
    );
  });

  await t.test("a whole-house item (no device_host) still appears in the list", () => {
    assert.ok($("#maintenance-view").textContent.includes("Annual service"));
    assert.ok($("#maintenance-view").textContent.includes("Whole house"));
  });

  // Check the tile badge WHILE the item is still genuinely overdue —
  // completing it below clears the flag, which would make this
  // assertion meaningless if checked afterward.
  window.showSub("grid");
  await wait(20);

  await t.test(
    "the device-scoped overdue item (Main LR) shows a wrench badge on its own grid tile, while it's actually overdue",
    () => {
      const wrenchPathFragment = "14.7 6.3";
      const mainLrTile = [...window.document.querySelectorAll(".tile")].find((tile) =>
        tile.textContent.includes("Main LR"),
      );
      const masterBrTile = [...window.document.querySelectorAll(".tile")].find((tile) =>
        tile.textContent.includes("Master BR"),
      );
      const kitchenTile = [...window.document.querySelectorAll(".tile")].find((tile) =>
        tile.textContent.includes("Kitchen"),
      );
      assert.ok(
        mainLrTile.innerHTML.includes(wrenchPathFragment),
        "Main LR tile (the one with the overdue item) shows the wrench badge",
      );
      assert.ok(
        !masterBrTile.innerHTML.includes(wrenchPathFragment),
        "Master BR tile (item not overdue) does not show a wrench badge",
      );
      assert.ok(
        !kitchenTile.innerHTML.includes(wrenchPathFragment),
        "Kitchen tile (no maintenance items at all) does not show a wrench badge",
      );
    },
  );

  window.showSub("maintenance");
  window.renderMaintenanceView();
  await wait(20);
  const doneBtn = $('[data-complete="m-overdue"]');
  await t.test("a Done button exists for the operator role and targets the right item", () => {
    assert.ok(doneBtn !== null);
  });

  doneBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("tapping Done calls the correct /maintenance/{id}/complete endpoint", () => {
    assert.ok(
      maintenanceCompleteCalls.some((u) => u.includes("/maintenance/m-overdue/complete")),
      `calls: ${JSON.stringify(maintenanceCompleteCalls)}`,
    );
  });

  await t.test(
    "after completing, the maintenance icon reflects the change (no longer red-overdue, since the mock cleared it)",
    () => {
      // m-soon (whole-house, due_soon) is still due_soon, so the icon
      // should now show amber rather than disappearing entirely.
      assert.notEqual(window.getComputedStyle($("#maintenance-btn")).display, "none");
      assert.doesNotMatch(maintBtn.getAttribute("aria-label"), /overdue/);
    },
  );

  // Role gating: viewer sees the reminder list (awareness matters for
  // everyone) but never a Done button (completing is operator+, same
  // as the backend enforces on POST /maintenance/{id}/complete).
  window.relock();
  "1357".split("").forEach(tap);
  await wait(100);

  await t.test(
    "maintenance icon is still visible for a viewer role (status is not admin-only)",
    () => {
      assert.notEqual(window.getComputedStyle($("#maintenance-btn")).display, "none");
    },
  );

  $("#maintenance-btn").dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(50);

  await t.test("viewer sees the maintenance list but no Done buttons on any item", () => {
    assert.ok($("#maintenance-view").textContent.includes("Clean filters"));
    assert.equal($("#maintenance-view").querySelectorAll("[data-complete]").length, 0);
  });

  // ── Staleness indicator ──────────────────────────────────────

  simulateNetworkDown = true;
  await window.refreshAll();
  await wait(20);

  await t.test("a single failed poll does not yet dim the display or show the banner", () => {
    // Avoids over-reacting to one transient blip.
    assert.equal(window.getComputedStyle($("#grid-view")).opacity, "1");
    assert.equal(window.getComputedStyle($("#loading-banner")).display, "none");
  });

  await window.refreshAll();
  await wait(20);

  await t.test("a second consecutive failed poll dims the display and shows the banner", () => {
    assert.notEqual(window.getComputedStyle($("#grid-view")).opacity, "1");
    assert.equal(window.getComputedStyle($("#loading-banner")).display, "block");
  });

  simulateNetworkDown = false;
  await window.refreshAll();
  await wait(20);

  await t.test("display and banner both recover as soon as a poll succeeds again", () => {
    assert.equal(window.getComputedStyle($("#grid-view")).opacity, "1");
    assert.equal(window.getComputedStyle($("#loading-banner")).display, "none");
  });

  // ── Reliability: fetch timeout + request coalescing ──────────

  let deviceFetchCount = 0;
  let sawAbortSignal = false;
  const originalFetch = window.fetch;
  window.fetch = async (url, opts) => {
    if (opts && opts.signal) sawAbortSignal = true;
    if (String(url).includes("/api/devices")) deviceFetchCount++;
    return originalFetch(url, opts);
  };
  await Promise.all([window.refreshAll(), window.refreshAll(), window.refreshAll()]);
  window.fetch = originalFetch;

  await t.test(
    "concurrent refreshAll() calls coalesce into one round of fetches, not three",
    () => {
      // A kiosk meant to run unattended for months shouldn't fire
      // overlapping requests if a periodic poll and a post-command
      // refresh happen to land at the same moment.
      assert.equal(deviceFetchCount, 1);
    },
  );

  await t.test(
    "requests go through the timeout wrapper (AbortController signal attached), not raw fetch",
    () => {
      assert.ok(sawAbortSignal);
    },
  );

  // ── Shared idle-badge across detail/schedules/devices ────────

  window.showSub("grid");
  const idleBadge = $("#idle-badge");

  await t.test("idle badge is hidden on the grid (no auto-return timer applies there)", () => {
    assert.equal(window.getComputedStyle(idleBadge).display, "none");
  });

  $(".tile").dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(20);

  await t.test("idle badge becomes visible on the detail view", () => {
    assert.notEqual(window.getComputedStyle(idleBadge).display, "none");
  });

  window.showSub("grid");
  const nextCellAgain = [...window.document.querySelectorAll("[data-action]")].find(
    (c) => c.dataset.action === "schedules",
  );
  nextCellAgain.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(20);

  await t.test(
    "the same shared idle badge also appears on the schedules view, not just the detail view",
    () => {
      // Regression coverage: an earlier draft embedded a separate
      // #idle-badge element inside each view's own template, which
      // meant three elements briefly shared the same id simultaneously
      // in the DOM — getElementById only ever returns the first match,
      // so the countdown could silently show in the wrong view.
      assert.notEqual(window.getComputedStyle(idleBadge).display, "none");
    },
  );

  // ── Role gating: viewer sees no admin-level controls ─────────

  window.relock();
  "1357".split("").forEach(tap);
  await wait(100);

  await t.test("vacation toggle is hidden entirely for a viewer role, not just disabled", () => {
    assert.equal(window.getComputedStyle($("#vacation-btn")).display, "none");
  });

  // ── Locked device: indicator placement + disabled +/- ────────

  window.relock();
  "4821".split("").forEach(tap);
  await wait(100);

  const lockedTile = [...window.document.querySelectorAll(".tile")].find((tile) =>
    tile.textContent.includes("Master BR"),
  );
  lockedTile.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(30);

  await t.test(
    "the separate LOCKED stat card is gone — moved next to the beeper icon, not duplicated",
    () => {
      const detailHtmlLocked = $("#grid-detail").innerHTML;
      assert.ok(!detailHtmlLocked.includes("LOCKED"));
      const lockIconRow = $('[data-act="beeper"]')?.parentElement;
      assert.ok(lockIconRow && lockIconRow.innerHTML.includes("Locked at"));
    },
  );

  const downBtnLocked = $('[data-act="down"]');
  const upBtnLocked = $('[data-act="up"]');

  await t.test("+/- buttons are visually grayed out when the device is locked", () => {
    assert.equal(downBtnLocked.style.color, "var(--gray)");
    assert.equal(upBtnLocked.style.color, "var(--gray)");
  });

  const cmdCallsBeforeLocked = cmdCalls.length;
  upBtnLocked.dispatchEvent(new window.Event("click", { bubbles: true }));
  downBtnLocked.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(700);

  await t.test("tapping +/- on a locked device sends no command at all", () => {
    // The worker unconditionally restores a locked device's target on
    // its next poll regardless of what sent the change, including the
    // kiosk itself — letting someone tap +, see it "work", then watch
    // it silently revert moments later would look like a bug.
    assert.equal(cmdCalls.length, cmdCallsBeforeLocked);
  });

  const modeBtn = $('[data-mode="HEAT"]');
  modeBtn.dispatchEvent(new window.Event("click", { bubbles: true }));
  await wait(30);

  await t.test(
    "mode buttons still work normally on a locked device — only temperature is locked",
    () => {
      assert.ok(cmdCalls.some((c) => c.body.params.mode === "HEAT"));
    },
  );

  // ── Quiet-hours screensaver dimming (isQuietHours) ────────────
  // Pure logic, tested directly via window.isQuietHours(now) rather
  // than by waiting out the real SCREENSAVER_IDLE_SECONDS or mocking
  // Date globally — the function takes an optional `now` for exactly
  // this reason.

  await t.test("disabled by default — never quiet regardless of time", () => {
    const cfg = {
      kiosk_quiet_hours_enabled: false,
      kiosk_quiet_start: "22:00",
      kiosk_quiet_end: "07:00",
    };
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 23, 0)), false);
  });

  await t.test("overnight window: inside the window on both sides of midnight", () => {
    const cfg = {
      kiosk_quiet_hours_enabled: true,
      kiosk_quiet_start: "22:00",
      kiosk_quiet_end: "07:00",
    };
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 23, 30)), true); // 11:30pm
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 5, 0)), true); // 5:00am
  });

  await t.test("overnight window: outside the window during the day", () => {
    const cfg = {
      kiosk_quiet_hours_enabled: true,
      kiosk_quiet_start: "22:00",
      kiosk_quiet_end: "07:00",
    };
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 14, 0)), false); // 2:00pm
  });

  await t.test("overnight window: boundaries are start-inclusive, end-exclusive", () => {
    const cfg = {
      kiosk_quiet_hours_enabled: true,
      kiosk_quiet_start: "22:00",
      kiosk_quiet_end: "07:00",
    };
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 22, 0)), true); // exactly start
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 7, 0)), false); // exactly end
  });

  await t.test("same-day window (not spanning midnight) works too", () => {
    const cfg = {
      kiosk_quiet_hours_enabled: true,
      kiosk_quiet_start: "13:00",
      kiosk_quiet_end: "18:00",
    };
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 15, 0)), true);
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 20, 0)), false);
  });

  await t.test("identical start and end is treated as no quiet window at all", () => {
    const cfg = {
      kiosk_quiet_hours_enabled: true,
      kiosk_quiet_start: "10:00",
      kiosk_quiet_end: "10:00",
    };
    assert.equal(window.isQuietHours(cfg, new Date(2026, 0, 1, 10, 0)), false);
  });

  await t.test("malformed time strings fail closed (never quiet) rather than throwing", () => {
    const cfg = {
      kiosk_quiet_hours_enabled: true,
      kiosk_quiet_start: "not-a-time",
      kiosk_quiet_end: "07:00",
    };
    assert.doesNotThrow(() => window.isQuietHours(cfg, new Date()));
    assert.equal(window.isQuietHours(cfg, new Date()), false);
  });

  // ── Last-updated indicator (fmtAgo) ───────────────────────────

  await t.test("fmtAgo formats seconds and minutes, and handles no-data gracefully", () => {
    const now = Date.now();
    assert.equal(window.fmtAgo(null), "");
    assert.equal(window.fmtAgo(new Date(now)), "0s ago");
    assert.equal(window.fmtAgo(new Date(now - 5000)), "5s ago");
    assert.equal(window.fmtAgo(new Date(now - 125000)), "2m ago");
  });

  // kiosk.html runs several setInterval timers forever by design
  // (the on-screen clock, background polling) — correct for a real
  // kiosk left open permanently, but without an explicit teardown
  // here they'd keep this jsdom window's timers alive indefinitely,
  // and node --test never naturally exits after this file.
  window.close();
});
