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
  const demo = JSON.parse(fs.readFileSync(path.join(__dirname, "../demo.example.json"), "utf8"));
  demo.interactionMode = "screen-only";
  const rectangle = { x: .1, y: .2, width: .6, height: .6 };
  for (const scenario of demo.scenarios) {
    for (const step of scenario.steps) step.image = `assets/${step.id}.jpg`;
    scenario.steps[0].hotspots = [{ x: .8, y: .2, width: .1, height: .05, label: "Save rule", action: "next" }];
    const play = scenario.steps.find(step => step.id === "play");
    play.videoBounds = rectangle;
    play.hotspots = [{ ...rectangle, label: "Play video", action: "play" }, { x: .8, y: .1, width: .03, height: .04, label: "Close video and continue to Generate Report", action: "next" }];
    scenario.steps.splice(4, 0, { id: "generateReport", kind: "alert", title: "Generate report", image: "assets/generate.jpg", hotspots: [{ x: .8, y: .5, width: .1, height: .05, label: "Generate Report", action: "report" }] });
    scenario.video = { src: "assets/incident.mp4" };
    scenario.incident = { id:`${scenario.id}-incident`, startUtc:'2026-11-15T18:00:00Z', endUtc:'2026-11-15T18:00:30Z' };
    scenario.reports = [{ src: "assets/report.pdf", type: "PDF" }, { src: "assets/report.md", type: "Markdown" }];
    scenario.steps.find(step => step.id === "report").hotspots = [
      { x: .6, y: .3, width: .1, height: .04, label: "PDF report", action: "report" },
      { x: .6, y: .4, width: .1, height: .04, label: "Markdown report", action: "report", reportType: "markdown" },
    ];
    scenario.followups.forEach((item, index) => Object.assign(item, { answer: `Captured answer ${index + 1}`, source: { kind: "captured-chat", label: "Recorded conversation" } }));
    scenario.steps.find(step => step.id === "followup").hotspots = scenario.followups.map((item, index) => ({ x: .6, y: .2 + .3 * index, width: .3, height: .05, label: item.question, action: "question", questionIndex: index, answerBounds: { x: .6, y: .26 + .3 * index, width: .3, height: .13 } }));
  }
  return demo;
}

// Minimal DOM unit harness. This exercises application handlers, not browser rendering.
async function harness(step = "createRule", demo = fixture()) {
  assert.deepEqual(core.validateDemo(demo), []);
  const ids = new Map();
  const opened = [];
  let now=0, timerId=0;
  const timers=new Map(), imageLoads=[];
  const document = { activeElement: null, events:{}, addEventListener(name,handler) { (this.events[name] ||= []).push(handler); } };
  class Node {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase(); this.children = []; this.style = {}; this.dataset = {}; this.attributes = {};
      this.events = {}; this.className = ""; this.hidden = false; this.open = false; this.paused = true;
      this.clientWidth = 1200; this.clientHeight = 700; this.naturalWidth = 1704; this.naturalHeight = 958;
      this.classList = {
        contains: name => this.className.split(/\s+/).includes(name),
        add: name => { if (!this.classList.contains(name)) this.className += ` ${name}`; },
        remove: name => { this.className = this.className.split(/\s+/).filter(x => x !== name).join(" "); },
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
    dispatch(name) { this['on'+name]?.(); for(const handler of this.events[name] || []) handler({target:this}); }
    set src(value) { this._src=value; if(this.id==='report-page-image' && value) { imageLoads.push(value);queueMicrotask(()=>this.onload?.()); } }
    get src() { return this._src; }
    click() { for (const handler of this.events.click || []) handler({ target: this }); }
    focus() { document.activeElement = this; }
    load() { this.loads = (this.loads || 0) + 1; }
    play() { this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatch('close'); }
    closest() { return null; }
    scrollIntoView(options) { this.scrollCalls=(this.scrollCalls||0)+1;this.scrollOptions=options; }
    querySelectorAll(selector) {
      const match = selector.match(/^\[data-([a-z]+)="([^"]+)"\]$/);
      const nodes = [];
      const visit = node => { if (!node || typeof node !== "object") return; if (match && node.dataset?.[match[1]] === match[2]) nodes.push(node); for (const child of node.children || []) visit(child); };
      for (const child of this.children) visit(child);
      return nodes;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  for (const [, id] of html.matchAll(/\bid="([^"]+)"/g)) {const node=new Node();node.id=id;ids.set(id,node);}
  document.getElementById = id => ids.get(id);
  document.createElement = tag => new Node(tag);
  document.createTextNode = value => ({ textContent: value });
  const window = { WarehouseDemo: core, addEventListener() {}, open: (...args) => opened.push(args),setTimeout(handler,delay){const id=++timerId;timers.set(id,{handler,at:now+delay});return id;},clearTimeout(id){timers.delete(id);} };
  class FakeDate extends Date { static now(){return now;} }
  vm.runInNewContext(source, { window, document, Date:FakeDate, URLSearchParams, location: { pathname: "/", search: "", hash: `#scenario=hard-hat-missing&step=${step}` }, history: { replaceState() {} }, fetch: async () => ({ ok: true, json: async () => demo,text:async()=> '# Original Markdown report' }), requestAnimationFrame: handler => handler(), console });
  await new Promise(resolve => setImmediate(resolve));
  const clock={async advance(ms){const end=now+ms;let count=0;while(true){await Promise.resolve();const next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;if(++count>200)throw new Error('Unbounded auto loop');now=next[1].at;timers.delete(next[0]);next[1].handler();await Promise.resolve();}now=end;await new Promise(resolve=>setImmediate(resolve));},pending(){return timers.size;}};
  return { ids, opened, demo,clock,imageLoads,document };
}

test("native journey is restored and presentation controls belong only to architecture", () => {
  for (const forbidden of ['id="followup-panel"', 'id="questions"','id="presenter-back"','id="presenter-next"','id="tour-toggle"','id="play-auto-all"','id="report-tour-toggle"']) assert.ok(!html.includes(forbidden), forbidden);
  for(const required of ['id="architecture-automatic"','id="architecture-click-through"','id="architecture-step-back"','id="architecture-step-next"'])assert.ok(html.includes(required));
  assert.ok(!source.includes('element("button", "question-button"'));
  assert.ok(!source.includes('element("button", "button primary", "Play captured evidence'));
  assert.ok(html.includes('id="dialog-hotspots"'));
});

test("landing omits capture-date and sign-in labels without removing evidence provenance", async () => {
  assert.ok(!html.includes('id="capture-date"'));
  assert.ok(!html.includes('No sign-in or live cluster required'));
  assert.ok(!source.includes('Deployment capture ·'));
  const demo = sequentialFixture();
  const capturedDate = demo.captureDate;
  const { ids } = await harness('createRule', demo);
  assert.equal(demo.captureDate, capturedDate);
  assert.equal(ids.get('capture-provenance').textContent, `Captured ${capturedDate}`);
});

test("screen-only geometry and captured question references are validated", () => {
  const demo = fixture();
  assert.deepEqual(core.validateDemo(demo), []);
  assert.equal(core.normalizedRect({ x: .9, y: .2, width: .2, height: .1 }), false);
  demo.scenarios[0].steps.find(step => step.id === "play").videoBounds.width = .95;
  assert.ok(core.validateDemo(demo).some(error => error.includes("videoBounds")));
  const second = fixture();
  second.scenarios[0].steps.find(step => step.id === "followup").hotspots[0].questionIndex = 9;
  assert.ok(core.validateDemo(second).some(error => error.includes("questionIndex")));
  const third = fixture();
  third.scenarios[0].steps.find(step => step.id === "followup").hotspots[0].answerBounds.y = 1;
  assert.ok(core.validateDemo(third).some(error => error.includes("answerBounds")));
});

test("actual Save hotspot advances and enlarged controls use the same image geometry", async () => {
  const { ids, demo } = await harness();
  ids.get("enlarge").click();
  assert.equal(ids.get("image-dialog").open, true);
  assert.equal(ids.get("dialog-hotspots").children[0].style.left, "80%");
  ids.get("dialog-size").click();
  assert.equal(ids.get("dialog-screen-content").style.width, "1704px");
  ids.get("dialog-hotspots").children[0].click();
  assert.equal(ids.get("image-dialog").open, false);
  assert.equal(ids.get("step-title").textContent, demo.scenarios[0].steps[1].title);
});

test("native clip stays inside the genuine player while its native Close X advances", async () => {
  const { ids } = await harness("play");
  assert.equal(ids.get("hotspots").querySelector('[data-action="next"]').children[0].children[1].textContent, "Close player");
  ids.get("hotspots").querySelector('[data-action="play"]').click();
  assert.equal(ids.get("screen-container").hidden, false);
  assert.equal(ids.get("video-container").hidden, false);
  assert.equal(ids.get("video-container").style.left, "10%");
  assert.equal(ids.get("video-container").style.width, "60%");
  assert.equal(ids.get("hotspots").querySelector('[data-action="play"]').hidden, true);
  ids.get("hotspots").querySelector('[data-action="next"]').click();
  assert.equal(ids.get("video-container").hidden, true);
  assert.equal(ids.get("evidence-video").paused, true);
  assert.equal(ids.get("step-title").textContent, "Generate report");
});

test("Generate Report from enlarged screen closes the modal before showing its result", async () => {
  const { ids, demo } = await harness("generateReport");
  assert.equal(ids.get("hotspots").children[0].children[0].children[1].textContent, "Generate Report");
  ids.get("enlarge").click();
  ids.get("dialog-hotspots").children[0].click();
  assert.equal(ids.get("image-dialog").open, false);
  assert.equal(ids.get("step-title").textContent, demo.scenarios[0].steps.find(step => step.id === "report").title);
});

test("playing evidence from enlarged screen closes the image dialog and exposes the actual player", async () => {
  const { ids } = await harness("play");
  ids.get("enlarge").click();
  ids.get("dialog-hotspots").querySelector('[data-action="play"]').click();
  assert.equal(ids.get("image-dialog").open, false);
  assert.equal(ids.get("video-container").hidden, false);
  assert.equal(ids.get("evidence-video").paused, false);
});

test("PDF and Markdown open inside the same-incident viewer without a popup dependency", async () => {
  const { ids, opened } = await harness("report");
  ids.get("hotspots").children[0].click();
  assert.equal(ids.get('report-dialog').open,true);
  assert.equal(ids.get('report-native-frame').src,'assets/report.pdf');
  assert.equal(ids.get('report-download').href,'assets/report.pdf');
  ids.get('report-close').click();
  assert.equal(ids.get('step-title').textContent,'Open the incident-bound report');
  ids.get("hotspots").children[1].click();
  assert.equal(ids.get('report-download').href,'assets/report.md');
  assert.equal(ids.get('report-markdown').hidden,false);
  assert.deepEqual(opened,[]);
  assert.ok(!source.includes('window.open('));
});

test("clicking an actual chat question highlights its genuine answer and announces the recorded text", async () => {
  const { ids } = await harness("followup");
  ids.get("hotspots").querySelector('[data-question="1"]').click();
  const layer = ids.get("hotspots");
  assert.equal(layer.querySelector('[data-question="1"]').getAttribute("aria-pressed"), "true");
  const highlight = layer.children.find(node => node.className === "answer-highlight");
  assert.ok(highlight);
  assert.ok(Math.abs(parseFloat(highlight.style.top) - 56) < 1e-9);
  assert.equal(ids.get("answer").children[1].textContent, "Captured answer 2");
});

function sequentialFixture() {
  const demo = fixture();
  demo.questionFlow = 'sequential';
  demo.overview = { architecture: 'assets/infrastructure.jpg', architectureAnimation: 'assets/analytics.mp4', architecturePoster: 'assets/poster.jpg' };
  demo.overview.architectureStages=Array.from({length:16},(_,index)=>({id:`stage-${index+1}`,title:`Component ${index+1}`,description:`Explanation ${index+1}`,image:`assets/stage-${index+1}.jpg`}));
  for (const scenario of demo.scenarios) {
    scenario.reports[0].pages=Array.from({length:3},(_,i)=>({src:`assets/${scenario.id}/page-${i+1}.jpg`,page:i+1,width:1654,height:2339}));
    scenario.steps.pop();
    scenario.followups.forEach((item,index) => {
      scenario.steps.push({ id:`question${index+1}`, kind:'followup', questionIndex:index, questionPhase:'prompt', title:`Question ${index+1}`, image:`assets/question-${index+1}.jpg`, hotspots:[{x:.95,y:.92,width:.03,height:.04,label:'Send recorded question',cue:'Send question',action:'next'}] });
      scenario.steps.push({ id:`answer${index+1}`, kind:'followup', questionIndex:index, questionPhase:'answer', title:`Answer ${index+1}`, image:`assets/answer-${index+1}.jpg`, answerBounds:{x:.6,y:.7,width:.3,height:.1}, hotspots:index+1<scenario.followups.length ? [{x:.6,y:.9,width:.3,height:.05,label:'Open next question',cue:'Next question',action:'next'}] : [] });
    });
  }
  return demo;
}

test('sequential prompt shows only its recorded result before advancing to the next question', async () => {
  const demo = sequentialFixture();
  const {ids} = await harness('question1', demo);
  assert.equal(ids.get('step-image').src,'assets/question-1.jpg');
  assert.equal(ids.get('hotspots').children.length,1);
  assert.equal(ids.get('hotspots').children[0].children[0].children[1].textContent,'Send question');
  ids.get('hotspots').children[0].click();
  assert.equal(ids.get('step-image').src,'assets/answer-1.jpg');
  assert.equal(ids.get('answer').children[0].textContent,`${demo.scenarios[0].followups[0].question} Captured answer 1`);
  assert.ok(ids.get('hotspots').children.some(node=>node.className==='answer-highlight'));
  ids.get('hotspots').children[0].click();
  assert.equal(ids.get('step-image').src,'assets/question-2.jpg');
  assert.equal(ids.get('hotspots').children.length,1);
});

test('architecture opens with infrastructure, then real animation, and can replay or return', async () => {
  const {ids}=await harness('createRule',sequentialFixture());
  ids.get('architecture-open').click();
  assert.equal(ids.get('architecture-dialog').open,true);
  assert.equal(ids.get('architecture-title').textContent,'Cisco AI POD platform overview');
  assert.equal(ids.get('architecture-still').hidden,false);
  assert.equal(ids.get('architecture-video').hidden,true);
  ids.get('architecture-next').click();
  assert.equal(ids.get('architecture-still').hidden,true);
  assert.equal(ids.get('architecture-video').hidden,false);
  assert.equal(ids.get('architecture-video').src,'assets/analytics.mp4');
  assert.equal(ids.get('architecture-video').paused,false);
  ids.get('architecture-back').click();
  assert.equal(ids.get('architecture-still').hidden,false);
  assert.equal(ids.get('architecture-video').paused,true);
  ids.get('architecture-close').click();
  assert.equal(ids.get('architecture-dialog').open,false);
});

test('sequential screen and architecture local assets reject missing evidence references',()=>{
  const demo=sequentialFixture();
  assert.deepEqual(core.validateDemo(demo),[]);
  assert.ok(core.listAssetPaths(demo).includes('assets/analytics.mp4'));
  demo.scenarios[0].steps.find(step=>step.id==='answer1').questionIndex=20;
  assert.ok(core.validateDemo(demo).some(error=>error.includes('questionIndex')));
  const bad=sequentialFixture();bad.overview.architectureAnimation='https://example.com/remote.mp4';
  assert.ok(core.validateDemo(bad).some(error=>error.includes('architectureAnimation')));
});

test('architecture click-through traverses all 16 held stages in both directions',async()=>{
  const {ids,clock}=await harness('createRule',sequentialFixture());
  ids.get('architecture-open').click();ids.get('architecture-next').click();
  ids.get('architecture-click-through').click();
  assert.equal(ids.get('architecture-video').paused,true);
  assert.equal(ids.get('architecture-video').hidden,true);
  assert.equal(ids.get('architecture-step-back').disabled,true);
  for(let i=0;i<16;i++){
    assert.equal(ids.get('architecture-stage-count').textContent,`Stage ${i+1} of 16`);
    assert.equal(ids.get('architecture-stage-image').src,`assets/stage-${i+1}.jpg`);
    assert.equal(ids.get('architecture-stage-title').textContent,`Component ${i+1}`);
    assert.equal(ids.get('architecture-stage-description').textContent,`Explanation ${i+1}`);
    await clock.advance(600000);
    assert.equal(ids.get('architecture-stage-count').textContent,`Stage ${i+1} of 16`);
    if(i<15)ids.get('architecture-step-next').click();
  }
  assert.equal(ids.get('architecture-step-next').disabled,true);
  for(let i=15;i>0;i--){ids.get('architecture-step-back').click();assert.equal(ids.get('architecture-stage-count').textContent,`Stage ${i} of 16`);}
  assert.equal(clock.pending(),0);
});

test('architecture modes pause hidden video and preserve the manual stage',async()=>{
  const {ids}=await harness('createRule',sequentialFixture());
  ids.get('architecture-open').click();ids.get('architecture-next').click();
  assert.equal(ids.get('architecture-video').paused,false);
  ids.get('architecture-click-through').click();ids.get('architecture-step-next').click();
  ids.get('architecture-automatic').click();
  assert.equal(ids.get('architecture-video').paused,false);
  assert.equal(ids.get('architecture-stage-panel').hidden,true);
  ids.get('architecture-click-through').click();
  assert.equal(ids.get('architecture-stage-count').textContent,'Stage 2 of 16');
  assert.equal(ids.get('architecture-video').paused,true);
  ids.get('architecture-back').click();
  assert.equal(ids.get('architecture-mode-controls').hidden,true);
  assert.equal(ids.get('architecture-video').paused,true);
});

test('only automatic architecture playback defaults to 1.38x',async()=>{
  const {ids}=await harness('createRule',sequentialFixture());
  ids.get('architecture-open').click();ids.get('architecture-next').click();
  const animation=ids.get('architecture-video');
  assert.equal(animation.defaultPlaybackRate,1.38);
  assert.equal(animation.playbackRate,1.38);
  assert.equal(ids.get('overview-video').playbackRate,undefined);
  assert.equal(ids.get('evidence-video').playbackRate,undefined);
  ids.get('architecture-click-through').click();
  ids.get('architecture-step-next').click();
  assert.equal(animation.paused,true);
  assert.equal(ids.get('architecture-stage-count').textContent,'Stage 2 of 16');
  animation.playbackRate=1;
  ids.get('architecture-automatic').click();
  assert.equal(animation.playbackRate,1.38);
  ids.get('architecture-click-through').click();
  assert.equal(ids.get('architecture-stage-count').textContent,'Stage 2 of 16');
});

test('native scenario and report page never auto advance',async()=>{
  const {ids,clock,demo}=await harness('createRule',sequentialFixture());
  await clock.advance(600000);assert.equal(ids.get('step-title').textContent,demo.scenarios[0].steps[0].title);
  const report=await harness('report',sequentialFixture());
  report.ids.get('hotspots').children[0].click();
  await report.clock.advance(600000);assert.equal(report.ids.get('report-page-count').textContent,'Page 1 of 3');
  report.ids.get('report-page-next').click();report.ids.get('report-zoom').click();
  assert.equal(report.ids.get('report-dialog').classList.contains('report-fit-width'),true);
  report.ids.get('report-close').click();report.ids.get('hotspots').children[0].click();
  assert.equal(report.ids.get('report-page-count').textContent,'Page 2 of 3');
  assert.equal(report.clock.pending(),0);
});

test('Rendered PDF assets are listed and malformed page metadata is rejected',()=>{
  const demo=sequentialFixture();assert.deepEqual(core.validateDemo(demo),[]);
  assert.ok(core.listAssetPaths(demo).includes('assets/hard-hat-missing/page-3.jpg'));
  demo.scenarios[0].reports[0].pages[1].page=9;
  assert.ok(core.validateDemo(demo).some(e=>e.includes('ordered page numbers')));
  assert.ok(core.listAssetPaths(sequentialFixture()).includes('assets/stage-16.jpg'));
  const bad=sequentialFixture();bad.overview.architectureStages[0].image='https://example.com/image.jpg';
  assert.ok(core.validateDemo(bad).some(e=>e.includes('architecture stage')));
  assert.equal(core.architectureStageIndex(15,1,16),15);
  assert.equal(core.architectureStageIndex(0,-1,16),0);
});

test('fresh deep links fit the native capture after initial render and decoded image load',async()=>{
  const {ids}=await harness('question2',sequentialFixture());
  assert.ok(ids.get('screen-container').scrollCalls>0);
  const before=ids.get('screen-container').scrollCalls;
  ids.get('step-image').dispatch('load');
  assert.equal(ids.get('screen-container').scrollCalls,before+1);
  assert.equal(ids.get('screen-container').scrollOptions.block,'start');
});

test('starting native evidence reapplies capture offset without stealing focus from presenter controls',async()=>{
  const {ids,document}=await harness('play',sequentialFixture());
  const before=ids.get('screen-container').scrollCalls;
  ids.get('hotspots').querySelector('[data-action="play"]').click();
  assert.ok(ids.get('screen-container').scrollCalls>before);
  assert.equal(ids.get('screen-container').scrollOptions.block,'start');
  assert.notEqual(document.activeElement,ids.get('evidence-video'));
});
