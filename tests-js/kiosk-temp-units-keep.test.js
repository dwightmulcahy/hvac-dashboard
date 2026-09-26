"use strict";
/**
 * kiosk.html: kiosk-local °C/°F/both toggle, power-on-to-last-mode when a
 * temp is adjusted on an OFF unit, and keep-temp paused highlighting.
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

test("kiosk: temp units, power-on to last mode, keep-temp", async (t) => {
  const devices = {
    devices: [
      dev("off-heat.local", "Off Heat", { _last_active_mode: "HEAT" }, { mode: "OFF", target_temperature: "22" }),
      dev("off-new.local", "Off New", { _last_active_mode: null }, { mode: "OFF", target_temperature: "22" }),
      dev(
        "keep.local",
        "Kept",
        { keep_temp: true, _keep_temp_paused: true, _keep_temp_target: 25.5, _last_active_mode: "COOL" },
        { mode: "OFF", target_temperature: "25.5" },
      ),
    ],
  };
  let cmdCalls = [];
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  const mockFetch = async (url, opts) => {
    const u = String(url);
    if (u.endsWith("/api/")) return ok({ status: "ok", version: "v0", git_sha: "x" });
    if (u.includes("/auth/login-pin"))
      return ok({ ok: true, token: "tok", username: "op", role: "operator", must_change_password: false });
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

  // ── Keep-temp highlight ──────────────────────────────────────

  await t.test("paused keep-temp tile shows KEEP and the resume target, not OFF", () => {
    const txt = tile("keep.local").textContent;
    assert.match(txt, /KEEP/);
    assert.doesNotMatch(txt, /\bOFF\b/);
    assert.match(txt, /→ 25\.5°/);
    assert.match(tile("keep.local").getAttribute("style"), /var\(--keep\)/);
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

  await t.test("keep-temp paused unit only changes its target and stays paused", async () => {
    await adjustOnce("keep.local");
    assert.deepEqual(cmdCalls[0].params, { target_temperature: 26 });
  });

  window.close();
});
