"use strict";
/**
 * Settings → General → LOCATION (Open-Meteo weather for the kiosk footer):
 * loads saved lat/lon from GET /settings, searches GET /weather/geocode,
 * picking a result fills the fields, Save PUTs weather_lat/lon/name.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "hvac-dashboard.html"), "utf8");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
// CI runners parse the 4k-line dashboard slowly — poll instead of fixed sleeps.
async function waitFor(fn, ms = 5000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      if (fn()) return;
    } catch {
      /* not ready */
    }
    await wait(20);
  }
  throw new Error("waitFor timed out");
}

function boot(saved) {
  const puts = [];
  const geocodeCalls = [];
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  const mockFetch = async (url, opts) => {
    const u = String(url);
    const method = opts?.method || "GET";
    if (u.endsWith("/api/")) return ok({ status: "ok", version: "v1", git_sha: "x", build: "" });
    if (u.includes("/auth/me")) return ok({ role: "admin", username: "admin", must_change_password: false });
    if (u.includes("/weather/geocode")) {
      geocodeCalls.push(u);
      return ok({ results: [{ name: "Tamarindo, Guanacaste, Costa Rica", lat: 10.29944, lon: -85.83722 }] });
    }
    if (u.includes("/weather"))
      return ok({
        configured: saved.weather_lat != null,
        location_name: saved.weather_location_name || "",
        weather: saved.weather_lat != null
          ? { temp_c: 30.5, code: 2, is_day: true, fetched_at: "2026-11-18T16:00:00" }
          : null,
      });
    if (u.includes("/settings") && method === "PUT") {
      puts.push(JSON.parse(opts.body));
      return ok({ ok: true });
    }
    if (u.includes("/settings")) return ok(saved);
    if (u.includes("/devices")) return ok({ devices: [] });
    if (u.includes("/schedules")) return ok({ schedules: [] });
    if (u.includes("/logs")) return ok({ logs: [] });
    if (u.includes("/maintenance")) return ok({ maintenance: [] });
    if (u.includes("/usage")) return ok({ devices: [] });
    if (u.includes("/vacation")) return ok({ vacation_mode: false });
    return { ok: false, status: 404, json: async () => ({}) };
  };
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    url: "http://localhost/hvac-dashboard.html",
    beforeParse(window) {
      window.fetch = mockFetch;
      window.confirm = () => true;
    },
  });
  return { window: dom.window, puts, geocodeCalls };
}

const click = (el, window) => el.dispatchEvent(new window.Event("click", { bubbles: true }));

async function openGeneral(window) {
  await waitFor(() => typeof window.openSettings === "function" && typeof window.toggleMenu === "function");
  window.document.querySelector("#gen-loc-status").textContent = "";
  click(window.document.querySelector("#menu-btn"), window);
  click(window.document.querySelector("#menu-settings"), window);
  await waitFor(() => window.document.querySelector("#gen-loc-status").textContent !== "");
}

test("dashboard location setting", async (t) => {
  const { window, puts, geocodeCalls } = boot({
    weather_lat: 10.6,
    weather_lon: -85.4,
    weather_location_name: "Liberia, Costa Rica",
  });
  await wait(100);
  await openGeneral(window);
  const $ = (s) => window.document.querySelector(s);

  await t.test("loads saved location and current reading", () => {
    assert.equal($("#gen-loc-lat").value, "10.6");
    assert.equal($("#gen-loc-lon").value, "-85.4");
    assert.equal($("#gen-loc-name").textContent, "Liberia, Costa Rica");
    assert.match($("#gen-loc-status").textContent, /now 30\.5°C/);
  });

  await t.test("search → pick result fills lat/lon/name", async () => {
    $("#gen-loc-search").value = "Tamarindo";
    click($("#gen-loc-find"), window);
    await waitFor(() => $("#gen-loc-results button"));
    assert.match(geocodeCalls[0], /q=Tamarindo/);
    const pick = $("#gen-loc-results button");
    assert.match(pick.textContent, /Tamarindo/);
    click(pick, window);
    assert.equal($("#gen-loc-lat").value, "10.2994");
    assert.equal($("#gen-loc-lon").value, "-85.8372");
    assert.equal($("#gen-loc-name").textContent, "Tamarindo, Guanacaste, Costa Rica");
  });

  await t.test("Save sends the location", async () => {
    window.saveGeneralSettings();
    await waitFor(() => puts.some((b) => "weather_lat" in b));
    const body = puts.find((b) => "weather_lat" in b);
    assert.equal(body.weather_lat, 10.2994);
    assert.equal(body.weather_lon, -85.8372);
    assert.equal(body.weather_location_name, "Tamarindo, Guanacaste, Costa Rica");
  });

  await t.test("blank lat/lon clears the location", async () => {
    await openGeneral(window);
    $("#gen-loc-lat").value = "";
    const before = puts.length;
    window.saveGeneralSettings();
    await waitFor(() => puts.length > before);
    const body = puts.filter((b) => "weather_lat" in b).at(-1);
    assert.equal(body.weather_lat, null);
    assert.equal(body.weather_lon, null);
    assert.equal(body.weather_location_name, "");
  });
  window.close();
});

test("dashboard location setting — unset shows fallback note", async () => {
  const { window } = boot({});
  await wait(100);
  await openGeneral(window);
  assert.match(window.document.querySelector("#gen-loc-status").textContent, /not set/);
  window.close();
});
