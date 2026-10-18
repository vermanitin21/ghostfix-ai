"use strict";
// Renders the adapter's view model only. Never decides outcomes and never uses innerHTML.

const ICONS = { pass: "\u2713", fail: "\u2715", model: "\u25C6", evidence: "\u25A3", neutral: "\u2022" };
const STEP_MS = 2200;
const STEPS = 9;
const SCROLL_TARGETS = { 1: "card-repro", 2: "card-diagnosis", 3: "card-c1", 4: "c1-verdict", 5: "card-evidence",
  6: "card-correction", 7: "card-c2", 8: "c2-verdict", 9: "card-proof" };
const STAGE_STEPS = { reproduce: 1, diagnose: 2, attempt1: 4, evidence: 5, correct: 6, attempt2: 8, proof: 9 };
const REQUIRED = ["mode", "mode_label", "run_id", "stages", "candidates", "proof", "benchmarks", "correction"];

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  el.append(...nodes(children));
  return el;
}

// Flatten and drop empty values; strings become text nodes (never parsed as HTML).
function nodes(children) {
  return children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false)
    .map((child) => (child instanceof Node ? child : document.createTextNode(String(child))));
}

const badge = (tone, text, extra = "") => h("span", { class: `badge ${tone} ${extra}`.trim() }, `${ICONS[tone]} ${text}`);
const replace = (el, ...children) => { el.replaceChildren(...nodes(children)); return el; };
const fill = (id, ...children) => replace(document.getElementById(id), ...children);

function diffBlock(lines, label) {
  return h("pre", { "aria-label": label }, lines.map((line) => h("span", { class: `ln ${line.kind}` }, line.text)));
}

function termBlock(lines, label) {
  return h("pre", { class: "term", "aria-label": label },
    lines.map((line) => h("span", { class: `ln ${/^\s*(E\s|FAILED)/.test(line) ? "err" : "muted"}` }, line)));
}

function sourceBlock(source, changed) {
  return h("pre", { "aria-label": "Original source" }, source.replace(/\n$/, "").split("\n").map((line) =>
    h("span", { class: `ln ${changed.has(line) ? "del" : ""}`.trim() }, line)));
}

function testRow(label, test) {
  if (!test) return h("div", { class: "verdict-row" }, h("span", { class: "label" }, label), badge("neutral", "Not run"));
  const tone = test.result === "PASS" ? "pass" : "fail";
  return h("div", { class: "verdict-row" },
    h("span", { class: "label" }, label),
    h("span", { class: "count" }, test.counts_text || "count unavailable", ` \u00B7 exit ${test.exit_code}`),
    badge(tone, test.result === "PASS" ? "Passed" : "Failed"));
}

function candidateCard(c, verdictId, verdictStep) {
  const injected = c.provenance === "DEMO-INJECTED CANDIDATE";
  const card = document.getElementById(`card-c${c.attempt}`);
  card.classList.add(c.tone);
  const verdict = h("div", { id: verdictId, class: `verdict ${c.tone}`, dataset: { step: String(verdictStep) } },
    testRow("Targeted verification", c.targeted),
    c.regression || c.attempt > 1 ? testRow("Regression verification", c.regression) : null,
    h("div", { class: "verdict-row" }, h("span", { class: "label" }, "Candidate status"), badge(c.tone, c.outcome, "big")),
    c.outcome === "FAILED" ? h("div", { class: "sub" }, "Execution disproved this candidate, so GhostFix rejected it.") : null);
  replace(card,
    h("div", { class: "card-head" }, h("h3", {}, `Attempt ${c.attempt}`),
      injected ? badge("neutral", c.provenance) : badge("model", c.provenance)),
    h("p", { class: "origin" }, c.origin),
    c.applied_to_original === null ? null :
      h("p", { class: "sub" }, `Applied to a fresh copy of the ORIGINAL source: ${c.applied_to_original ? "yes" : "not confirmed"}`),
    diffBlock(c.diff, `Candidate ${c.attempt} patch`),
    verdict,
    c.protocol_note ? h("p", { class: "sub", style: null }, c.protocol_note) : null);
}

function render(view) {
  const verified = view.proof.verified === true;
  fill("issue-file", view.issue_file);
  fill("issue-target", view.target_test || "unavailable");
  fill("issue-status", view.reproduced ? badge("fail", "Failure reproduced") : badge("neutral", "Not reproduced"));
  const hero = document.getElementById("hero-proof");
  hero.classList.toggle("not-verified", !verified);
  fill("hero-status", `${verified ? ICONS.pass + " " : ICONS.fail + " "}${view.proof.display_status}`);
  fill("hero-run", `Recorded run ${view.run_id}`);

  fill("stages", ...view.stages.map((s) => h("li", { class: `stage ${s.tone}`, dataset: { reveal: String(STAGE_STEPS[s.key] || 0) } },
    h("div", { class: "num" }, String(s.step).padStart(2, "0")), h("div", { class: "title" }, s.title), badge(s.tone, s.status))));

  const finalDiff = view.candidates.length ? view.candidates[view.candidates.length - 1].diff : [];
  const changed = new Set(finalDiff.filter((l) => l.kind === "del").map((l) => l.text.slice(1)));
  fill("card-repro",
    h("div", { class: "card-head" }, h("h3", {}, "Failure reproduced"), badge("evidence", "Executed evidence")),
    h("dl", { class: "kv" },
      h("dt", {}, "Target test"), h("dd", { class: "mono" }, view.target_test || "unavailable"),
      h("dt", {}, "pytest exit"), h("dd", { class: "big-num" }, String(view.reproduction_exit_code)),
      h("dt", {}, "Status"), h("dd", {}, view.reproduced ? badge("fail", "Fail reproduced") : badge("neutral", "Not reproduced")),
      view.reproduction_observed ? [h("dt", {}, "Observed"), h("dd", { class: "mono" }, view.reproduction_observed)] : null),
    h("p", { class: "sub", style: null }, `Original ${view.issue_file} (highlighted: line changed by the final candidate)`),
    sourceBlock(view.buggy_source, changed));

  fill("card-diagnosis",
    h("div", { class: "card-head" }, h("h3", {}, "Root cause"), badge("model", "Model hypothesis")),
    h("p", { class: "quote" }, view.diagnosis_hypothesis || "Diagnosis unavailable."),
    h("dl", { class: "kv" }, h("dt", {}, "Model"), h("dd", { class: "mono" }, view.diagnosis_model || "unavailable")),
    h("p", { class: "sub" }, "A hypothesis guides the repair. It is not proof; only executed tests decide."));

  view.candidates.forEach((c) => candidateCard(c, `c${c.attempt}-verdict`, c.attempt === 1 ? 4 : 8));
  if (view.candidates.length < 2) fill("card-c2", h("p", { class: "sub" }, "No second candidate."));

  const ev = view.evidence;
  fill("card-evidence", ...(ev ? [
    h("div", { class: "card-head" }, h("h3", {}, "Execution evidence"), badge("evidence", "Executed evidence")),
    h("dl", { class: "kv" },
      h("dt", {}, "Target"), h("dd", { class: "mono" }, ev.target),
      h("dt", {}, "Exit"), h("dd", { class: "big-num" }, String(ev.exit_code)),
      ev.observed !== null ? [h("dt", {}, "Observed"), h("dd", { class: "mono" }, ev.expression ? `${ev.expression} returned ${ev.observed}` : ev.observed)] : null,
      ev.expected !== null ? [h("dt", {}, "Expected"), h("dd", { class: "mono" }, ev.expected)] : null,
      h("dt", {}, "Status"), h("dd", {}, badge("fail", ev.status))),
    ev.key_lines.length ? h("div", { style: null }, h("p", { class: "sub" }, "Bounded pytest output"), termBlock(ev.key_lines, "pytest output")) : null,
    h("p", { class: "supplied" }, ev.supplied_to_correction
      ? "This executed failure evidence was supplied to the correction step."
      : "This evidence was not supplied to a correction step."),
  ] : [h("p", { class: "sub" }, "No failed execution evidence in this run.")]));

  const cr = view.correction;
  fill("card-correction",
    h("div", { class: "card-head" }, h("h3", {}, "Autonomous correction"), badge(cr.occurred ? "model" : "neutral", cr.occurred ? "Model call" : "Not performed")),
    cr.occurred ? [
      h("dl", { class: "kv" }, h("dt", {}, "Model"), h("dd", { class: "mono" }, cr.model || "unavailable")),
      h("p", { class: "sub" }, "Correction request included:"),
      h("ul", { class: "inputs" }, cr.inputs.map((item) => h("li", {}, item))),
      h("p", { class: "sub" }, "No human patch editing between attempts."),
      h("details", {}, h("summary", {}, "Correction request SHA-256"), h("pre", { class: "term" }, h("span", { class: "ln muted" }, cr.request_sha256 || "unavailable"))),
    ] : null);

  const proof = document.getElementById("card-proof");
  proof.classList.toggle("not-verified", !verified);
  replace(proof,
    h("div", {},
      h("div", { class: "eyebrow", id: "proof-title" }, "Proof of Fix"),
      h("div", { class: "proof-status" }, `${verified ? ICONS.pass : ICONS.fail} ${view.proof.display_status}`),
      h("div", { class: "hero-run mono" }, `Run ${view.proof.run_id}`),
      h("div", { class: "hero-run mono" }, `Generated ${view.proof.generated_at.replace("T", " ").slice(0, 19)} UTC`)),
    h("div", {},
      h("ul", { class: "checklist" }, view.proof.checklist.map((item) => h("li", {},
        h("span", { class: `check ${item.ok ? "ok" : "no"}`, "aria-hidden": "true" }, item.ok ? ICONS.pass : ICONS.fail),
        h("span", {}, `${item.ok ? "" : "Not met: "}${item.label}`)))),
      view.proof.evidence_issues.length ? termBlock(view.proof.evidence_issues, "Evidence issues") : null));

  fill("bench", ...view.benchmarks.map((b) => h("div", { class: "bench-row" },
    h("span", { class: "bench-ver" }, b.version),
    h("div", { class: "bar", role: "img", "aria-label": `${b.version}: ${b.verified} of ${b.total} verified` },
      h("span", { style: null, dataset: { pct: b.percent } })),
    h("span", { class: "bench-val" }, b.percent, h("small", {}, `${b.verified} / ${b.total} verified`)))));
  document.querySelectorAll(".bar span").forEach((bar) => { bar.style.width = bar.dataset.pct; });

  const local = { match: "hash-matched to local canonical artifacts", mismatch: "DOES NOT MATCH local canonical artifacts",
    unavailable: "local canonical artifacts not present (bundle only)" }[view.local_canonical];
  fill("provenance",
    h("span", {}, `Source: recorded replay bundle for run ${view.run_id}`), h("span", {}, `Provenance: ${local}`),
    h("span", {}, "Replay makes no model, network or Docker calls. Candidate 1 was deliberately demo-injected."));
}

// Replay: reveals already-rendered recorded evidence in order. It never changes content.
const replay = { step: STEPS, timer: null, playing: false, view: null };
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function setMode() {
  const mode = document.getElementById("mode");
  if (document.body.classList.contains("replaying")) {
    mode.className = "mode replaying";
    mode.textContent = replay.step >= STEPS ? "REPLAY COMPLETE \u00B7 RECORDED EVIDENCE"
      : `REPLAYING RECORDED EVIDENCE \u00B7 STEP ${Math.max(replay.step, 1)}/${STEPS}${replay.playing ? "" : " \u00B7 PAUSED"}`;
  } else {
    mode.className = `mode ${replay.view.proof.verified ? "recorded" : "unverified"}`;
    mode.textContent = `${replay.view.mode_label} \u00B7 ${replay.view.run_id.slice(0, 8)}`;
  }
}

function showStep(n, scroll) {
  replay.step = n;
  document.querySelectorAll("[data-step]").forEach((el) => {
    const shown = Number(el.dataset.step) <= n;
    el.classList.toggle("revealed", shown);
    el.classList.toggle("pending", !shown);
  });
  document.querySelectorAll(".stage").forEach((el) => {
    const at = Number(el.dataset.reveal);
    el.classList.toggle("pending", at > n);
    el.classList.toggle("current", at === n);
  });
  const target = document.getElementById(SCROLL_TARGETS[n]);
  if (scroll && target) target.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
  setMode();
}

function tick() {
  if (replay.step >= STEPS) { stop(); setMode(); return; }
  showStep(replay.step + 1, true);
  replay.timer = setTimeout(tick, STEP_MS);
}

function stop() {
  clearTimeout(replay.timer);
  replay.timer = null;
  replay.playing = false;
  document.getElementById("pause").textContent = "Pause";
  document.getElementById("pause").disabled = replay.step >= STEPS;
}

function startReplay() {
  stop();
  document.body.classList.add("replaying");
  showStep(0, false);
  window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
  replay.playing = true;
  document.getElementById("pause").disabled = false;
  document.getElementById("reset").disabled = false;
  replay.timer = setTimeout(tick, 700);
  setMode();
}

function togglePause() {
  if (replay.playing) { stop(); document.getElementById("pause").disabled = false; document.getElementById("pause").textContent = "Resume"; }
  else if (replay.step < STEPS) { replay.playing = true; document.getElementById("pause").textContent = "Pause"; replay.timer = setTimeout(tick, 400); }
  setMode();
}

function resetReplay() {
  stop();
  document.body.classList.remove("replaying");
  document.querySelectorAll(".pending, .revealed, .current").forEach((el) => el.classList.remove("pending", "revealed", "current"));
  replay.step = STEPS;
  document.getElementById("pause").disabled = true;
  document.getElementById("reset").disabled = true;
  setMode();
}

function unavailable() {
  document.getElementById("app").hidden = true;
  document.getElementById("unavailable").hidden = false;
  const mode = document.getElementById("mode");
  mode.className = "mode unverified";
  mode.textContent = "EVIDENCE UNAVAILABLE";
}

async function main() {
  let view;
  try {
    const response = await fetch("/api/replay", { cache: "no-store" });
    view = await response.json();
    if (!response.ok || view.mode !== "RECORDED" || REQUIRED.some((key) => !(key in view))) throw new Error("invalid");
    render(view);
  } catch (error) {
    unavailable();
    return;
  }
  replay.view = view;
  document.getElementById("app").hidden = false;
  document.getElementById("replay").disabled = false;
  document.getElementById("replay").addEventListener("click", startReplay);
  document.getElementById("pause").addEventListener("click", togglePause);
  document.getElementById("reset").addEventListener("click", resetReplay);
  setMode();
}

main();
