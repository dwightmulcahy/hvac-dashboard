"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loadDashboardFunctions } = require("./extract.js");

const fns = loadDashboardFunctions();
const API = {
  version: "1.4.2", build: "2026-11-01", git_sha: "a1b2c3d",
  git_sha_full: "a1b2c3d4e5f6789012345678901234567890abcd",
  python: "3.15.0", uptime_s: 93784, devices: 6, schedules: 3,
};
const PLACEHOLDER = "DASHBOARD_VERSION_" + "PLACEHOLDER";

test("fmtUptime picks the two largest units", () => {
  assert.equal(fns.fmtUptime(42), "42s");
  assert.equal(fns.fmtUptime(125), "2m");
  assert.equal(fns.fmtUptime(3 * 3600 + 7 * 60), "3h 7m");
  assert.equal(fns.fmtUptime(93784), "1d 2h");
  assert.equal(fns.fmtUptime(null), "—");
  assert.equal(fns.fmtUptime(-1), "—");
});

test("buildInfoRows links commit and release", () => {
  const rows = Object.fromEntries(fns.buildInfoRows(API, "1.4.2").map(([l, t, h]) => [l, { t, h }]));
  assert.equal(rows.Dashboard.t, "1.4.2");
  assert.equal(rows.Commit.t, "a1b2c3d");
  assert.match(rows.Commit.h, /\/commit\/a1b2c3d4e5f6789012345678901234567890abcd$/);
  assert.match(rows.Release.h, /\/releases\/tag\/v1\.4\.2$/);
  assert.equal(rows["API uptime"].t, "1d 2h");
  assert.equal(rows.Devices.t, "6");
});

test("buildInfoRows handles unknown sha, dev build and offline API", () => {
  const rows = fns.buildInfoRows({ ...API, version: "dev", git_sha: "unknown", git_sha_full: "unknown" }, PLACEHOLDER);
  const byLabel = Object.fromEntries(rows.map((r) => [r[0], r]));
  assert.equal(byLabel.Dashboard[1], "dev");
  assert.equal(byLabel.Commit[2], null);
  assert.equal(byLabel.Release, undefined);
  assert.deepEqual([...fns.buildInfoRows(null, "1.4.2").map((r) => r[1])], ["1.4.2", "offline"]);
});

test("buildInfoMismatch only fires for a real version difference", () => {
  assert.equal(fns.buildInfoMismatch(API, "1.4.2"), null);
  assert.equal(fns.buildInfoMismatch(API, PLACEHOLDER), null);
  assert.equal(fns.buildInfoMismatch(null, "1.4.1"), null);
  assert.match(fns.buildInfoMismatch(API, "1.4.1"), /1\.4\.1.*1\.4\.2/);
});

test("Dockerfile's sed rewrites only the DASHBOARD_VERSION constant", () => {
  const html = fs.readFileSync(path.join(__dirname, "../frontend/hvac-dashboard.html"), "utf8");
  const hits = html.match(/DASHBOARD_VERSION_PLACEHOLDER/g) || [];
  assert.equal(hits.length, 1, "placeholder literal must appear exactly once (in DASHBOARD_VERSION)");
  assert.match(html, /const DASHBOARD_VERSION='DASHBOARD_VERSION_PLACEHOLDER'/);
});
