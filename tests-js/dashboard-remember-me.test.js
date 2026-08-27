"use strict";
/**
 * Functional test suite for hvac-dashboard.html's "remember me" login
 * flow — checking the box on login mints a persisted remember-token
 * (stored in localStorage, not sessionStorage — it must survive a
 * browser restart, which is the entire point), and a return visit
 * with no valid session silently redeems it instead of showing the
 * login form. See auth.py's own comment above
 * _create_remember_token for the full design rationale this mirrors.
 *
 * Follows the same jsdom-with-mocked-fetch pattern as
 * dashboard-maintenance.test.js and kiosk.test.js, but needs its own
 * boot() rather than reusing dashboard-maintenance.test.js's — that
 * one always mocks /auth/me as already-authenticated (200), since its
 * job is testing the maintenance tab, not login itself. This file's
 * whole point is exercising the 401 / no-valid-session paths that one
 * deliberately skips.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "hvac-dashboard.html"), "utf8");

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// meStatus: what /auth/me returns on boot (401 = no valid session, the
// normal case for a fresh page load with no/expired sessionStorage
// token). validRememberToken: if set, /auth/login-remember will
// accept exactly this raw token (mirrors the backend actually having
// that hash on file). presetLocalStorage: seeds localStorage before
// any script on the page runs, same as a returning visit would have.
function boot({ meStatus = 401, validRememberToken = null, presetLocalStorage = {} } = {}) {
  const calls = { login: [], remember: [], loginRemember: [], logout: [] };
  let currentRememberToken = validRememberToken;
  // Tracks every session token this mock has actually issued (via
  // /auth/login or /auth/login-remember), so /auth/me can validate
  // the presented Authorization header for real instead of returning
  // a fixed status — matching real backend behavior. This matters
  // because doLogin() calls init() on success, and init() always
  // calls checkAuth() again as its first step; a dumb static-401 mock
  // would make that second call look like still-no-session and
  // trigger an unwanted second tryRememberLogin() attempt.
  const issuedSessionTokens = new Set();

  const mockFetch = async (url, opts) => {
    const u = String(url);
    const method = opts?.method || "GET";
    const body = opts?.body ? JSON.parse(opts.body) : {};

    if (u.endsWith("/api/"))
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "ok", version: "v1.14.0", git_sha: "abc1234", build: "" }),
      };
    if (u.includes("/auth/me")) {
      const authHeader = opts?.headers?.Authorization || "";
      const presented = authHeader.replace("Bearer ", "");
      const valid = meStatus === 200 || issuedSessionTokens.has(presented);
      return valid
        ? {
            ok: true,
            status: 200,
            json: async () => ({ role: "admin", username: "admin", must_change_password: false }),
          }
        : { ok: false, status: 401, json: async () => ({ detail: "Authentication required" }) };
    }
    if (u.includes("/auth/login-remember")) {
      calls.loginRemember.push(body);
      if (body.remember_token && body.remember_token === currentRememberToken) {
        currentRememberToken = "rotated-" + body.remember_token;
        issuedSessionTokens.add("session-from-remember");
        return {
          ok: true,
          status: 200,
          json: async () => ({
            ok: true,
            token: "session-from-remember",
            remember_token: currentRememberToken,
            username: "admin",
            role: "admin",
            must_change_password: false,
          }),
        };
      }
      return {
        ok: false,
        status: 401,
        json: async () => ({ detail: "Invalid or expired remember token" }),
      };
    }
    if (u.includes("/auth/remember") && method === "POST") {
      calls.remember.push(true);
      currentRememberToken = "new-remember-token";
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true, remember_token: currentRememberToken }),
      };
    }
    if (u.includes("/auth/login") && method === "POST") {
      calls.login.push(body);
      issuedSessionTokens.add("session-token");
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ok: true,
          token: "session-token",
          username: body.username,
          role: "admin",
          must_change_password: false,
        }),
      };
    }
    if (u.includes("/auth/logout")) {
      calls.logout.push(body);
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    if (u.includes("/vacation"))
      return { ok: true, status: 200, json: async () => ({ vacation_mode: false }) };
    if (u.includes("/devices"))
      return { ok: true, status: 200, json: async () => ({ devices: [] }) };
    if (u.includes("/schedules"))
      return { ok: true, status: 200, json: async () => ({ schedules: [] }) };
    if (u.includes("/settings")) return { ok: true, status: 200, json: async () => ({}) };
    if (u.includes("/logs")) return { ok: true, status: 200, json: async () => ({ logs: [] }) };
    if (u.includes("/usage/summary") || u.includes("/usage/rolling30"))
      return { ok: true, status: 200, json: async () => ({ devices: [] }) };
    if (u.includes("/maintenance"))
      return { ok: true, status: 200, json: async () => ({ maintenance: [] }) };

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
      window.navigator.clipboard = { writeText: async () => {} };
      for (const [k, v] of Object.entries(presetLocalStorage)) {
        window.localStorage.setItem(k, v);
      }
    },
  });
  return { dom, window: dom.window, calls, currentRememberToken: () => currentRememberToken };
}

const $ = (window, sel) => window.document.querySelector(sel);
const click = (el, window) => el.dispatchEvent(new window.Event("click", { bubbles: true }));

test("fresh visit, no remember token — shows the login form, no silent-login attempted", async () => {
  const { window, calls } = boot({ meStatus: 401 });
  await wait(150);

  assert.equal($(window, "#login-modal").style.display, "flex");
  assert.equal(calls.loginRemember.length, 0);

  window.close();
});

test("fresh visit with a valid stored remember-token — silently logs in, no login form shown", async () => {
  const { window, calls } = boot({
    meStatus: 401,
    validRememberToken: "my-valid-remember-token",
    presetLocalStorage: { hvac_remember_token: "my-valid-remember-token" },
  });
  await wait(200);

  assert.equal(calls.loginRemember.length, 1);
  assert.equal(calls.loginRemember[0].remember_token, "my-valid-remember-token");
  assert.notEqual($(window, "#login-modal").style.display, "flex");

  window.close();
});

test("silent login rotates the stored token and updates localStorage", async () => {
  const { window } = boot({
    meStatus: 401,
    validRememberToken: "old-token",
    presetLocalStorage: { hvac_remember_token: "old-token" },
  });
  await wait(200);

  assert.equal(window.localStorage.getItem("hvac_remember_token"), "rotated-old-token");

  window.close();
});

test("expired/invalid stored remember-token — falls back to the login form and clears it", async () => {
  const { window, calls } = boot({
    meStatus: 401,
    validRememberToken: "the-real-token",
    presetLocalStorage: { hvac_remember_token: "a-stale-or-forged-token" },
  });
  await wait(200);

  assert.equal(calls.loginRemember.length, 1);
  assert.equal($(window, "#login-modal").style.display, "flex");
  assert.equal(window.localStorage.getItem("hvac_remember_token"), null);

  window.close();
});

test("logging in without checking Remember me never mints a remember-token", async () => {
  const { window, calls } = boot({ meStatus: 401 });
  await wait(150);

  $(window, "#login-user").value = "admin";
  $(window, "#login-pw").value = "admin";
  $(window, "#login-remember").checked = false;
  click($(window, '#login-modal button[onclick="doLogin()"]'), window);
  await wait(150);

  assert.equal(calls.login.length, 1);
  assert.equal(calls.remember.length, 0);
  assert.equal(window.localStorage.getItem("hvac_remember_token"), null);

  window.close();
});

test("logging in with Remember me checked mints and stores a remember-token", async () => {
  const { window, calls } = boot({ meStatus: 401 });
  await wait(150);

  $(window, "#login-user").value = "admin";
  $(window, "#login-pw").value = "admin";
  $(window, "#login-remember").checked = true;
  click($(window, '#login-modal button[onclick="doLogin()"]'), window);
  await wait(150);

  assert.equal(calls.login.length, 1);
  assert.equal(calls.remember.length, 1);
  assert.equal(window.localStorage.getItem("hvac_remember_token"), "new-remember-token");

  window.close();
});

test("logging out with a stored remember-token sends it to the server and clears it locally", async () => {
  const { window, calls } = boot({
    meStatus: 200,
    presetLocalStorage: { hvac_remember_token: "a-remembered-device" },
  });
  await wait(150);

  await window.doLogout();
  await wait(50);

  assert.equal(calls.logout.length, 1);
  assert.equal(calls.logout[0].remember_token, "a-remembered-device");
  assert.equal(window.localStorage.getItem("hvac_remember_token"), null);

  window.close();
});

test("logging out with no stored remember-token sends an empty body and doesn't throw", async () => {
  const { window, calls } = boot({ meStatus: 200 });
  await wait(150);

  await assert.doesNotReject(() => window.doLogout());
  await wait(50);

  assert.equal(calls.logout.length, 1);
  assert.equal(calls.logout[0].remember_token, undefined);

  window.close();
});
