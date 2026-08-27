"use strict";
/**
 * Functional test suite for hvac-dashboard.html's maintenance
 * create/edit/delete/complete flow in the Settings modal.
 *
 * Follows the same pattern as tests-js/kiosk.test.js: rather than
 * extracting pure functions (dashboard-functions.test.js's approach,
 * which can't reach anything DOM-driven), this loads the real file
 * into an actual DOM (jsdom) with a mocked backend and drives it the
 * way a person would — click the menu, click Settings, click the
 * Maintenance tab, fill the form, click Save — then asserts on both
 * what actually rendered and what request the mock backend received.
 *
 * Scope: this file is about the maintenance CRUD *form* — role-gated
 * rendering (admin sees +Add/edit/delete, operator sees only
 * Done, viewer sees neither), validation, and that Save/PUT/DELETE
 * send the right payload to the right endpoint. It deliberately does
 * NOT re-test the login modal itself (checkAuth() below is mocked to
 * always return an already-authenticated session) — that's a
 * different flow with its own coverage elsewhere, and re-driving a
 * password-entry form here would just add setup cost for no benefit
 * to what this file exists to test.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "hvac-dashboard.html"), "utf8");

const MOCK_DEVICE = {
  host: "ac1.local",
  name: "Main LR",
  btu: 24000,
  seer: 20,
  max_temp: null,
  beeper: "OFF",
  lock_temp: false,
  locked_target_temp: null,
  watchdog_minutes: 5,
  _max_temp_active: false,
  _retry_queue: [],
  _consecutive_failures: 0,
  _firmware_version: "2026.7.0",
  state: {
    mode: "COOL",
    current_temperature: 23,
    target_temperature: 22,
    outdoor_temp: 31,
    wifi_signal: -48,
    actual_power_watts: 900,
  },
};
const MOCK_ROOT = { status: "ok", version: "v1.14.0", git_sha: "abc1234", build: "" };

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// Builds one shared jsdom window authenticated as `role`, with its own
// isolated maintenance list and call-tracking arrays. Each top-level
// test() in this file gets a fresh instance rather than sharing one
// across the whole file, since the admin-flow tests below mutate
// maintenance state (add/edit/delete) in ways that would leak into a
// separately-scoped role-gating check if they shared one window.
function boot(role) {
  const maintenanceItems = [
    {
      id: "m-existing",
      name: "Annual service",
      device_host: null,
      device_name: null,
      trigger_type: "days",
      interval_days: 365,
      notes: "whole-house",
      status: { overdue: false, due_soon: false, days_remaining: 40 },
    },
  ];
  const calls = { post: [], put: [], delete: [], complete: [] };

  const mockFetch = async (url, opts) => {
    const u = String(url);
    const method = opts?.method || "GET";
    if (u.endsWith("/api/")) return { ok: true, status: 200, json: async () => MOCK_ROOT };
    if (u.includes("/auth/me"))
      return {
        ok: true,
        status: 200,
        json: async () => ({ role, username: role, must_change_password: false }),
      };
    if (u.includes("/vacation"))
      return { ok: true, status: 200, json: async () => ({ vacation_mode: false }) };
    if (u.includes("/devices"))
      return { ok: true, status: 200, json: async () => ({ devices: [MOCK_DEVICE] }) };
    if (u.includes("/schedules"))
      return { ok: true, status: 200, json: async () => ({ schedules: [] }) };
    if (u.includes("/settings")) return { ok: true, status: 200, json: async () => ({}) };
    if (u.includes("/logs")) return { ok: true, status: 200, json: async () => ({ logs: [] }) };
    if (u.includes("/usage/summary") || u.includes("/usage/rolling30"))
      return { ok: true, status: 200, json: async () => ({ devices: [] }) };

    if (u.includes("/maintenance") && u.includes("/complete") && method === "POST") {
      const id = u.split("/maintenance/")[1].split("/complete")[0];
      calls.complete.push(id);
      const item = maintenanceItems.find((m) => m.id === id);
      if (item) item.status = { ...item.status, overdue: false, due_soon: false };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (u.includes("/maintenance/") && method === "PUT") {
      const id = u.split("/maintenance/")[1];
      const body = JSON.parse(opts.body);
      calls.put.push({ id, body });
      const item = maintenanceItems.find((m) => m.id === id);
      if (item) Object.assign(item, body);
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (u.includes("/maintenance/") && method === "DELETE") {
      const id = u.split("/maintenance/")[1];
      calls.delete.push(id);
      const idx = maintenanceItems.findIndex((m) => m.id === id);
      if (idx >= 0) maintenanceItems.splice(idx, 1);
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (u.includes("/maintenance") && method === "POST") {
      const body = JSON.parse(opts.body);
      calls.post.push(body);
      const id = "m-new-" + (maintenanceItems.length + 1);
      maintenanceItems.push({
        id,
        ...body,
        status: { overdue: false, due_soon: false, days_remaining: body.interval_days },
      });
      return { ok: true, status: 200, json: async () => ({ ok: true, id }) };
    }
    if (u.includes("/maintenance"))
      return { ok: true, status: 200, json: async () => ({ maintenance: maintenanceItems }) };

    return { ok: false, status: 404, json: async () => ({}) };
  };

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    url: "http://localhost/hvac-dashboard.html",
    beforeParse(window) {
      window.fetch = mockFetch;
      // confirm() blocks in jsdom by default (throws "not implemented")
      // — the delete flow below needs it to resolve true, same as a
      // real user clicking "OK".
      window.confirm = () => true;
      window.navigator.clipboard = { writeText: async () => {} };
    },
  });
  return { dom, window: dom.window, calls, maintenanceItems };
}

const $ = (window, sel) => window.document.querySelector(sel);
const click = (el, window) => el.dispatchEvent(new window.Event("click", { bubbles: true }));

async function openMaintenanceTab(window) {
  click($(window, "#menu-btn"), window);
  click($(window, "#menu-settings"), window);
  await wait(20);
  click($(window, "#stab-maintenance"), window);
  await wait(50);
}

test("hvac-dashboard.html maintenance tab — admin flow", async (t) => {
  const { window, calls, maintenanceItems } = boot("admin");
  await wait(100); // init() bootstrap

  await openMaintenanceTab(window);

  await t.test("existing reminder renders in the table", () => {
    const bodyText = $(window, "#settings-maintenance-body").textContent;
    assert.match(bodyText, /Annual service/);
    assert.match(bodyText, /Whole house/);
  });

  await t.test("admin sees the + Add reminder button", () => {
    const bodyHtml = $(window, "#settings-maintenance-body").innerHTML;
    assert.match(bodyHtml, /\+ Add reminder/);
  });

  await t.test("admin sees edit and delete controls on the existing row", () => {
    const buttons = [...window.document.querySelectorAll("#settings-maintenance-body button")];
    // onclick handlers here are assigned as JS properties
    // (btn.onclick=function(){...}), not inline HTML attributes, so
    // they never appear in innerHTML — title is the only reliable
    // thing to assert on from outside.
    assert.ok(buttons.some((b) => b.title === "Edit"));
    assert.ok(buttons.some((b) => b.title === "Delete"));
  });

  // ── Open the add form ────────────────────────────────────────
  const addBtn = [...window.document.querySelectorAll("#settings-maintenance-body button")].find(
    (b) => b.textContent.includes("Add reminder"),
  );
  click(addBtn, window);

  await t.test("form appears with cleared, defaulted fields for a new item", () => {
    assert.equal($(window, "#maintenance-form").style.display, "block");
    assert.equal($(window, "#maint-name").value, "");
    assert.equal($(window, "#maint-trigger").value, "days");
    assert.equal($(window, "#maint-interval").value, "90");
    assert.equal($(window, "#maint-interval-label").textContent, "INTERVAL (DAYS)");
  });

  await t.test("device scope dropdown is populated from the loaded devices", () => {
    const opts = [...$(window, "#maint-device").options].map((o) => o.textContent);
    assert.ok(opts.includes("Whole house"));
    assert.ok(opts.includes("Main LR"));
  });

  const saveBtn = () =>
    [...$(window, "#maintenance-form").querySelectorAll("button")].find(
      (b) => b.textContent === "Save",
    );

  // ── Validation: empty name blocks submission ────────────────
  click(saveBtn(), window);
  await wait(20);

  await t.test("empty name shows an inline error and sends no request", () => {
    const err = $(window, "#maintenance-form-error");
    assert.equal(err.style.display, "block");
    assert.match(err.textContent, /required/i);
    assert.equal(calls.post.length, 0);
  });

  // ── Fill and switch to runtime_hours to check the label swap ──
  $(window, "#maint-name").value = "Coil clean";
  $(window, "#maint-device").value = "ac1.local";
  $(window, "#maint-trigger").value = "runtime_hours";
  $(window, "#maint-trigger").dispatchEvent(new window.Event("change", { bubbles: true }));
  $(window, "#maint-interval").value = "500";
  $(window, "#maint-notes").value = "check coils and drain line";

  await t.test("interval label swaps to hours when trigger changes to runtime_hours", () => {
    assert.equal($(window, "#maint-interval-label").textContent, "INTERVAL (HOURS)");
  });

  click(saveBtn(), window);
  await wait(50);

  await t.test("save sends a POST with the correct device-scoped runtime_hours payload", () => {
    assert.equal(calls.post.length, 1);
    const body = calls.post[0];
    assert.equal(body.name, "Coil clean");
    assert.equal(body.device_host, "ac1.local");
    assert.equal(body.device_name, "Main LR");
    assert.equal(body.trigger_type, "runtime_hours");
    assert.equal(body.interval_hours, 500);
    assert.equal(body.notes, "check coils and drain line");
  });

  await t.test("form closes and the new reminder appears in the re-rendered list", () => {
    assert.equal($(window, "#maintenance-form").style.display, "none");
    assert.match($(window, "#settings-maintenance-body").textContent, /Coil clean/);
  });

  // ── Edit the existing whole-house item ──────────────────────
  const editBtn = [...window.document.querySelectorAll("#settings-maintenance-body button")].find(
    (b) => b.title === "Edit",
  );
  click(editBtn, window);

  await t.test("edit pre-fills the form with the existing item's values", () => {
    assert.equal($(window, "#maint-name").value, "Annual service");
    assert.equal($(window, "#maint-device").value, "");
    assert.equal($(window, "#maint-trigger").value, "days");
    assert.equal($(window, "#maint-interval").value, "365");
  });

  $(window, "#maint-interval").value = "180";
  click(saveBtn(), window);
  await wait(50);

  await t.test("edit save sends a PUT to the existing item's id, not a POST", () => {
    assert.equal(calls.put.length, 1);
    assert.equal(calls.put[0].id, "m-existing");
    assert.equal(calls.put[0].body.interval_days, 180);
    assert.equal(calls.post.length, 1); // unchanged from before — edit didn't also POST
  });

  // ── Complete the (now-edited) whole-house item ──────────────
  const doneBtn = [...window.document.querySelectorAll("#settings-maintenance-body button")].find(
    (b) => b.textContent.includes("Done"),
  );
  click(doneBtn, window);
  await wait(50);

  await t.test("Done sends a complete request for the correct item", () => {
    assert.deepEqual(calls.complete, ["m-existing"]);
  });

  // ── Delete the coil-clean item just created ─────────────────
  const rows = [...window.document.querySelectorAll("#settings-maintenance-body tbody tr")];
  const coilRow = rows.find((r) => r.textContent.includes("Coil clean"));
  const delBtn = [...coilRow.querySelectorAll("button")].find((b) => b.title === "Delete");
  click(delBtn, window);
  await wait(50);

  await t.test("delete sends a DELETE for the correct id and removes it from the list", () => {
    assert.deepEqual(calls.delete, ["m-new-2"]);
    assert.ok(!maintenanceItems.some((m) => m.id === "m-new-2"));
    assert.doesNotMatch($(window, "#settings-maintenance-body").textContent, /Coil clean/);
  });

  window.close();
});

test("hvac-dashboard.html maintenance tab — operator can complete but not manage", async (t) => {
  const { window } = boot("operator");
  await wait(100);
  await openMaintenanceTab(window);

  await t.test("no + Add reminder button for operator", () => {
    const bodyHtml = $(window, "#settings-maintenance-body").innerHTML;
    assert.doesNotMatch(bodyHtml, /\+ Add reminder/);
  });

  await t.test("no edit/delete controls, but Done is present", () => {
    const buttons = [...window.document.querySelectorAll("#settings-maintenance-body button")];
    assert.ok(!buttons.some((b) => b.title === "Edit"));
    assert.ok(!buttons.some((b) => b.title === "Delete"));
    assert.ok(buttons.some((b) => b.textContent.includes("Done")));
  });

  window.close();
});

test("hvac-dashboard.html maintenance tab — viewer sees read-only status only", async (t) => {
  const { window } = boot("viewer");
  await wait(100);
  await openMaintenanceTab(window);

  await t.test("no + Add reminder, no Done, no edit/delete — pure read-only", () => {
    const buttons = [...window.document.querySelectorAll("#settings-maintenance-body button")];
    assert.ok(!buttons.some((b) => b.textContent.includes("Add reminder")));
    assert.ok(!buttons.some((b) => b.textContent.includes("Done")));
    assert.ok(!buttons.some((b) => b.title === "Edit"));
    assert.ok(!buttons.some((b) => b.title === "Delete"));
  });

  await t.test("the reminder itself is still visible", () => {
    assert.match($(window, "#settings-maintenance-body").textContent, /Annual service/);
  });

  window.close();
});
