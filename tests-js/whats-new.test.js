// frontend/whats-new.js in jsdom: hidden trigger, keyboard opening, empty notes.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "frontend", "whats-new.js"), "utf8");

async function page(notes, seen) {
  const dom = new JSDOM(
    '<!doctype html><html><head></head><body><div class="menu-item" data-whats-new role="button" tabindex="0" hidden>What\'s new</div></body></html>',
    { runScripts: "outside-only", url: "http://hvac.test/" },
  );
  const w = dom.window;
  if (seen) w.localStorage.setItem("whatsNew.seenVersion", seen);
  w.fetch = async () => (notes ? { ok: true, json: async () => notes } : { ok: false, json: async () => ({}) });
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  w.eval(SRC);
  await new Promise((r) => setTimeout(r, 20));
  return w;
}
const trigger = (w) => w.document.querySelector("[data-whats-new]");
const dialog = (w) => w.document.getElementById("wn-dlg");

test("trigger stays hidden without notes", async () => {
  const w = await page(null);
  assert.equal(trigger(w).hidden, true);
  assert.equal(dialog(w), null);
});

test("trigger is revealed once notes load; Enter and Space open the dialog", async () => {
  const w = await page({ current: "1.0.0", releases: [] });
  assert.equal(trigger(w).hidden, false);
  for (const key of ["Enter", " "]) {
    trigger(w).dispatchEvent(new w.KeyboardEvent("keydown", { key, bubbles: true }));
    assert.ok(dialog(w).hasAttribute("open"));
    dialog(w).querySelector(".wn-ok").click();
    assert.ok(!dialog(w).hasAttribute("open"));
  }
  trigger(w).dispatchEvent(new w.KeyboardEvent("keydown", { key: "a", bubbles: true }));
  assert.ok(!dialog(w).hasAttribute("open"));
});

test("empty notes say so", async () => {
  const w = await page({ current: "1.0.0", releases: [] });
  trigger(w).click();
  assert.match(dialog(w).textContent, /No release notes yet\./);
});

test("opens by itself after an upgrade with user-facing changes", async () => {
  const notes = { current: "1.1.0", repository: "", releases: [
    { version: "1.1.0", date: "2026-11-24", internal: 1,
      sections: [{ type: "feat", title: "Features", items: [{ text: "eco auto" }] }] }] };
  const w = await page(notes, "1.0.0");
  assert.ok(dialog(w).hasAttribute("open"));
  assert.match(dialog(w).textContent, /eco auto/);
  assert.equal(w.localStorage.getItem("whatsNew.seenVersion"), "1.1.0");
});
