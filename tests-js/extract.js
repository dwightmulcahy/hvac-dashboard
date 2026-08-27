"use strict";
/**
 * Extracts specific pure utility functions from hvac-dashboard.html so
 * they can be unit tested in Node without a browser or a build step.
 *
 * Extraction uses sentinel comments placed directly in the dashboard
 * source, not brace-counting and not adjacent-function-name matching:
 *
 *   // ── TESTABLE:region-name:start ──
 *   ...code...
 *   // ── TESTABLE:region-name:end ──
 *
 * Why sentinels instead of the alternatives:
 *
 *   - Brace-counting: this codebase hit a real bug from exactly this
 *     approach — a naive brace-counter got confused by `${...}` inside
 *     a template literal and silently extracted the wrong span.
 *
 *   - Adjacent-function-name boundaries (the first version of this
 *     file): worked, but coupled a region's extraction to whatever
 *     function happened to be defined next in the file. Renaming that
 *     *unrelated* neighboring function broke extraction for code that
 *     never changed. Sentinels are owned by whoever writes the region
 *     they wrap, so they only need to move when the region itself is
 *     restructured — not whenever anything nearby changes.
 *
 * Sentinels don't fully solve "new code accidentally lands inside an
 * existing region and gets bundled in untested" — that's still
 * possible if someone pastes a new function between an existing
 * start/end pair. What they do fix is boundary fragility, and the
 * `checkAllSentinelsConsumed()` export below at least catches the
 * inverse mistake: a new sentinel-wrapped region added to the
 * dashboard that nobody wired up here yet.
 */

const fs = require("fs");
const path = require("path");

const START_RE = /\/\/ ── TESTABLE:([\w-]+):start ──/g;
const END_RE = (name) => `// ── TESTABLE:${name}:end ──`;
const START_MARKER = (name) => `// ── TESTABLE:${name}:start ──`;

function readDashboardScript() {
  const htmlPath = path.join(__dirname, "..", "frontend", "hvac-dashboard.html");
  const html = fs.readFileSync(htmlPath, "utf8");
  const start = html.indexOf("<script>") + "<script>".length;
  const end = html.lastIndexOf("</script>");
  if (start < 0 || end < 0) {
    throw new Error("extract.js: could not find <script>...</script> in hvac-dashboard.html");
  }
  return html.slice(start, end);
}

/** Extract the code between a named sentinel pair, sentinels excluded. */
function extractRegion(source, name) {
  const startMarker = START_MARKER(name);
  const endMarker = END_RE(name);
  const startIdx = source.indexOf(startMarker);
  if (startIdx === -1) {
    throw new Error(
      `extract.js: sentinel "${startMarker}" not found in hvac-dashboard.html.\n` +
        `Either the region was renamed/removed from the dashboard, or a typo ` +
        `crept into REGIONS in extract.js — they must match exactly.`,
    );
  }
  const contentStart = startIdx + startMarker.length;
  const endIdx = source.indexOf(endMarker, contentStart);
  if (endIdx === -1) {
    throw new Error(
      `extract.js: found start sentinel for "${name}" but no matching ` +
        `"${endMarker}". Every TESTABLE:${name}:start needs a TESTABLE:${name}:end.`,
    );
  }
  return source.slice(contentStart, endIdx);
}

/** List every region name that has a start sentinel in the dashboard source. */
function listAllSentinelRegions(source) {
  const names = new Set();
  let m;
  START_RE.lastIndex = 0;
  while ((m = START_RE.exec(source)) !== null) {
    names.add(m[1]);
  }
  return names;
}

// The regions actually loaded for testing. If you add a new
// TESTABLE:...:start/:end pair to hvac-dashboard.html, add its name
// here too — checkAllSentinelsConsumed() will fail the test run if
// you forget, rather than the new region just silently going
// untested forever.
const REGIONS = [
  "log-filter",
  "rates",
  "watts-temp",
  "wifi-ontime",
  "day-names",
  "schedule-fmt",
  "fmt-hours",
  "fmt-ago",
  "select-temp-unit",
];

/**
 * Fails loudly if hvac-dashboard.html has a TESTABLE region that
 * REGIONS above doesn't know about — the "someone added a new
 * sentinel block and forgot to wire it into extract.js" case.
 */
function checkAllSentinelsConsumed(source) {
  const present = listAllSentinelRegions(source);
  const known = new Set(REGIONS);
  const unconsumed = [...present].filter((name) => !known.has(name));
  if (unconsumed.length > 0) {
    throw new Error(
      `extract.js: hvac-dashboard.html has TESTABLE region(s) not listed ` +
        `in REGIONS: ${unconsumed.join(", ")}. Add them to REGIONS in ` +
        `extract.js (and write tests for them) or remove the sentinels ` +
        `if they're no longer meant to be tested.`,
    );
  }
}

/**
 * Loads the pure utility functions into a fresh, isolated context and
 * returns them as an object. Uses Node's built-in vm module rather
 * than plain eval so the extracted code runs in its own global scope
 * instead of polluting this module's scope.
 */
function loadDashboardFunctions() {
  const vm = require("vm");
  const src = readDashboardScript();

  checkAllSentinelsConsumed(src);

  const combined = REGIONS.map((name) => extractRegion(src, name)).join("\n\n");

  // minimal browser-global stubs — loadRateSettings() calls
  // localStorage.getItem() at module-eval time via
  // `let rateSettings=loadRateSettings();`, so it needs to exist.
  const sandbox = {
    localStorage: {
      _data: {},
      getItem(key) {
        return Object.prototype.hasOwnProperty.call(this._data, key) ? this._data[key] : null;
      },
      setItem(key, val) {
        this._data[key] = String(val);
      },
    },
    console,
  };
  // expose _tempUnit as a mutable sandbox property so tests can flip
  // between 'C'/'F'/'both' the same way the dashboard's own toggle does
  sandbox._tempUnit = "both";

  // minimal DOM stub: selectTempUnit() reads/writes button styles via
  // document.getElementById(...).style.*, and calls renderUnits() as
  // a side effect (a big function with its own device-state/DOM
  // dependencies we don't want to drag in here — it's stubbed out
  // since it isn't what this test file is verifying). _buttons is
  // exposed on the returned object so tests can inspect what
  // selectTempUnit actually set on each button.
  sandbox._buttons = {};
  sandbox.document = {
    getElementById(id) {
      if (!sandbox._buttons[id]) sandbox._buttons[id] = { style: {} };
      return sandbox._buttons[id];
    },
  };
  sandbox.renderUnits = () => {};
  // selectTempUnit() also calls refreshVisibleTempInputs() to live-
  // update the vacation/schedule temp input fields — same reasoning
  // as renderUnits above, that's DOM orchestration outside the scope
  // of what these tests verify (button highlighting), not pure logic.
  sandbox.refreshVisibleTempInputs = () => {};

  vm.createContext(sandbox);
  vm.runInContext(combined, sandbox, { filename: "hvac-dashboard.html (extracted)" });

  // `const`/`let` declarations inside a vm context don't become
  // properties of the sandbox object the way `function` declarations
  // do — so DAY_NAMES, LOG_LEVELS, RATE_DEFAULTS, and rateSettings are
  // usable by the extracted functions (which close over them
  // correctly) but invisible from outside without this bridge. This
  // makes them readable so tests can build expected values from the
  // real current constants instead of guessing/duplicating them.
  vm.runInContext(
    "this.__bridge = { DAY_NAMES, LOG_LEVELS, RATE_DEFAULTS, rateSettings };",
    sandbox,
  );
  Object.assign(sandbox, sandbox.__bridge);

  return sandbox;
}

module.exports = {
  loadDashboardFunctions,
  readDashboardScript,
  extractRegion,
  listAllSentinelRegions,
  checkAllSentinelsConsumed,
  REGIONS,
};
