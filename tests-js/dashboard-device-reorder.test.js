/*
 * Settings → Devices drag reorder. .irow is display:contents, so the drag
 * source must be the ⠿ handle (a display:contents element can't be dragged).
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "hvac-dashboard.html"), "utf8");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const dev = (host, name) => ({ host, name, btu: 12000, seer: 20, state: { mode: "OFF" } });

test("device list: handle drags, drop reorders and persists", async (t) => {
  const posts = [];
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  const mockFetch = async (url, opts) => {
    const u = String(url);
    if (u.endsWith("/api/")) return ok({ status: "ok", version: "v0", git_sha: "x", build: "" });
    if (u.includes("/auth/me")) return ok({ role: "admin", username: "admin", must_change_password: false });
    if (u.includes("/devices/reorder")) {
      posts.push(JSON.parse(opts.body));
      return ok({});
    }
    if (u.includes("/vacation")) return ok({ vacation_mode: false });
    if (u.includes("/devices")) return ok({ devices: [dev("a", "A"), dev("b", "B"), dev("c", "C")] });
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
  const rows = () => [...doc.querySelectorAll("#ip-list .irow")];

  await t.test("handle is the drag source, not the display:contents row", () => {
    const row = rows()[0];
    assert.equal(row.getAttribute("draggable"), null);
    assert.equal(row.querySelector(".drag-handle").getAttribute("draggable"), "true");
  });

  await t.test("drag first row onto third reorders and POSTs", async () => {
    const data = {};
    const dt = { effectAllowed: "", dropEffect: "", setData: (k, v) => (data[k] = v) };
    const fire = (el, type) => {
      const e = new window.Event(type, { bubbles: true, cancelable: true });
      e.dataTransfer = dt;
      el.dispatchEvent(e);
    };
    const handle = rows()[0].querySelector(".drag-handle");
    fire(handle, "dragstart");
    assert.ok(rows()[0].classList.contains("dragging"));
    assert.equal(data["text/plain"], "0");
    const target = rows()[2].querySelector("input");
    fire(target, "dragover");
    fire(target, "drop");
    fire(handle, "dragend");
    await wait(10);
    assert.deepEqual(posts.at(-1), { hosts: ["b", "c", "a"] });
    assert.deepEqual(rows().map((r) => r.dataset.origHost), ["b", "c", "a"]);
  });
});
