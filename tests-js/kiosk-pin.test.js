"use strict";
/**
 * kiosk.html PIN keypad: backspace bottom-left, enter bottom-right. PINs are
 * 4–6 digits (auth.PIN_MIN_LENGTH/PIN_MAX_LENGTH), so entry submits on enter
 * (or automatically at 6 digits) instead of auto-submitting at 4.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "kiosk.html"),
  "utf8",
);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test("kiosk PIN keypad", async (t) => {
  const attempts = [];
  const mockFetch = async (url, opts) => {
    const u = String(url);
    if (u.includes("/auth/login-pin")) {
      attempts.push(JSON.parse(opts.body).pin);
      return {
        ok: false,
        status: 401,
        json: async () => ({ detail: "Incorrect PIN" }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
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
  await wait(50);
  const tap = (k) =>
    [...window.document.querySelectorAll("[data-key]")]
      .find((b) => b.dataset.key === k)
      .dispatchEvent(new window.Event("click", { bubbles: true }));
  const dots = () =>
    window.document.querySelectorAll("#pin-dots > span").length;

  await t.test("bottom row: backspace, 0, enter", () => {
    const keys = [...window.document.querySelectorAll("#keypad > *")].map(
      (b) => b.dataset.key,
    );
    assert.deepEqual(keys.slice(-3), ["back", "0", "enter"]);
  });

  await t.test("4 digits alone don't submit; enter does", async () => {
    ["1", "2", "3", "4"].forEach(tap);
    await wait(20);
    assert.deepEqual(attempts, []);
    tap("enter");
    await wait(20);
    assert.deepEqual(attempts, ["1234"]);
  });

  await t.test("enter with fewer than 4 digits is ignored", async () => {
    ["back", "back", "back", "back", "9", "9"].forEach(tap);
    tap("enter");
    await wait(20);
    assert.equal(attempts.length, 1);
    ["back", "back"].forEach(tap);
  });

  await t.test("5-digit PIN submits on enter, dots grow to match", async () => {
    ["5", "4", "3", "2", "1"].forEach(tap);
    assert.equal(dots(), 5);
    tap("enter");
    await wait(20);
    assert.equal(attempts.at(-1), "54321");
    [0, 1, 2, 3, 4].forEach(() => tap("back"));
  });

  await t.test("6th digit (max length) submits automatically", async () => {
    ["1", "1", "2", "2", "3", "3"].forEach(tap);
    await wait(20);
    assert.equal(attempts.at(-1), "112233");
  });

  window.close();
});
