"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const core = require("../core.js");
const example = JSON.parse(fs.readFileSync(path.join(__dirname, "../demo.example.json"), "utf8"));
const copy = () => JSON.parse(JSON.stringify(example));

test("example is a valid honest source-only manifest", () => {
  assert.deepEqual(core.validateDemo(example), []);
  assert.equal(example.scenarios.length, 2);
  assert.equal(example.scenarios[0].camera, "Camera_01");
  assert.equal(example.scenarios[1].camera, "Camera");
  assert.equal(core.listAssetPaths(example).length, 0);
  for (const scenario of example.scenarios) assert.deepEqual(scenario.steps.map(step => step.id), core.STEP_IDS);
});

test("asset paths allow only local relative files", () => {
  for (const value of ["assets/screens/hardhat-alert.jpg", "assets/reports/report.pdf", "assets/video/Camera 01.mp4"]) assert.equal(core.assetPath(value), true, value);
  for (const value of ["https://example.com/a.jpg", "//example.com/a.jpg", "/private/a.jpg", "../a.jpg", "a/../../b", "a/./b", "a//b", "a\\b", "data:image/png,a", "javascript:alert(1)", "a?token=secret", "a#x", "a/%2e%2e/b", "a/%252e%252e/b", "%2Fetc/passwd", "a\n.jpg", "C:/video.mp4", " a.jpg", "a.jpg ", "", null, 12]) assert.equal(core.assetPath(value), false, String(value));
});

test("navigation selects either scenario and clamps step boundaries", () => {
  let value = core.state();
  assert.equal(value.scenarioIndex, -1);
  value = core.transition(value, { type: "SCENARIO", index: 0 }, example);
  assert.deepEqual(value, { scenarioIndex: 0, stepIndex: 0, questionIndex: -1 });
  assert.deepEqual(core.transition(value, { type: "BACK" }, example), value);
  for (let i = 0; i < 8; i++) value = core.transition(value, { type: "NEXT" }, example);
  assert.equal(value.stepIndex, 5);
  value = core.transition(value, { type: "SCENARIO", index: 1 }, example);
  assert.deepEqual(value, { scenarioIndex: 1, stepIndex: 0, questionIndex: -1 });
});

test("restart clears selected question and restores first step", () => {
  let value = core.state(1, 5, 1);
  value = core.transition(value, { type: "RESTART" }, example);
  assert.deepEqual(value, core.state(1));
  assert.deepEqual(core.transition(value, { type: "HOME" }, example), core.state());
});

test("invalid navigation never escapes scenario or step bounds", () => {
  const value = core.state(0, 2);
  for (const action of [{ type: "SCENARIO", index: 99 }, { type: "SCENARIO", index: -1 }, { type: "STEP", index: -1 }, { type: "STEP", index: 20 }, { type: "STEP", index: 1.5 }, { type: "QUESTION", index: 50 }]) assert.deepEqual(core.transition(value, action, example), value);
  assert.deepEqual(core.transition(core.state(), { type: "NEXT" }, example), core.state());
});

test("question selection is restricted to the active scenario", () => {
  const selected = core.transition(core.state(0, 5), { type: "QUESTION", index: 1 }, example);
  assert.equal(selected.questionIndex, 1);
  assert.equal(core.transition(selected, { type: "SCENARIO", index: 1 }, example).questionIndex, -1);
});

test("manifest lists all screenshot, media, report and source assets once", () => {
  const data = copy();
  data.overview = { image: "assets/overview.jpg", architecture: "assets/architecture.jpg", video: "assets/overview.mp4" };
  data.scenarios[0].coverImage = "assets/overview.jpg";
  data.scenarios[0].steps[0].image = "assets/rule.jpg";
  data.scenarios[0].video = { src: "assets/evidence.mp4", poster: "assets/poster.jpg" };
  data.scenarios[0].reports = [{ src: "assets/report.pdf" }];
  data.scenarios[0].followups[0].source = { kind: "captured-chat", label: "Recorded chat", src: "assets/chat.jpg" };
  assert.equal(core.listAssetPaths(data).length, 8);
  assert.deepEqual(core.validateDemo(data), []);
});

test("captured answers require provenance, never fabricated defaults", () => {
  const data = copy();
  data.scenarios[0].followups[0].answer = "Captured answer text.";
  assert.ok(core.validateDemo(data).some(error => error.includes("require a source")));
  data.scenarios[0].followups[0].source = { kind: "captured-chat", label: "Recorded conversation, 15 November 2026" };
  assert.deepEqual(core.validateDemo(data), []);
  data.scenarios[0].followups[0].source.kind = "generated";
  assert.ok(core.validateDemo(data).length);
});

test("out-of-image hotspots are rejected", () => {
  const data = copy();
  data.scenarios[0].steps[0].hotspots = [{ x: .91, y: .24, width: .08, height: .04, label: "Save recorded rule", action: "next" }];
  assert.deepEqual(core.validateDemo(data), []);
  data.scenarios[0].steps[0].hotspots[0].width = .2;
  assert.ok(core.validateDemo(data).some(error => error.includes("normalized image bounds")));
});

test("unsupported actions and duplicate IDs are rejected", () => {
  const data = copy();
  data.scenarios[1].id = data.scenarios[0].id;
  data.scenarios[0].steps[1].id = data.scenarios[0].steps[0].id;
  data.scenarios[0].steps[0].hotspots = [{ x: 0, y: 0, width: .5, height: .5, label: "Invalid action", action: "fetch-secret" }];
  const errors = core.validateDemo(data);
  assert.ok(errors.some(error => error.includes("id must be unique")));
  assert.ok(errors.some(error => error.includes("step ids must be unique")));
  assert.ok(errors.some(error => error.includes("unsupported hotspot action")));
});

test("invalid manifests return validation errors without throwing", () => {
  for (const value of [null, [], {}, { schemaVersion: 1, title: "Example", scenarios: [null] }, { schemaVersion: 1, title: "Example", scenarios: [{ id: "case", title: "Case", camera: "Camera", steps: {}, followups: {}, reports: {} }] }]) {
    assert.ok(core.validateDemo(value).length);
  }
});

test("source includes no third-party scripts, analytics, WebSockets or free-text chat", () => {
  const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
  const script = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
  assert.ok(html.includes("Captured interactive walkthrough"));
  assert.ok(html.includes("does not run live video analytics"));
  const externalLinks = [...html.matchAll(/<a\b[^>]*href="(https?:\/\/[^\"]+)"[^>]*>/g)];
  assert.deepEqual(externalLinks.map(match => match[1]), ["https://docs.nvidia.com/vss/3.2.1/warehouse-docs/Introduction.html"]);
  assert.match(externalLinks[0][0], /target="_blank"/);
  assert.match(externalLinks[0][0], /rel="noopener noreferrer"/);
  assert.ok(!/<(?:script|link|img|video|iframe)\b[^>]*(?:src|href)="https?:\/\//.test(html));
  assert.ok(!/new WebSocket|XMLHttpRequest|localStorage|sessionStorage|innerHTML/.test(script));
  assert.ok(!/<input|<textarea/.test(html));
  assert.ok(html.includes("<video"));
  assert.ok(html.includes("aria-live=\"polite\""));
});

test("blueprint attribution sits between the introduction and demo actions", () => {
  const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
  const summary = html.indexOf('class="hero-summary"');
  const reference = html.indexOf('class="blueprint-reference"');
  const actions = html.indexOf('class="hero-actions"');
  assert.ok(summary < reference && reference < actions);
  assert.match(html, /Based on the <a[^>]+>NVIDIA VSS 3\.2\.1 Warehouse Operations Blueprint<\/a>\.<\/p>/);
  assert.match(html, /id="watch-overview"[^>]*>Watch the demo </);
  assert.ok(!html.includes("Watch the edited demo"));
});

test("customer-facing landing leads with Cisco AI POD", () => {
  const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
  assert.match(html, /<h1 id="landing-title">Cisco AI POD<br> for AI Data Platforms<\/h1>/);
  assert.ok(html.includes("Warehouse Video Intelligence with NVIDIA AI and VAST Data"));
  assert.ok(html.includes("Cisco AI POD demo home"));
  assert.ok(html.includes("<title>Cisco AI POD | Warehouse Video Intelligence</title>"));
});

test("every application DOM binding has one matching element", () => {
  const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
  const script = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length, "HTML ids must be unique");
  for (const [, id] of script.matchAll(/\$\("([^"]+)"\)/g)) assert.ok(ids.includes(id), `Missing #${id}`);
  for (const [, target] of html.matchAll(/aria-labelledby="([^"]+)"/g)) for (const id of target.split(/\s+/)) assert.ok(ids.includes(id), `Missing accessible label #${id}`);
});

test("non-string image paths and invalid report sources are rejected", () => {
  const data = copy();
  data.overview.image = { remote: "image" };
  data.scenarios[0].video = { src: 12 };
  data.scenarios[0].steps[0].image = true;
  data.scenarios[0].reports = [{}];
  const errors = core.validateDemo(data);
  assert.ok(errors.some(error => error.includes("overview.image")));
  assert.ok(errors.some(error => error.includes("video.src")));
  assert.ok(errors.some(error => error.includes("createRule image")));
  assert.ok(errors.some(error => error.includes("report needs")));
});
