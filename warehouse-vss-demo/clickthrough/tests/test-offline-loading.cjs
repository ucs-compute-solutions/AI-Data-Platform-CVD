"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const core = require("../core.js");
const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");

function fixture() {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "../demo.example.json"), "utf8"));
  for (const scenario of manifest.scenarios) {
    scenario.incident = { id: `${scenario.id}-incident` };
    for (const step of scenario.steps) step.image = `assets/${step.id}.jpg`;
    scenario.steps[0].hotspots = [{ x: .8, y: .2, width: .1, height: .05, label: "Save rule", action: "next" }];
    scenario.reports = [
      { type: "PDF", src: "assets/report.pdf", pages: [1, 2, 3].map(page => ({ page, src: `assets/page-${page}.jpg`, width: 1654, height: 2339 })) },
      { type: "Markdown", src: "assets/report.md" },
    ];
    scenario.steps.find(step => step.id === "report").hotspots = [
      { x: .6, y: .3, width: .1, height: .04, label: "PDF report", action: "report" },
      { x: .6, y: .4, width: .1, height: .04, label: "Markdown report", action: "report", reportType: "markdown" },
    ];
  }
  return manifest;
}

// This unit harness tests actual handlers without changing browser privileges.
// Images, video and PDF pages retain ordinary relative file URLs in the app.
function launch(options = {}) {
  const manifest = options.manifest || fixture();
  const ids = new Map(), fetchCalls = [], historyCalls = [];
  const document = { activeElement: null, addEventListener() {} };
  class Node {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase(); this.children = []; this.style = {}; this.dataset = {}; this.attributes = {};
      this.events = {}; this.className = ""; this.hidden = false; this.open = false; this.paused = true;
      this.classList = {
        contains: name => this.className.split(/\s+/).includes(name),
        add: name => { if (!this.classList.contains(name)) this.className += ` ${name}`; },
        remove: name => { this.className = this.className.split(/\s+/).filter(value => value !== name).join(" "); },
        toggle: (name, force) => { (force === undefined ? !this.classList.contains(name) : force) ? this.classList.add(name) : this.classList.remove(name); },
      };
    }
    append(...nodes) { this.children.push(...nodes); }
    prepend(...nodes) { this.children.unshift(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    remove() {}
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name]; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
    click() { for (const handler of this.events.click || []) handler({ target: this }); }
    focus() { document.activeElement = this; }
    pause() { this.paused = true; }
    play() { this.paused = false; return Promise.resolve(); }
    load() {}
    showModal() { this.open = true; }
    close() { this.open = false; }
    scrollIntoView() {}
    querySelectorAll() { return []; }
  }
  for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) ids.set(id, new Node());
  document.getElementById = id => ids.get(id);
  document.createElement = tag => new Node(tag);
  document.createTextNode = textContent => ({ textContent });
  const window = { WarehouseDemo: core, addEventListener() {} };
  if (Object.prototype.hasOwnProperty.call(options, "payload")) window.WarehouseOfflineCapture = options.payload;
  const location = {
    protocol: options.protocol || "file:", pathname: "/demo folder/Start Demo.html", search: options.search || "",
    hash: `#scenario=hard-hat-missing&step=${options.step || "report"}`,
  };
  const fetch = async (...args) => {
    fetchCalls.push(args);
    if (!options.http) throw new Error("Offline mode must not call fetch");
    return { ok: true, json: async () => manifest, text: async () => "# Hosted original report\n" };
  };
  const history = { replaceState(...args) {
    historyCalls.push(args);
    if (options.historyError) throw Object.assign(new Error("Restricted file history"), { name: "SecurityError" });
  } };
  vm.runInNewContext(source, { window, document, URLSearchParams, location, history, fetch, requestAnimationFrame: handler => handler(), console });
  return { ids, window, location, fetchCalls, historyCalls, manifest };
}

function payload(manifest = fixture(), reports = { "assets/report.md": "# Original Markdown report\r\nExact captured text.\n" }) {
  return { format: 1, manifest, reports };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("file entry loads its validated embedded manifest synchronously without fetch", () => {
  const h = launch({ payload: payload() });
  assert.equal(h.ids.get("error").hidden, true);
  assert.equal(h.ids.get("loading").hidden, true);
  assert.equal(h.ids.get("step-title").textContent, "Open the incident-bound report");
  assert.equal(h.ids.get("scenario-grid").children.length, 2);
  assert.equal(h.fetchCalls.length, 0);
});

test("embedded Markdown uses exact original text and no fetch or HTML interpretation", () => {
  const text = "# Report\r\n<svg onload=invalid()> & original Unicode: café — person.\n";
  const h = launch({ payload: payload(fixture(), { "assets/report.md": text }) });
  h.ids.get("hotspots").children[1].click();
  assert.equal(h.ids.get("report-dialog").open, true);
  assert.equal(h.ids.get("report-markdown").textContent, text);
  assert.equal(h.ids.get("report-markdown").hidden, false);
  assert.equal(h.ids.get("report-download").href, "assets/report.md");
  assert.equal(h.fetchCalls.length, 0);
});

test("empty embedded report is an own entry, not a signal to fetch", () => {
  const h = launch({ payload: payload(fixture(), { "assets/report.md": "" }) });
  h.ids.get("hotspots").children[1].click();
  assert.equal(h.ids.get("report-markdown").textContent, "");
  assert.equal(h.fetchCalls.length, 0);
});

test("missing or inherited offline report gives a clear error without fetching", () => {
  for (const reports of [{}, Object.create({ "assets/report.md": "inherited report must not be read" })]) {
    const h = launch({ payload: payload(fixture(), reports) });
    h.ids.get("hotspots").children[1].click();
    assert.match(h.ids.get("report-markdown").textContent, /missing from the offline package/);
    assert.match(h.ids.get("report-markdown").textContent, /Start Demo\.html/);
    assert.equal(h.fetchCalls.length, 0);
  }
});

test("offline PDF keeps all three image pages and original relative download", () => {
  const h = launch({ payload: payload() });
  h.ids.get("hotspots").children[0].click();
  assert.equal(h.ids.get("report-page-image").src, "assets/page-1.jpg");
  assert.equal(h.ids.get("report-native-frame").hidden, true);
  assert.equal(h.ids.get("report-download").href, "assets/report.pdf");
  for (const page of [2, 3]) {
    h.ids.get("report-page-next").click();
    assert.equal(h.ids.get("report-page-image").src, `assets/page-${page}.jpg`);
    assert.equal(h.ids.get("report-page-count").textContent, `Page ${page} of 3`);
  }
  assert.equal(h.fetchCalls.length, 0);
});

test("file entry without offline payload explains which complete-package entry to open", () => {
  const h = launch();
  assert.equal(h.ids.get("error").hidden, false);
  assert.match(h.ids.get("error-detail").textContent, /Open Start Demo\.html from the complete extracted package/);
  assert.equal(h.fetchCalls.length, 0);
});

test("offline manifest queries are rejected rather than fetched or silently ignored", () => {
  for (const search of ["?manifest=other.json", "?manifest=demo.json", "?manifest=", "?manifest=https%3A%2F%2Fexample.com%2Fdemo.json"]) {
    const h = launch({ search, payload: payload() });
    assert.equal(h.ids.get("error").hidden, false);
    assert.match(h.ids.get("error-detail").textContent, /Custom manifest selection is unavailable/);
    assert.equal(h.fetchCalls.length, 0);
  }
});

test("invalid offline envelopes fail without falling back to network loading", () => {
  const invalid = [null, [], {}, { ...payload(), format: 2 }, { ...payload(), reports: null }, { ...payload(), reports: [] },
    { ...payload(), reports: { "assets/report.md": 1 } }, { ...payload(), reports: { "https://example.com/report.md": "remote" } }];
  for (const bundle of invalid) {
    const h = launch({ payload: bundle });
    assert.equal(h.ids.get("error").hidden, false);
    assert.match(h.ids.get("error-detail").textContent, /offline capture data is invalid/);
    assert.equal(h.fetchCalls.length, 0);
  }
});

test("embedded manifest still passes normal scenario and local asset validation", () => {
  const manifest = fixture();
  manifest.scenarios[0].video = { src: "https://example.com/video.mp4" };
  const h = launch({ payload: payload(manifest) });
  assert.equal(h.ids.get("error").hidden, false);
  assert.match(h.ids.get("error-detail").textContent, /local.*relative/i);
  assert.equal(h.fetchCalls.length, 0);
});

test("provided offline data also stays fetch-free when the folder is hosted", () => {
  const h = launch({ protocol: "https:", payload: payload() });
  assert.equal(h.ids.get("error").hidden, true);
  h.ids.get("hotspots").children[1].click();
  assert.equal(h.ids.get("report-markdown").textContent, "# Original Markdown report\r\nExact captured text.\n");
  assert.equal(h.fetchCalls.length, 0);
});

test("hosted source without payload preserves manifest and Markdown fetches", async () => {
  const h = launch({ protocol: "https:", http: true, search: "?manifest=capture.json" });
  await settle();
  assert.equal(h.ids.get("error").hidden, true);
  assert.equal(h.fetchCalls[0][0], "capture.json");
  assert.equal(h.fetchCalls[0][1].cache, "no-store");
  assert.equal(h.fetchCalls[0][1].credentials, "omit");
  h.ids.get("hotspots").children[1].click();
  await settle();
  assert.equal(h.fetchCalls[1][0], "assets/report.md");
  assert.equal(h.fetchCalls[1][1].credentials, "omit");
  assert.equal(h.ids.get("report-markdown").textContent, "# Hosted original report\n");
});

test("file navigation only changes its fragment and tolerates restricted history", () => {
  for (const historyError of [false, true]) {
    const h = launch({ payload: payload(), step: "createRule", historyError });
    h.ids.get("hotspots").children[0].click();
    assert.equal(h.ids.get("step-title").textContent, "Review the active camera rule");
    assert.equal(h.historyCalls[0][2], "#scenario=hard-hat-missing&step=activeRule");
    h.ids.get("home").click();
    assert.equal(h.historyCalls[1][2], "#");
    assert.equal(h.ids.get("landing").hidden, false);
    assert.equal(h.fetchCalls.length, 0);
  }
});
