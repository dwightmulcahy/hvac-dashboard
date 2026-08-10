"use strict";
/**
 * Extracts specific pure utility functions from hvac-dashboard.html so
 * they can be unit tested in Node without a browser or a build step.
 *
 * Deliberately does NOT use brace-counting to find function boundaries
 * — this codebase hit a real bug earlier from exactly that approach
 * (a naive brace-counter got confused by `${...}` inside a template
 * literal and silently extracted the wrong span). Instead this uses
 * exact substring boundaries: a unique "start marker" (the target
 * function's own signature) and a unique "end marker" (the next
 * function's signature, verified to exist immediately after it in the
 * current file). If hvac-dashboard.html changes shape — a function
 * gets renamed, reordered, or the next function after it changes —
 * extraction throws loudly instead of silently testing stale or wrong
 * code. That's intentional: a loud failure here means "go update this
 * extractor," not "the test is now meaningless but still green."
 *
 * Each REGION below bundles several logically-related functions
 * together (rather than one marker pair per function) specifically to
 * keep the total number of fragile boundary strings small.
 */

const fs = require("fs");
const path = require("path");

function readDashboardScript() {
  const htmlPath = path.join(__dirname, "..", "hvac-dashboard.html");
  const html = fs.readFileSync(htmlPath, "utf8");
  const start = html.indexOf("<script>") + "<script>".length;
  const end = html.lastIndexOf("</script>");
  if (start < 0 || end < 0) {
    throw new Error("extract.js: could not find <script>...</script> in hvac-dashboard.html");
  }
  return html.slice(start, end);
}

function extractBetween(source, startMarker, endMarker, label) {
  const startIdx = source.indexOf(startMarker);
  if (startIdx === -1) {
    throw new Error(
      `extract.js: start marker not found for "${label}".\n` +
      `Looked for: ${JSON.stringify(startMarker)}\n` +
      `hvac-dashboard.html has likely changed — update the marker in extract.js.`
    );
  }
  const endIdx = source.indexOf(endMarker, startIdx + startMarker.length);
  if (endIdx === -1) {
    throw new Error(
      `extract.js: end marker not found for "${label}" after its start marker.\n` +
      `Looked for: ${JSON.stringify(endMarker)}\n` +
      `hvac-dashboard.html has likely changed — update the marker in extract.js.`
    );
  }
  return source.slice(startIdx, endIdx);
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

  const regions = [
    // seerToEer, maxWatts, safeFloat, estWatts, toF, fmtTemp, fmtCF
    extractBetween(src, "function seerToEer(seer)", "async function withBusy", "watts/temp helpers"),
    // fmtWifi, fmtOnTime
    extractBetween(src, "function fmtWifi(dbm)", "async function toggleLockTemp", "fmtWifi/fmtOnTime"),
    // LOG_LEVELS, matchesFilter
    extractBetween(src, "const LOG_LEVELS=", "function renderLog()", "LOG_LEVELS/matchesFilter"),
    // RATE_DEFAULTS, loadRateSettings, saveRateSettings, rateSettings,
    // effectiveRateUsd, estCostDay, estCostMonth
    extractBetween(src, "const RATE_DEFAULTS=", "function updatePeakCard()", "rate settings + cost helpers"),
    // DAY_NAMES, fmtDays, fmtActions, nextFireMs, fmtNext
    extractBetween(src, "const DAY_NAMES=", "function renderSchedules()", "DAY_NAMES/schedule formatters"),
    // fmtHours
    extractBetween(src, "function fmtHours(h)", "function renderUsageTable(", "fmtHours"),
  ];

  const combined = regions.join("\n\n");

  // minimal browser-global stubs — loadRateSettings() calls
  // localStorage.getItem() at module-eval time via
  // `let rateSettings=loadRateSettings();`, so it needs to exist.
  const sandbox = {
    localStorage: {
      _data: {},
      getItem(key) { return Object.prototype.hasOwnProperty.call(this._data, key) ? this._data[key] : null; },
      setItem(key, val) { this._data[key] = String(val); },
    },
    console,
  };
  // expose _tempUnit as a mutable sandbox property so tests can flip
  // between 'C'/'F'/'both' the same way the dashboard's own toggle does
  sandbox._tempUnit = "both";

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
    sandbox
  );
  Object.assign(sandbox, sandbox.__bridge);

  return sandbox;
}

module.exports = { loadDashboardFunctions, readDashboardScript, extractBetween };
