/* Shared, dependency-free state and asset validation. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WarehouseDemo = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const STEP_IDS = ["createRule", "activeRule", "alert", "play", "report", "followup"];
  const array = value => Array.isArray(value) ? value : [];

  function normalizedRect(value) {
    if (!value || typeof value !== "object") return false;
    const values = [value.x, value.y, value.width, value.height];
    return values.every(Number.isFinite) && value.x >= 0 && value.y >= 0 &&
      value.width > 0 && value.height > 0 && value.x + value.width <= 1.000001 &&
      value.y + value.height <= 1.000001;
  }

  function assetPath(value) {
    if (typeof value !== "string" || !value.trim() || value !== value.trim()) return false;
    let path;
    try { path = decodeURIComponent(value); } catch (_) { return false; }
    if (/^[/.\\]|[\\%?#\u0000-\u001f\u007f]/.test(path) || /^[a-z][a-z0-9+.-]*:/i.test(path)) return false;
    if (path.includes(":")) return false;
    return path.split("/").every(part => part && part !== "." && part !== "..");
  }

  function listAssetPaths(demo) {
    const paths = [];
    function add(path) { if (typeof path === "string" && path) paths.push(path); }
    add(demo?.overview?.image);
    add(demo?.overview?.architecture);
    add(demo?.overview?.architectureAnimation);
    add(demo?.overview?.architecturePoster);
    add(demo?.overview?.video);
    for (const stage of array(demo?.overview?.architectureStages)) add(stage?.image);
    for (const scenario of array(demo?.scenarios)) {
      if (!scenario || typeof scenario !== "object") continue;
      add(scenario.coverImage);
      for (const step of array(scenario.steps)) add(step?.image);
      add(scenario.video?.src);
      add(scenario.video?.poster);
      for (const report of array(scenario.reports)) {
        add(report?.src);
        for (const page of array(report?.pages)) add(page?.src);
      }
      for (const followup of array(scenario.followups)) add(followup?.source?.src);
    }
    return [...new Set(paths)];
  }

  function validateDemo(demo) {
    const errors = [];
    if (!demo || typeof demo !== "object" || Array.isArray(demo)) return ["Manifest must be an object."];
    if (demo.schemaVersion !== 1) errors.push("schemaVersion must be 1.");
    if (demo.interactionMode !== undefined && demo.interactionMode !== "screen-only") errors.push("interactionMode must be screen-only when present.");
    if (demo.questionFlow !== undefined && demo.questionFlow !== "sequential") errors.push("questionFlow must be sequential when present.");
    if (typeof demo.title !== "string" || !demo.title.trim()) errors.push("A title is required.");
    function optionalPath(value, label) {
      if (value !== undefined && value !== null && value !== "" && !assetPath(value)) errors.push(`${label} must be a local relative asset path.`);
    }
    for (const key of ["image", "architecture", "architectureAnimation", "architecturePoster", "video"]) optionalPath(demo.overview?.[key], `overview.${key}`);
    if (demo.overview?.architectureStages !== undefined && (!Array.isArray(demo.overview.architectureStages) || !demo.overview.architectureStages.length)) errors.push("overview.architectureStages must be a nonempty array.");
    for (const stage of array(demo.overview?.architectureStages)) {
      if (!stage || typeof stage.title !== "string" || !stage.title.trim() || typeof stage.description !== "string" || !stage.description.trim() || !assetPath(stage.image)) errors.push("Each architecture stage needs a title, explanation and local image.");
    }
    if (!Array.isArray(demo.scenarios) || !demo.scenarios.length) errors.push("At least one captured scenario is required.");
    const ids = new Set();
    for (const [index, scenario] of (Array.isArray(demo.scenarios) ? demo.scenarios : []).entries()) {
      const prefix = `Scenario ${index + 1}`;
      if (!scenario || typeof scenario !== "object") { errors.push(`${prefix} must be an object.`); continue; }
      if (!/^[a-z0-9][a-z0-9-]*$/.test(scenario.id || "")) errors.push(`${prefix} needs a lowercase, hyphenated id.`);
      if (ids.has(scenario.id)) errors.push(`${prefix} id must be unique.`);
      ids.add(scenario.id);
      if (typeof scenario.title !== "string" || !scenario.title.trim()) errors.push(`${prefix} needs a title.`);
      if (typeof scenario.camera !== "string" || !scenario.camera.trim()) errors.push(`${prefix} needs a camera.`);
      optionalPath(scenario.coverImage, `${prefix} coverImage`);
      optionalPath(scenario.video?.src, `${prefix} video.src`);
      optionalPath(scenario.video?.poster, `${prefix} video.poster`);
      if (!Array.isArray(scenario.steps) || !scenario.steps.length) errors.push(`${prefix} needs captured steps.`);
      const stepIds = new Set();
      for (const list of ["reports", "followups"]) if (scenario[list] !== undefined && !Array.isArray(scenario[list])) errors.push(`${prefix} ${list} must be an array.`);
      for (const step of array(scenario.steps)) {
        if (!step || typeof step !== "object") { errors.push(`${prefix} has an invalid step.`); continue; }
        if (typeof step.id !== "string" || !step.id) errors.push(`${prefix} has a step without id.`);
        if (stepIds.has(step.id)) errors.push(`${prefix} step ids must be unique.`);
        stepIds.add(step.id);
        if (typeof step.title !== "string" || !step.title.trim()) errors.push(`${prefix}/${step.id} needs a title.`);
        optionalPath(step.image, `${prefix}/${step.id} image`);
        if (step.videoBounds !== undefined && !normalizedRect(step.videoBounds)) errors.push(`${prefix}/${step.id} videoBounds must fit normalized image bounds.`);
        if (step.answerBounds !== undefined && !normalizedRect(step.answerBounds)) errors.push(`${prefix}/${step.id} answerBounds must fit normalized image bounds.`);
        if (step.questionPhase !== undefined && (!["prompt", "answer"].includes(step.questionPhase) || !Number.isInteger(step.questionIndex) || !scenario.followups?.[step.questionIndex])) errors.push(`${prefix}/${step.id} sequential questions need a phase and captured questionIndex.`);
        if (step.hotspots !== undefined && !Array.isArray(step.hotspots)) errors.push(`${prefix}/${step.id} hotspots must be an array.`);
        for (const hotspot of Array.isArray(step.hotspots) ? step.hotspots : []) {
          if (!hotspot || typeof hotspot !== "object") { errors.push(`${prefix}/${step.id} has an invalid hotspot.`); continue; }
          if (!normalizedRect(hotspot)) {
            errors.push(`${prefix}/${step.id} hotspot must fit normalized image bounds.`);
          }
          if (typeof hotspot.label !== "string" || !hotspot.label.trim()) errors.push(`${prefix}/${step.id} hotspot needs a label.`);
          if (!["next", "play", "report", "followup", "question"].includes(hotspot.action)) errors.push(`${prefix}/${step.id} has an unsupported hotspot action.`);
          if (hotspot.action === "question" && (!Number.isInteger(hotspot.questionIndex) || !scenario.followups?.[hotspot.questionIndex])) errors.push(`${prefix}/${step.id} question hotspot needs a captured questionIndex.`);
          if (hotspot.answerBounds !== undefined && !normalizedRect(hotspot.answerBounds)) errors.push(`${prefix}/${step.id} answerBounds must fit normalized image bounds.`);
          if (hotspot.reportType !== undefined && !["pdf", "markdown"].includes(hotspot.reportType)) errors.push(`${prefix}/${step.id} reportType must be pdf or markdown.`);
          if (hotspot.cue !== undefined && (typeof hotspot.cue !== "string" || !hotspot.cue.trim() || hotspot.cue.length > 40)) errors.push(`${prefix}/${step.id} cue must be concise text.`);
        }
      }
      for (const report of array(scenario.reports)) {
        if (!report || typeof report !== "object" || !assetPath(report.src)) errors.push(`${prefix} report needs a local relative src.`);
        if (report?.pages !== undefined && (!Array.isArray(report.pages) || !report.pages.length)) errors.push(`${prefix} PDF pages must be a nonempty array.`);
        for (const [pageIndex, page] of array(report?.pages).entries()) {
          if (!page || page.page !== pageIndex + 1 || !assetPath(page.src) || !Number.isInteger(page.width) || !Number.isInteger(page.height) || page.width <= 0 || page.height <= 0) errors.push(`${prefix} PDF pages need ordered page numbers, local images and native dimensions.`);
        }
      }
      for (const followup of array(scenario.followups)) {
        if (!followup || typeof followup.question !== "string" || !followup.question.trim()) errors.push(`${prefix} follow-up needs a question.`);
        if (followup?.answer && (typeof followup.answer !== "string" || !followup.source || !["captured-chat", "report"].includes(followup.source.kind) || typeof followup.source.label !== "string" || !followup.source.label.trim())) {
          errors.push(`${prefix} captured answers require a source kind and label.`);
        }
        optionalPath(followup?.source?.src, `${prefix} follow-up source`);
      }
    }
    for (const path of listAssetPaths(demo)) if (!assetPath(path)) errors.push(`Asset path must be local and relative: ${path}`);
    return errors;
  }

  function state(scenarioIndex = -1, stepIndex = 0, questionIndex = -1) {
    return { scenarioIndex, stepIndex, questionIndex };
  }

  function transition(current, action, demo) {
    const scenarios = demo.scenarios;
    if (action.type === "HOME") return state();
    if (action.type === "SCENARIO") {
      return Number.isInteger(action.index) && scenarios[action.index] ? state(action.index) : current;
    }
    const scenario = scenarios[current.scenarioIndex];
    if (!scenario) return current;
    const maximum = scenario.steps.length - 1;
    if (action.type === "NEXT") return state(current.scenarioIndex, Math.min(maximum, current.stepIndex + 1));
    if (action.type === "BACK") return state(current.scenarioIndex, Math.max(0, current.stepIndex - 1));
    if (action.type === "RESTART") return state(current.scenarioIndex);
    if (action.type === "STEP") {
      return Number.isInteger(action.index) && action.index >= 0 && action.index <= maximum ? state(current.scenarioIndex, action.index) : current;
    }
    if (action.type === "QUESTION") {
      return Number.isInteger(action.index) && scenario.followups?.[action.index] ? state(current.scenarioIndex, current.stepIndex, action.index) : current;
    }
    return current;
  }

  function architectureStageIndex(current, delta, count) {
    if (!Number.isInteger(count) || count < 1) return 0;
    const value = (Number.isInteger(current) ? current : 0) + (Number.isInteger(delta) ? delta : 0);
    return Math.max(0, Math.min(count - 1, value));
  }

  return { STEP_IDS, assetPath, normalizedRect, listAssetPaths, validateDemo, state, transition, architectureStageIndex };
});
