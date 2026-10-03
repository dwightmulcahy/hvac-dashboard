"use strict";
/**
 * hvac-dashboard.html unit tile: KEEP indicator lives in the mode bar's fan
 * segment (not a header badge that crowded the name/temp controls), and the
 * footer shows only "fw <version>" with details in the tooltip.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "hvac-dashboard.html"),
  "utf8",
);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const DEVICE = {
  host: "ac1.local",
  name: "Main LR",
  btu: 24000,
  seer: 20,
  max_temp: 30,
  beeper: "OFF",
  lock_temp: false,
  locked_target_temp: null,
  watchdog_minutes: 5,
  keep_mode: true,
  _keep_paused: true,
  _keep_target: 25,
  _keep_resume_mode: "COOL",
  _max_temp_active: false,
  _retry_queue: [],
  _consecutive_failures: 0,
  state: {
    mode: "FAN_ONLY",
    current_temperature: 24.5,
    target_temperature: 25,
    outdoor_temp: 27,
    wifi_signal: -50,
    esphome_version:
      "2026.6.5 (config hash 0xbd538c66, built 2026-09-26 17:12:28 -0600)",
    firmware_name: "SMLIGHT.SLWF-01Pro-Pro",
    firmware_version: "1.1.0",
    firmware_latest: "1.1.0",
    firmware_outdated: false,
    firmware_built: "2026-09-26 17:12:28 -0600",
    firmware_built_date: "2026-09-26",
    firmware_config_hash: "0xbd538c66",
  },
};

const OFF_DEVICE = {
  ...DEVICE,
  host: "ac2.local",
  name: "A Very Long Bedroom Name",
  beeper: "ON",
  keep_mode: false,
  _keep_paused: false,
  lock_temp: true,
  locked_target_temp: 23,
  state: { ...DEVICE.state, mode: "OFF", target_temperature: 23, eco: true },
};

test("dashboard unit tile: KEEP in mode bar, compact firmware line", async (t) => {
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  const mockFetch = async (url) => {
    const u = String(url);
    if (u.endsWith("/api/"))
      return ok({ status: "ok", version: "v0", git_sha: "x", build: "" });
    if (u.includes("/auth/me"))
      return ok({
        role: "admin",
        username: "admin",
        must_change_password: false,
      });
    if (u.includes("/vacation")) return ok({ vacation_mode: false });
    if (u.includes("/devices")) return ok({ devices: [DEVICE, OFF_DEVICE] });
    if (u.includes("/schedules")) return ok({ schedules: [] });
    if (u.includes("/settings")) return ok({});
    if (u.includes("/logs")) return ok({ logs: [] });
    if (u.includes("/usage")) return ok({ devices: [] });
    if (u.includes("/maintenance")) return ok({ maintenance: [] });
    return { ok: false, status: 404, json: async () => ({}) };
  };
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "http://localhost/hvac-dashboard.html",
    beforeParse(window) {
      window.fetch = mockFetch;
    },
  });
  const { window } = dom;
  await wait(150);
  const tile = window.document.querySelector("#tile-0");

  await t.test("tile rendered", () => assert.ok(tile, "tile-0 missing"));

  await t.test("fan segment reads '⏸ keep' with a KEEP tooltip", () => {
    const fanBtn = [...tile.querySelectorAll("button")].find(
      (b) => b.getAttribute("onclick") === "setMode(0,'FAN_ONLY')",
    );
    assert.ok(fanBtn);
    assert.equal(fanBtn.textContent.trim(), "⏸ keep");
    assert.match(fanBtn.getAttribute("title"), /KEEP: target 25°C reached/);
  });

  await t.test("no separate keep badge in the tile header", () => {
    const keepSpans = [...tile.querySelectorAll("span")].filter(
      (s) => s.textContent.trim() === "⏸ keep",
    );
    assert.equal(keepSpans.length, 1, "only the mode-bar label");
    assert.ok(keepSpans[0].closest("button"));
  });

  await t.test("footer shows 'fw 1.1.0' only; build details in tooltip", () => {
    const fw = [...tile.querySelectorAll("span[title]")].find((s) =>
      s.textContent.startsWith("fw "),
    );
    assert.equal(fw.textContent, "fw 1.1.0");
    assert.doesNotMatch(tile.textContent, /config hash/);
    assert.match(fw.getAttribute("title"), /Built 2026-09-26 17:12:28 -0600/);
    assert.match(fw.getAttribute("title"), /Config hash 0xbd538c66/);
  });

  // ── decluttered header ──
  const tile1 = window.document.querySelector("#tile-1");
  const headerButtons = (t) =>
    [...t.firstElementChild.querySelectorAll(":scope > div > button")].map(
      (b) => b.textContent.trim(),
    );

  await t.test("header keeps only −, +, ⋯ and power as buttons", () => {
    const btns = headerButtons(tile);
    assert.deepEqual(btns.slice(0, 3), ["−", "+", "⋯"]);
    assert.equal(btns.length, 4);
  });

  await t.test("⋯ menu holds beeper and lock with ✓ state", () => {
    const menu = window.document.querySelector("#tile-menu-1");
    const items = [...menu.querySelectorAll(".menu-item")].map((m) =>
      m.textContent.trim(),
    );
    assert.deepEqual(items, ["✓🔔 Beeper", "✓🔒 Locked at 23°C"]);
    assert.equal(menu.style.display, "");
    window.toggleTileMenu(1);
    assert.equal(menu.style.display, "block");
    window.closeTileMenus();
    assert.equal(menu.style.display, "none");
  });

  await t.test(
    "ECO and lock show as status icons after the name only when on",
    () => {
      const stats = [...tile1.querySelectorAll(".tstat")].map(
        (e) => e.textContent,
      );
      assert.deepEqual(stats, ["🌿", "🔒"]);
      assert.equal(tile.querySelectorAll(".tstat").length, 0);
    },
  );

  await t.test(
    "long name truncates via .uname with full name in tooltip",
    () => {
      const name = tile1.querySelector(".uname");
      assert.equal(name.getAttribute("title"), "A Very Long Bedroom Name");
    },
  );

  await t.test("off unit shows greyed target instead of 'off'", () => {
    const tdisp = tile1.querySelector(".tdisp");
    assert.doesNotMatch(tdisp.textContent, /off/);
    assert.match(tdisp.innerHTML, /var\(--text3\)/);
  });

  await t.test(
    "mode bar ends with an ECO leaf toggle reflecting ECO state",
    () => {
      const eco0 = tile.querySelector(".eco-seg");
      const eco1 = tile1.querySelector(".eco-seg");
      assert.equal(eco0.textContent.trim(), "🌿");
      assert.equal(eco0.getAttribute("onclick"), "toggleEco(0)");
      assert.equal(eco0.dataset.eco, "off");
      assert.equal(eco1.dataset.eco, "on");
      // enabled even while the unit is off, so ECO intent can still be changed
      assert.ok(!eco1.disabled);
    },
  );

  await t.test(
    "ecoState: on / armed / off from intent + reported state",
    () => {
      const st = (eco_wanted, eco) =>
        window.ecoState({ eco_wanted, state: { eco } });
      assert.equal(st(true, true), "on");
      assert.equal(st(true, false), "armed"); // unit dropped it (off, fan, mode/temp change)
      assert.equal(st(false, true), "off");
      assert.equal(st(null, true), "on"); // never toggled: follow the unit
      assert.equal(st(undefined, false), "off");
    },
  );

  // ── KEEP highlight follows the actual mode ──

  await t.test(
    "turning a KEEP-paused unit off drops the KEEP highlight",
    async () => {
      window.fetch = async (url) => {
        const u = String(url);
        if (u.includes("/cmd")) return ok({ ok: true });
        return mockFetch(url);
      };
      await window.togglePower(0);
      await wait(20);
      const t0 = window.document.querySelector("#tile-0");
      assert.doesNotMatch(t0.getAttribute("style"), /--keep/);
      assert.ok(!t0.textContent.includes("⏸ keep"));
    },
  );

  await t.test(
    "stale _keep_paused alone (unit not in fan) is not highlighted",
    () => {
      assert.equal(
        window.keepActive({ _keep_paused: true, state: { mode: "OFF" } }),
        false,
      );
      assert.equal(
        window.keepActive({ _keep_paused: true, state: { mode: "COOL" } }),
        false,
      );
      assert.equal(
        window.keepActive({ _keep_paused: true, state: { mode: "FAN_ONLY" } }),
        true,
      );
    },
  );

  await t.test(
    "ECO on: target shown as ECO setpoint, +/− warn it turns ECO off",
    async () => {
      window.eval(
        "devices[1].state.mode='COOL';devices[1].state.eco=true;devices[1].eco_wanted=true;renderUnits();",
      );
      const t1 = window.document.querySelector("#tile-1");
      const tdisp = t1.querySelector(".tdisp");
      assert.match(tdisp.textContent, /^ECO/);
      assert.match(tdisp.innerHTML, /var\(--eco\)/);
      const plus = [...t1.querySelectorAll("button")].find(
        (b) => b.getAttribute("onclick") === "adjustTemp(1,1)",
      );
      assert.match(plus.getAttribute("title"), /turns ECO off/);
    },
  );

  await t.test("schedule modal warns when Temp and ECO On are both set", () => {
    window.openScheduleModal(null);
    const $id = (x) => window.document.getElementById(x);
    $id("sch-temp-en").checked = true;
    $id("sch-eco-en").checked = true;
    $id("sch-eco").value = "on";
    window.updateSchEcoHint();
    assert.equal($id("sch-eco-hint").style.display, "block");
    $id("sch-eco").value = "off";
    window.updateSchEcoHint();
    assert.equal($id("sch-eco-hint").style.display, "none");
    window.closeSchModal();
  });

  window.close();
});
