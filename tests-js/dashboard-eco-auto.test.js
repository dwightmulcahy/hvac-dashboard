/*
 * Settings → Devices: the "Eco auto" column (replaced KEEP) toggles
 * eco_auto and saves it with the device config.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "hvac-dashboard.html"), "utf8");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test("device list: Eco auto checkbox saves eco_auto", async (t) => {
  const saves = [];
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  const mockFetch = async (url, opts) => {
    const u = String(url);
    if (u.endsWith("/api/")) return ok({ status: "ok", version: "v0", git_sha: "x", build: "" });
    if (u.includes("/auth/me")) return ok({ role: "admin", username: "admin", must_change_password: false });
    if (u.includes("/vacation")) return ok({ vacation_mode: false });
    if (u.includes("/devices") && opts?.method && opts.method !== "GET") {
      saves.push(JSON.parse(opts.body));
      return ok({ ok: true });
    }
    if (u.includes("/devices")) {
      return ok({ devices: [{ host: "a", name: "A", btu: 12000, seer: 20, keep_mode: true, state: { mode: "COOL" } }] });
    }
    if (u.includes("/schedules")) return ok({ schedules: [] });
    if (u.includes("/logs")) return ok({ logs: [] });
    if (u.includes("/usage")) return ok({ devices: [] });
    if (u.includes("/maintenance")) return ok({ maintenance: [] });
    if (u.includes("/settings")) return ok({});
    return { ok: false, status: 404, json: async () => ({}) };
  };
  const { window } = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "http://localhost/hvac-dashboard.html",
    beforeParse(w) {
      w.fetch = mockFetch;
    },
  });
  t.after(() => window.close());
  await wait(150);
  window.renderIPList();
  const doc = window.document;

  await t.test("header says Eco auto, no Keep column", () => {
    const heads = [...doc.querySelectorAll("#ip-list .dev-hcell")].map((h) => h.textContent.trim());
    assert.ok(heads.includes("Eco auto"));
    assert.ok(!heads.includes("Keep"));
  });

  await t.test("checkbox toggles eco_auto and saves it (no keep_mode)", async () => {
    const box = doc.querySelector('#ip-list input[onchange="toggleEcoAuto(0)"]');
    assert.ok(box);
    assert.equal(box.checked, false);
    box.click();
    await wait(20);
    const body = saves.at(-1);
    assert.equal(body.eco_auto, true);
    assert.ok(!("keep_mode" in body));
  });
});
