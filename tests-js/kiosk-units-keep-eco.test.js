"use strict";
/**
 * kiosk.html: kiosk-local °C/°F/both toggle, power-on-to-last-mode when a
 * temp is adjusted on an OFF unit, FAN mode, KEEP paused highlighting, and the unit's ECO preset toggle.
 * Separate jsdom instance from kiosk.test.js so device fixtures and
 * localStorage start clean.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "kiosk.html"), "utf8");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const dev = (host, name, extra, state) => ({
  host,
  name,
  btu: 24000,
  seer: 20,
  max_temp: null,
  beeper: "OFF",
  lock_temp: false,
  locked_target_temp: null,
  _stale: false,
  _max_temp_active: false,
  _firmware_version: "2026.7.0",
  ...extra,
  state: { current_temperature: "24.5", target_temperature: "25.5", outdoor_temp: 30, ...state },
});

test("kiosk: temp units, power-on to last mode, FAN mode, KEEP, ECO", async (t) => {
  const devices = {
    devices: [
      dev("off-heat.local", "Off Heat", { _last_active_mode: "HEAT" }, { mode: "OFF", target_temperature: "22" }),
      dev("off-new.local", "Off New", { _last_active_mode: null }, { mode: "OFF", target_temperature: "22" }),
      dev(
        "keep.local",
        "Kept",
        { keep_mode: true, _keep_paused: true, _keep_target: 25.5, _keep_resume_mode: "COOL", _last_active_mode: "FAN_ONLY" },
        { mode: "FAN_ONLY", target_temperature: "25.5" },
      ),
      dev("fan.local", "Fan", { _last_active_mode: "FAN_ONLY" }, { mode: "FAN_ONLY", eco: true, preset: "NONE" }),
      dev("legacy.local", "Legacy", {}, { mode: "COOL", preset: "ECO" }),
    ],
  };
  let cmdCalls = [];
  const ecoCalls = [];
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  const mockFetch = async (url, opts) => {
    const u = String(url);
    if (u.endsWith("/api/")) return ok({ status: "ok", version: "v0", git_sha: "x" });
    if (u.includes("/auth/login-pin"))
      return ok({ ok: true, token: "tok", username: "op", role: "operator", must_change_password: false });
    if (u.includes("/eco/")) {
      ecoCalls.push(u);
      const d = devices.devices.find((x) => u.includes(`/devices/${x.host}/`));
      if (d) d.state.eco = u.endsWith("/on"); // backend updates eco optimistically
      return ok({ ok: true });
    }
    if (u.includes("/cmd")) {
      cmdCalls.push({ url: u, params: JSON.parse(opts.body).params });
      return ok({ ok: true });
    }
    if (u.includes("/api/devices")) return ok(devices);
    if (u.includes("/api/settings")) return ok({ exchangeRate: 455, tiered: false, flatRate: 70 });
    if (u.includes("/api/schedules")) return ok({ schedules: [] });
    if (u.includes("/api/maintenance")) return ok({ maintenance: [] });
    if (u.includes("/api/usage/summary")) return ok({ devices: [] });
    return { ok: false, status: 404, json: async () => ({}) };
  };

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "http://localhost/kiosk.html",
    beforeParse(window) {
      window.fetch = mockFetch;
    },
  });
  const { window } = dom;
  const $ = (s) => window.document.querySelector(s);
  const click = (el) => el.dispatchEvent(new window.Event("click", { bubbles: true }));
  const tile = (host) => $(`.tile[data-host="${host}"]`);
  await wait(50);
  "4821".split("").forEach((d) =>
    click([...window.document.querySelectorAll("[data-key]")].find((b) => b.dataset.key === d)),
  );
  await wait(150);

  // ── Temp unit toggle ─────────────────────────────────────────

  await t.test("defaults to both units", () => {
    assert.equal($("#unit-toggle").textContent, "°C·°F");
    assert.match(tile("off-heat.local").textContent, /24\.5°\/76°/);
  });

  await t.test("tapping cycles both → C → F → both and re-renders tiles", () => {
    click($("#unit-toggle"));
    assert.equal($("#unit-toggle").textContent, "°C");
    assert.match(tile("off-heat.local").textContent, /24\.5°C/);
    assert.doesNotMatch(tile("off-heat.local").textContent, /°F/);
    click($("#unit-toggle"));
    assert.equal($("#unit-toggle").textContent, "°F");
    assert.match(tile("off-heat.local").textContent, /76°F/);
    assert.doesNotMatch(tile("off-heat.local").textContent, /°C/);
  });

  await t.test("choice persists in localStorage", () => {
    assert.equal(window.localStorage.getItem("kiosk_temp_unit"), "F");
    click($("#unit-toggle"));
    assert.equal(window.localStorage.getItem("kiosk_temp_unit"), "both");
  });

  // ── KEEP highlight / FAN mode ─────────────────────────────────

  await t.test("paused KEEP tile shows KEEP and its target, not FAN", () => {
    const txt = tile("keep.local").textContent;
    assert.match(txt, /KEEP/);
    assert.doesNotMatch(txt, /FAN/);
    assert.match(txt, /→ 25\.5°/);
    assert.match(tile("keep.local").getAttribute("style"), /var\(--keep\)/);
  });

  await t.test("a unit in FAN_ONLY shows FAN on its tile", () => {
    assert.match(tile("fan.local").textContent, /\bFAN\b/);
    assert.doesNotMatch(tile("fan.local").textContent, /FAN_ONLY/);
  });

  await t.test("detail mode bar is OFF/COOL/HEAT/FAN/AUTO and FAN sends FAN_ONLY", async () => {
    click(tile("off-heat.local"));
    await wait(30);
    const btns = [...window.document.querySelectorAll("[data-mode]")];
    assert.deepEqual(
      btns.map((b) => b.textContent),
      ["OFF", "COOL", "HEAT", "FAN", "AUTO"],
    );
    cmdCalls = [];
    click(btns[3]);
    await wait(30);
    assert.deepEqual(cmdCalls[0].params, { mode: "FAN_ONLY" });
    click($("#sub-header"));
    await wait(30);
    devices.devices[0].state.mode = "OFF";
  });

  // ── Power on to last mode ────────────────────────────────────

  const adjustOnce = async (host) => {
    cmdCalls = [];
    click(tile(host));
    await wait(30);
    click($('[data-act="up"]'));
    await wait(700);
    click($("#sub-header"));
    await wait(30);
  };

  await t.test("OFF unit powers on in its last active mode with the new temp", async () => {
    await adjustOnce("off-heat.local");
    assert.equal(cmdCalls.length, 1);
    assert.deepEqual(cmdCalls[0].params, { mode: "HEAT", target_temperature: 22.5 });
  });

  await t.test("OFF unit with no known last mode powers on in COOL", async () => {
    await adjustOnce("off-new.local");
    assert.deepEqual(cmdCalls[0].params, { mode: "COOL", target_temperature: 22.5 });
  });

  await t.test("KEEP-paused unit (in fan) only changes its target", async () => {
    await adjustOnce("keep.local");
    assert.deepEqual(cmdCalls[0].params, { target_temperature: 26 });
  });

  // ── ECO preset ──────────────────────────────────────────────

  await t.test("tile leaf follows eco_status (eco) over climate preset", () => {
    assert.match(tile("fan.local").innerHTML, /<svg/);
    const leafCount = (h) => (tile(h).innerHTML.match(/M5 20c0-9/g) || []).length;
    assert.equal(leafCount("fan.local"), 1);
    assert.equal(leafCount("off-heat.local"), 0);
    assert.equal(leafCount("legacy.local"), 1, "falls back to climate preset without eco_status sensor");
  });

  await t.test("detail ECO button reflects preset and calls eco/off then eco/on", async () => {
    click(tile("fan.local"));
    await wait(30);
    const btn = () => $('[data-act="eco"]');
    assert.match(btn().getAttribute("aria-label"), /ECO is on/);
    click(btn());
    await wait(30);
    assert.match(ecoCalls.at(-1), /\/devices\/fan\.local\/eco\/off$/);
    assert.match(btn().getAttribute("aria-label"), /ECO is off/);
    click($("#sub-header"));
    await wait(30);
    click(tile("off-heat.local"));
    await wait(30);
    click(btn());
    await wait(30);
    assert.match(ecoCalls.at(-1), /\/devices\/off-heat\.local\/eco\/on$/);
    click($("#sub-header"));
    await wait(30);
  });

  window.close();
});
