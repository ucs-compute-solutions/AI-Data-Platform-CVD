(function () {
  "use strict";
  const core = window.WarehouseDemo;
  const $ = id => document.getElementById(id);
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  let demo;
  let offlineMode = false, bundledReports = null;
  let current = core.state();
  let screenPath = "";
  let previousFocus;
  let dialogInteractive = false;
  const video = $("evidence-video");
  const overviewVideo = $("overview-video");
  const architectureVideo = $("architecture-video");
  const ARCHITECTURE_PLAYBACK_RATE = 1.38;
  const architectureDialog = $("architecture-dialog");
  const dialog = $("image-dialog");
  const reportDialog = $("report-dialog");
  let currentReport = null, reportPage = 0;
  let architectureMode = "automatic", architectureStep = 0;
  const reportPositions = new Map();
  const upcomingScreens = [];

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function show(node, visible = true) { node.hidden = !visible; }
  function clear(node) { node.replaceChildren(); }
  function safeLink(label, src, className = "download-link") {
    if (!core.assetPath(src)) return null;
    const link = element("a", className, label);
    link.href = src;
    link.target = "_blank";
    link.rel = "noopener";
    return link;
  }

  function pauseVideo() { video.pause(); overviewVideo.pause(); architectureVideo.pause(); }
  function focusHeading(node) {
    node.setAttribute("tabindex", "-1");
    node.focus({ preventScroll: true });
  }

  function writeHash() {
    const scenario = demo.scenarios[current.scenarioIndex];
    const hash = scenario ? `#scenario=${encodeURIComponent(scenario.id)}&step=${encodeURIComponent(scenario.steps[current.stepIndex].id)}` : "";
    if (location.protocol === "file:") {
      // Fragment-only changes preserve spaces, drive prefixes and the chosen
      // entry filename. Some browsers restrict history for opaque file origins.
      try { history.replaceState(null, "", hash || "#"); }
      catch (error) { if (error.name !== "SecurityError") throw error; }
    } else history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
  }

  function navigate(action, { focus = true } = {}) {
    if (!demo) return;
    if (reportDialog.open) closeReport();
    pauseVideo();
    current = core.transition(current, action, demo);
    writeHash();
    render(focus);
  }

  function renderLanding(focus) {
    show($("landing"));
    show($("walkthrough"), false);
    if (focus) focusHeading($("landing-title"));
  }

  function renderCards() {
    const grid = $("scenario-grid");
    clear(grid);
    demo.scenarios.forEach((scenario, index) => {
      const card = element("article", "scenario-card");
      const cover = element("div", "scenario-cover");
      const imagePath = scenario.coverImage || scenario.steps.find(step => step.id === "alert")?.image || scenario.steps[0]?.image;
      if (imagePath) {
        const image = element("img");
        image.src = imagePath;
        image.alt = `${scenario.title} — captured ${scenario.camera} screen`;
        image.loading = "lazy";
        image.addEventListener("error", () => {
          image.remove();
          cover.prepend(element("div", "cover-placeholder", "Captured screen unavailable"));
        }, { once: true });
        cover.append(image);
      } else cover.append(element("div", "cover-placeholder", "Capture not yet packaged"));
      cover.append(element("span", "camera-tag", scenario.camera));
      const copy = element("div", "scenario-copy");
      copy.append(element("span", "eyebrow", `Scenario ${String(index + 1).padStart(2, "0")}`));
      copy.append(element("h3", "", scenario.title));
      copy.append(element("p", "", scenario.summary || "Create the rule, review its alert, and inspect the captured evidence and report."));
      const start = element("button", "scenario-start", "Explore scenario");
      start.type = "button";
      start.setAttribute("aria-label", `Explore ${scenario.title} on ${scenario.camera}`);
      start.append(element("span", "", "→"));
      start.addEventListener("click", () => navigate({ type: "SCENARIO", index }));
      copy.append(start);
      card.append(cover, copy);
      grid.append(card);
    });
  }

  function renderStepNavigation(scenario) {
    const nav = $("steps");
    clear(nav);
    const groups = [];
    scenario.steps.forEach((step, index) => {
      const key = step.questionPhase ? `question-${step.questionIndex}` : step.id;
      const previous = groups[groups.length - 1];
      if (previous?.key === key) previous.lastIndex = index;
      else groups.push({ key, step, index, lastIndex: index });
    });
    groups.forEach(({ step, index, lastIndex }, groupIndex) => {
      const button = element("span", "step-nav");
      if (index <= current.stepIndex && current.stepIndex <= lastIndex) { button.classList.add("is-current"); button.setAttribute("aria-current", "step"); }
      if (lastIndex < current.stepIndex) button.classList.add("is-complete");
      button.append(element("span", "step-number", lastIndex < current.stepIndex ? "✓" : String(groupIndex + 1)));
      button.append(element("span", "", step.questionPhase ? `Question ${step.questionIndex + 1}` : step.navLabel || step.title));
      button.setAttribute("aria-label", step.questionPhase ? `Question ${step.questionIndex + 1}: prompt and recorded answer` : `Step ${index + 1}: ${step.title}`);
      nav.append(button);
    });
  }

  function kindOf(step) { return step.kind || step.id; }
  function stepOfKind(kind) {
    const steps = demo.scenarios[current.scenarioIndex].steps;
    return steps.findIndex(step => kindOf(step) === kind);
  }
  function jumpToKind(kind) {
    const index = stepOfKind(kind);
    if (index >= 0) navigate({ type: "STEP", index });
    else navigate({ type: "NEXT" });
  }

  function hotspotAction(hotspot) {
    const action = hotspot.action;
    if (dialog.open && action !== "question") dialog.close();
    if (action === "next") navigate({ type: "NEXT" });
    else if (action === "play") {
      if (kindOf(demo.scenarios[current.scenarioIndex].steps[current.stepIndex]) !== "play") jumpToKind("play");
      showVideo();
    } else if (action === "report") {
      const scenario = demo.scenarios[current.scenarioIndex];
      if (kindOf(scenario.steps[current.stepIndex]) === "report") {
        openReport(hotspot.reportType || "pdf");
      } else jumpToKind("report");
    }
    else if (action === "followup") jumpToKind("followup");
    else if (action === "question") {
      current = core.transition(current, { type: "QUESTION", index: hotspot.questionIndex }, demo);
      const scenario = demo.scenarios[current.scenarioIndex];
      const step = scenario.steps[current.stepIndex];
      renderHotspots(step);
      renderAnswer(scenario);
      if (dialog.open && dialogInteractive) renderHotspots(step, $("dialog-hotspots"));
      const layer = dialog.open ? $("dialog-hotspots") : $("hotspots");
      layer.querySelector(`[data-question="${hotspot.questionIndex}"]`)?.focus({ preventScroll: true });
    }
  }

  function placeRectangle(node, bounds) {
    node.style.left = `${bounds.x * 100}%`;
    node.style.top = `${bounds.y * 100}%`;
    node.style.width = `${bounds.width * 100}%`;
    node.style.height = `${bounds.height * 100}%`;
  }

  function cueFor(hotspot, index) {
    if (hotspot.cue) return hotspot.cue;
    if (hotspot.action === "question") return `Question ${hotspot.questionIndex + 1}`;
    if (/close/i.test(hotspot.label)) return "Close player";
    if (/generate/i.test(hotspot.label)) return "Generate Report";
    if (hotspot.action === "report") return hotspot.reportType === "markdown" ? "Open Markdown" : "Open PDF";
    if (hotspot.action === "play") return "Play video";
    if (hotspot.action === "followup") return "Open questions";
    if (/save/i.test(hotspot.label)) return "Save rule";
    if (/view alerts/i.test(hotspot.label)) return "View Alerts";
    return `Continue ${index + 1}`;
  }

  function renderHotspots(step, layer = $("hotspots")) {
    clear(layer);
    (step.hotspots || []).forEach((hotspot, index) => {
      const button = element("button", "hotspot");
      button.type = "button";
      button.setAttribute("aria-label", hotspot.label);
      button.title = hotspot.label;
      placeRectangle(button, hotspot);
      button.dataset.hotspot = String(index);
      button.dataset.action = hotspot.action;
      button.dataset.download = String(hotspot.action === "report" && kindOf(step) === "report");
      button.dataset.cueAlign = hotspot.x + hotspot.width > .93 ? "right" : "left";
      button.dataset.cueSide = hotspot.y < .09 ? "below" : "above";
      if (hotspot.action === "question") {
        button.dataset.question = String(hotspot.questionIndex);
        button.setAttribute("aria-pressed", String(hotspot.questionIndex === current.questionIndex));
      }
      const cue = element("span", "hotspot-cue");
      cue.setAttribute("aria-hidden", "true");
      cue.append(element("b", "hotspot-number", String(index + 1)), document.createTextNode(cueFor(hotspot, index)));
      button.append(cue);
      button.addEventListener("click", () => hotspotAction(hotspot));
      layer.append(button);
      if (hotspot.action === "question" && hotspot.questionIndex === current.questionIndex && core.normalizedRect(hotspot.answerBounds)) {
        const highlight = element("div", "answer-highlight");
        highlight.setAttribute("aria-hidden", "true");
        placeRectangle(highlight, hotspot.answerBounds);
        layer.append(highlight);
      }
    });
    if (core.normalizedRect(step.answerBounds)) {
      const highlight = element("div", "answer-highlight");
      highlight.setAttribute("aria-hidden", "true");
      placeRectangle(highlight, step.answerBounds);
      layer.append(highlight);
    }
    if (layer === $("hotspots")) {
      $("click-cue").replaceChildren(element("i"));
      $("click-cue").append(document.createTextNode(step.hotspots?.length ? "Click the highlighted control on the captured screen." : step.questionPhase === "answer" ? "Scenario complete. Use All scenarios above to explore the other workflow." : "Use the keyboard arrows to review another captured screen."));
    }
  }

  function renderScreenshot(step, scenario) {
    const image = $("step-image");
    screenPath = step.image || "";
    show($("screen-container"));
    show($("video-container"), false);
    show($("show-image"), false);
    $("enlarge").disabled = !screenPath;
    show($("screen-content"), Boolean(screenPath));
    show($("missing-image"), !screenPath);
    image.onload = () => {
      show($("screen-content")); show($("missing-image"), false); $("enlarge").disabled = false;
      requestAnimationFrame(() => $("screen-container").scrollIntoView({ block: "start", behavior: "instant" }));
    };
    image.onerror = () => { show($("screen-content"), false); show($("missing-image")); $("enlarge").disabled = true; };
    if (screenPath) {
      const expected = screenPath;
      const commit = () => {
        if (screenPath !== expected) return;
        image.alt = `${scenario.title}: ${step.title}. Actual captured Warehouse interface.`;
        image.src = expected;
        renderHotspots(step);
        if (!$("video-container").hidden) for (const button of $("hotspots").querySelectorAll('[data-action="play"]')) show(button, false);
      };
      const preload = document.createElement("img");
      preload.src = expected;
      if (typeof preload.decode === "function") {
        clear($("hotspots"));
        preload.decode().then(commit).catch(() => image.onerror());
      } else commit();
    } else image.removeAttribute("src");
    upcomingScreens.length = 0;
    scenario.steps.slice(current.stepIndex + 1, current.stepIndex + 3).forEach(next => {
      const preload = document.createElement("img");
      preload.src = next.image;
      preload.decode?.().catch(() => {});
      upcomingScreens.push(preload);
    });
  }

  function renderIncident(scenario, step) {
    const panel = $("incident-details");
    clear(panel);
    const incident = scenario.incident;
    if (!incident || ["createRule", "activeRule"].includes(kindOf(step))) { show(panel, false); return; }
    show(panel);
    panel.append(element("span", "detail-label", "Same captured incident"));
    const list = element("dl");
    for (const [label, value] of [["Camera", scenario.camera], ["UTC interval", incident.startUtc && incident.endUtc ? `${incident.startUtc} → ${incident.endUtc}` : ""], ["Incident ID", incident.id]]) {
      if (value) list.append(element("dt", "", label), element("dd", "", value));
    }
    panel.append(list);
  }

  function renderDetails(scenario, step) {
    const details = $("step-details");
    clear(details);
    const kind = kindOf(step);
    if (["createRule", "activeRule"].includes(kind) && scenario.rule) {
      details.append(element("span", "detail-label", "Recorded rule"));
      details.append(element("p", "rule-name", scenario.rule.name || scenario.title));
      if (scenario.rule.prompt) details.append(element("blockquote", "prompt-box", scenario.rule.prompt));
      if (kind === "createRule") details.append(element("p", "guide-note", "The selected camera and English prompt are captured from the demonstrated rule. The highlighted Save control advances this replay; it does not create a live rule."));
    }
    if (kind === "play") {
      if (scenario.video?.src) {
        details.append(element("p", "guide-note", scenario.video.caption || "The saved MP4 belongs to this incident's camera and recorded time window."));
      } else details.append(element("p", "guide-note", "The incident video has not been added to this capture package. The captured screen is available above."));
    }
    if (["report", "followup"].includes(kind)) {
      details.append(element("p", "guide-note", "Open report links and questions directly on the captured screen."));
    }
    if (step.note) details.append(element("p", "guide-note", step.note));
  }

  function showVideo(restart = false) {
    const scenario = demo.scenarios[current.scenarioIndex];
    if (!scenario?.video?.src) return;
    if (video.dataset.src !== scenario.video.src) {
      video.src = scenario.video.src;
      video.dataset.src = scenario.video.src;
      if (scenario.video.poster) video.poster = scenario.video.poster;
      else video.removeAttribute("poster");
      video.load();
    }
    if (restart || video.ended) video.currentTime = 0;
    $("video-caption").textContent = scenario.video.caption || `Recorded ${scenario.camera} evidence for the selected incident.`;
    show($("video-error"), false);
    const step = scenario.steps[current.stepIndex];
    const bounds = step.videoBounds || step.hotspots?.find(hotspot => hotspot.action === "play");
    if (!core.normalizedRect(bounds)) return;
    placeRectangle($("video-container"), bounds);
    show($("screen-container"));
    show($("video-container"));
    for (const button of $("hotspots").querySelectorAll('[data-action="play"]')) show(button, false);
    show($("show-image"), false);
    $("stage-label").textContent = "Captured incident evidence · local MP4";
    video.play().catch(() => { /* The native Play control remains available. */ });
    requestAnimationFrame(() => $("screen-container").scrollIntoView({ block: "start", behavior: "instant" }));
  }

  function reportKey() { return `${demo.scenarios[current.scenarioIndex].id}:${currentReport?.type}`; }

  function closeReport() {
    if (!reportDialog.open) return;
    reportDialog.close();
    $("report-native-frame").removeAttribute("src");
  }

  function renderReportPage() {
    const pages = currentReport?.pages || [], page = pages[reportPage];
    $("report-page-back").disabled = reportPage === 0;
    $("report-page-next").disabled = !pages.length || reportPage === pages.length - 1;
    $("report-page-count").textContent = pages.length ? `Page ${reportPage + 1} of ${pages.length}` : "Original PDF";
    show($("report-page-image"), Boolean(page));
    show($("report-native-frame"), !page && String(currentReport?.type).toLowerCase() === "pdf");
    show($("report-markdown"), String(currentReport?.type).toLowerCase() === "markdown");
    show($("report-page-controls"), pages.length > 0);
    show($("report-page-error"), false);
    if (page) {
      const expectedReport = currentReport, expectedPage = reportPage;
      const image = $("report-page-image");
      image.alt = `Original ${demo.scenarios[current.scenarioIndex].title} report, page ${page.page} of ${pages.length}`;
      image.onload = () => { if (currentReport !== expectedReport || reportPage !== expectedPage) return; };
      image.onerror = () => show($("report-page-error"));
      image.src = page.src;
      reportPositions.set(reportKey(), reportPage);
    } else if (String(currentReport?.type).toLowerCase() === "pdf") {
      $("report-native-frame").src = currentReport.src;
    }
    $("report-page-area").scrollTop = 0;
    $("report-status").textContent = "Original report · use page controls or return to this incident.";
  }

  function openReport(type = "pdf") {
    const scenario = demo.scenarios[current.scenarioIndex];
    const report = scenario.reports?.find(item => String(item.type).toLowerCase() === type);
    if (!report || !core.assetPath(report.src)) return;
    currentReport = report;
    reportPage = reportPositions.get(reportKey()) || 0;
    $("report-title").textContent = `${scenario.title} · ${type === "pdf" ? "PDF report" : "Markdown report"}`;
    $("report-identity").textContent = `${scenario.camera} · incident ${scenario.incident.id}`;
    $("report-original").href = report.src;
    $("report-original").textContent = type === "pdf" ? "Open original PDF" : "Open original Markdown";
    $("report-download").href = report.src;
    $("report-download").setAttribute("download", report.src.split("/").pop());
    $("report-download").textContent = type === "pdf" ? "Download PDF" : "Download Markdown";
    reportDialog.classList.remove("report-fit-width");
    $("report-zoom").textContent = "Fit width / read closely";
    if (!reportDialog.open) reportDialog.showModal();
    renderReportPage();
    if (type === "markdown") {
      $("report-markdown").textContent = "Opening the original Markdown report…";
      if (bundledReports && own(bundledReports, report.src)) {
        $("report-markdown").textContent = bundledReports[report.src];
        return;
      }
      if (offlineMode) {
        $("report-markdown").textContent = "This Markdown report is missing from the offline package. Open Start Demo.html from the complete extracted package.";
        return;
      }
      fetch(report.src, { credentials: "omit" }).then(response => {
        if (!response.ok) throw new Error("Report unavailable");
        return response.text();
      }).then(text => { if (currentReport === report) $("report-markdown").textContent = text; })
        .catch(() => { $("report-markdown").textContent = "Use Open original or Download to read this report."; });
    }
  }

  function renderAnswer(scenario) {
    const container = $("answer");
    clear(container);
    const followup = scenario.followups?.[current.questionIndex];
    if (!followup) { container.append(element("p", "answer-placeholder", "Choose a question to reveal the captured answer.")); return; }
    if (!followup.answer) {
      container.append(element("p", "answer-placeholder", "A verified captured answer has not been included for this question. No answer is generated by this walkthrough."));
      return;
    }
    container.append(element("span", "detail-label", "Captured answer"));
    container.append(element("p", "", followup.answer));
    container.append(element("div", "answer-source", followup.source.label));
  }

  function renderFollowups(scenario, step) {
    clear($("answer"));
    if (step.questionPhase) {
      const item = scenario.followups[step.questionIndex];
      $("answer").append(element("p", "", step.questionPhase === "answer" ? `${item.question} ${item.answer}` : item.question));
    } else if (kindOf(step) === "followup") renderAnswer(scenario);
  }

  function render(focus = true) {
    const scenario = demo.scenarios[current.scenarioIndex];
    if (!scenario) { renderLanding(focus); return; }
    const step = scenario.steps[current.stepIndex];
    show($("landing"), false);
    show($("walkthrough"));
    $("walkthrough-title").textContent = scenario.title;
    $("scenario-camera").textContent = `${scenario.camera} · captured scenario`;
    $("step-count").textContent = `Step ${current.stepIndex + 1} of ${scenario.steps.length}`;
    $("step-eyebrow").textContent = `Step ${String(current.stepIndex + 1).padStart(2, "0")} · ${step.navLabel || step.title}`;
    $("step-title").textContent = step.title;
    $("step-instruction").textContent = step.instruction || "Explore the captured screen, then continue to the next step.";
    $("stage-label").textContent = "Captured Warehouse UI";
    $("capture-provenance").textContent = step.captureLabel || (demo.captureDate ? `Captured ${demo.captureDate}` : "Recorded deployment");
    renderStepNavigation(scenario);
    renderScreenshot(step, scenario);
    renderDetails(scenario, step);
    renderIncident(scenario, step);
    renderFollowups(scenario, step);
    if (focus) {
      focusHeading($("step-title"));
    }
    requestAnimationFrame(() => $("screen-container").scrollIntoView({ block: "start", behavior: "instant" }));
  }

  function syncDialogLayout() {
    if (!dialog.open) return;
    const image = $("dialog-image");
    if (!image.naturalWidth || !image.naturalHeight) return;
    const area = $("dialog-image-area");
    const scale = dialog.classList.contains("original-size") ? 1 : Math.min(
      (area.clientWidth - 24) / image.naturalWidth,
      (area.clientHeight - 24) / image.naturalHeight, 1);
    if (scale <= 0) return;
    $("dialog-screen-content").style.width = `${image.naturalWidth * scale}px`;
    $("dialog-screen-content").style.height = `${image.naturalHeight * scale}px`;
  }

  function openImage(src, title, alt, interactive = false) {
    if (!core.assetPath(src)) return;
    previousFocus = document.activeElement;
    $("dialog-image").src = src;
    $("dialog-image").alt = alt || title;
    $("dialog-title").textContent = title;
    dialogInteractive = interactive;
    clear($("dialog-hotspots"));
    if (interactive) renderHotspots(demo.scenarios[current.scenarioIndex].steps[current.stepIndex], $("dialog-hotspots"));
    dialog.classList.remove("original-size");
    $("dialog-size").textContent = "Original size";
    dialog.showModal();
    requestAnimationFrame(syncDialogLayout);
  }

  function closeImage() { dialog.close(); previousFocus?.focus(); }

  function renderArchitectureStage() {
    const stages = demo.overview?.architectureStages || [];
    architectureStep = core.architectureStageIndex(architectureStep, 0, stages.length);
    const stage = stages[architectureStep];
    $("architecture-step-back").disabled = architectureStep === 0;
    $("architecture-step-next").disabled = !stages.length || architectureStep === stages.length - 1;
    $("architecture-stage-count").textContent = stages.length ? `Stage ${architectureStep + 1} of ${stages.length}` : "Stages unavailable";
    if (!stage) return;
    $("architecture-stage-image").src = stage.image;
    $("architecture-stage-image").alt = `Stage ${architectureStep + 1}: ${stage.title}. Current component highlighted.`;
    $("architecture-stage-title").textContent = stage.title;
    $("architecture-stage-description").textContent = stage.description;
    $("architecture-stage-status").textContent = "Click-through · this stage stays on screen until you choose Back or Next.";
  }

  function architectureModePage(mode) {
    const stages = demo.overview?.architectureStages || [];
    architectureMode = mode === "click-through" && stages.length ? "click-through" : "automatic";
    const manual = architectureMode === "click-through";
    architectureDialog.classList.toggle("architecture-manual", manual);
    $("architecture-automatic").setAttribute("aria-pressed", String(!manual));
    $("architecture-click-through").setAttribute("aria-pressed", String(manual));
    $("architecture-click-through").disabled = !stages.length;
    show(architectureVideo, !manual);
    show($("architecture-stage-panel"), manual);
    if (manual) {
      architectureVideo.pause();
      renderArchitectureStage();
    } else {
      if (!architectureVideo.getAttribute("src")) {
        architectureVideo.src = demo.overview.architectureAnimation;
        if (demo.overview.architecturePoster) architectureVideo.poster = demo.overview.architecturePoster;
      }
      architectureVideo.defaultPlaybackRate = ARCHITECTURE_PLAYBACK_RATE;
      architectureVideo.playbackRate = ARCHITECTURE_PLAYBACK_RATE;
      architectureVideo.play().catch(() => { /* Native playback controls remain available. */ });
    }
  }

  function architecturePage(technical) {
    const overview = demo.overview || {};
    show($("architecture-still"), !technical);
    if (!technical) architectureDialog.classList.remove("architecture-manual");
    show(architectureVideo, technical && architectureMode === "automatic");
    show($("architecture-mode-controls"), technical);
    show($("architecture-stage-panel"), technical && architectureMode === "click-through");
    show($("architecture-next"), !technical && Boolean(overview.architectureAnimation));
    show($("architecture-back"), technical);
    $("architecture-title").textContent = technical ? "Cisco AI POD — Warehouse Analytics in Motion" : "Cisco AI POD platform overview";
    $("architecture-description").textContent = technical ? "Choose Automatic for the animated flow, or Click-through to explain each highlighted component with Back and Next." : "Shared platform architecture. Next: Warehouse-specific components and GPU allocation.";
    show($("architecture-zoom"), !technical);
    architectureDialog.classList.remove("architecture-zoomed");
    $("architecture-zoom").textContent = "Zoom image";
    if (technical) architectureModePage(architectureMode);
    else architectureVideo.pause();
  }

  function openArchitecture() {
    if (!core.assetPath(demo.overview?.architecture)) return;
    pauseVideo();
    previousFocus = document.activeElement;
    architectureMode = "automatic";
    architectureStep = 0;
    $("architecture-still").src = demo.overview.architecture;
    architecturePage(false);
    architectureDialog.showModal();
  }

  function initialState() {
    const params = new URLSearchParams(location.hash.slice(1));
    const index = demo.scenarios.findIndex(scenario => scenario.id === params.get("scenario"));
    if (index < 0) return core.state();
    const stepIndex = demo.scenarios[index].steps.findIndex(step => step.id === params.get("step"));
    return core.state(index, Math.max(0, stepIndex));
  }

  async function loadDemo() {
    show($("loading")); show($("error"), false); show($("landing"), false); show($("walkthrough"), false);
    try {
      const params = new URLSearchParams(location.search);
      offlineMode = location.protocol === "file:" || own(window, "WarehouseOfflineCapture");
      bundledReports = null;
      let data;
      if (offlineMode) {
        if (params.has("manifest")) throw new Error("Custom manifest selection is unavailable in the offline walkthrough. Open Start Demo.html without a manifest query.");
        if (!own(window, "WarehouseOfflineCapture")) throw new Error("This offline walkthrough is incomplete. Open Start Demo.html from the complete extracted package.");
        const payload = window.WarehouseOfflineCapture;
        if (!payload || typeof payload !== "object" || Array.isArray(payload) || payload.format !== 1 ||
            !own(payload, "manifest") || !own(payload, "reports") || !payload.reports ||
            typeof payload.reports !== "object" || Array.isArray(payload.reports) ||
            Object.entries(payload.reports).some(([src, text]) => !core.assetPath(src) || typeof text !== "string")) {
          throw new Error("The offline capture data is invalid. Open Start Demo.html from the complete extracted package.");
        }
        data = payload.manifest;
        bundledReports = payload.reports;
      } else {
        const name = params.get("manifest") || "demo.json";
        if (!core.assetPath(name)) throw new Error("The manifest must be a local relative file.");
        const response = await fetch(name, { cache: "no-store", credentials: "omit" });
        if (!response.ok) throw new Error("The captured demo manifest is not available. Open the complete packaged walkthrough using its local server or published URL.");
        data = await response.json();
      }
      const errors = core.validateDemo(data);
      if (errors.length) throw new Error(errors.join(" "));
      demo = data;
      document.title = "Cisco AI POD | Warehouse Video Intelligence";
      if (demo.overview?.image) {
        $("overview-image").src = demo.overview.image;
        $("overview-image").onerror = () => { show($("overview-wrap"), false); $("landing").classList.add("no-image"); };
        show($("overview-wrap"));
        $("landing").classList.remove("no-image");
      } else $("landing").classList.add("no-image");
      show($("architecture-open"), Boolean(demo.overview?.architecture));
      show($("watch-overview"), Boolean(demo.overview?.video));
      renderCards();
      current = initialState();
      show($("loading"), false);
      render(false);
    } catch (error) {
      show($("loading"), false);
      $("error-detail").textContent = error.message;
      show($("error"));
    }
  }

  $("retry").addEventListener("click", loadDemo);
  $("home").addEventListener("click", () => navigate({ type: "HOME" }));
  $("restart").addEventListener("click", () => navigate({ type: "RESTART" }));
  $("show-image").addEventListener("click", () => {
    pauseVideo();
    show($("video-container"), false); show($("screen-container")); show($("show-image"), false);
    $("stage-label").textContent = "Captured Warehouse UI";
  });
  $("enlarge").addEventListener("click", () => openImage(screenPath, $("step-title").textContent, $("step-image").alt, true));
  $("architecture-open").addEventListener("click", openArchitecture);
  $("architecture-next").addEventListener("click", () => architecturePage(true));
  $("architecture-back").addEventListener("click", () => architecturePage(false));
  $("architecture-automatic").addEventListener("click", () => architectureModePage("automatic"));
  $("architecture-click-through").addEventListener("click", () => architectureModePage("click-through"));
  $("architecture-step-back").addEventListener("click", () => { architectureStep = core.architectureStageIndex(architectureStep, -1, demo.overview.architectureStages.length); renderArchitectureStage(); });
  $("architecture-step-next").addEventListener("click", () => { architectureStep = core.architectureStageIndex(architectureStep, 1, demo.overview.architectureStages.length); renderArchitectureStage(); });
  $("architecture-close").addEventListener("click", () => { architectureVideo.pause(); architectureDialog.close(); previousFocus?.focus(); });
  $("architecture-zoom").addEventListener("click", () => {
    architectureDialog.classList.toggle("architecture-zoomed");
    $("architecture-zoom").textContent = architectureDialog.classList.contains("architecture-zoomed") ? "Fit image" : "Zoom image";
  });
  architectureDialog.addEventListener("close", () => architectureVideo.pause());
  $("watch-overview").addEventListener("click", () => {
    if (!demo.overview?.video || !core.assetPath(demo.overview.video)) return;
    if (!overviewVideo.getAttribute("src")) {
      overviewVideo.src = demo.overview.video;
      if (demo.overview.image) overviewVideo.poster = demo.overview.image;
    }
    show($("overview-video-panel"));
    overviewVideo.play().catch(() => { /* The viewer can use the native Play control. */ });
    overviewVideo.focus({ preventScroll: true });
    $("overview-video-panel").scrollIntoView({ block: "nearest", behavior: "instant" });
  });
  $("overview-video-close").addEventListener("click", () => {
    overviewVideo.pause();
    show($("overview-video-panel"), false);
    $("watch-overview").focus({ preventScroll: true });
  });
  overviewVideo.addEventListener("error", () => show($("overview-video-error")));
  $("dialog-close").addEventListener("click", closeImage);
  $("dialog-size").addEventListener("click", () => {
    dialog.classList.toggle("original-size");
    $("dialog-size").textContent = dialog.classList.contains("original-size") ? "Fit to window" : "Original size";
    syncDialogLayout();
  });
  $("dialog-image").addEventListener("load", syncDialogLayout);
  window.addEventListener("resize", syncDialogLayout);
  if (window.ResizeObserver) new ResizeObserver(syncDialogLayout).observe($("dialog-image-area"));
  dialog.addEventListener("click", event => { if (event.target === dialog) closeImage(); });
  video.addEventListener("error", () => show($("video-error")));
  $("report-close").addEventListener("click", () => closeReport());
  $("report-continue").addEventListener("click", () => { closeReport(); navigate({ type: "NEXT" }); });
  $("report-page-back").addEventListener("click", () => { reportPage = Math.max(0, reportPage - 1); renderReportPage(); });
  $("report-page-next").addEventListener("click", () => { reportPage = Math.min(currentReport.pages.length - 1, reportPage + 1); renderReportPage(); });
  $("report-zoom").addEventListener("click", () => {
    reportDialog.classList.toggle("report-fit-width");
    $("report-zoom").textContent = reportDialog.classList.contains("report-fit-width") ? "Fit whole page" : "Fit width / read closely";
  });
  document.addEventListener("keydown", event => {
    if (architectureDialog.open && architectureMode === "click-through" && !$("architecture-mode-controls").hidden && !event.altKey && !event.ctrlKey && !event.metaKey && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
      event.preventDefault();
      architectureStep = core.architectureStageIndex(architectureStep, event.key === "ArrowRight" ? 1 : -1, demo.overview.architectureStages.length);
      renderArchitectureStage();
      return;
    }
    if (!demo || current.scenarioIndex < 0 || dialog.open || reportDialog.open || architectureDialog.open || event.altKey || event.ctrlKey || event.metaKey || event.target.closest("video,input,textarea,select,[contenteditable=true]")) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      navigate({ type: event.key === "ArrowRight" ? "NEXT" : "BACK" });
    }
  });
  window.addEventListener("hashchange", () => { if (demo) { pauseVideo(); current = initialState(); render(); } });
  loadDemo();
})();
