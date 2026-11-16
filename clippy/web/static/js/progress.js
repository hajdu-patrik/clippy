import { chatTitle, t, tx } from "./i18n.js";
import { currentChat, state } from "./state.js";
import { esc, fmtTs } from "./util.js";

const CIRCLE = '<svg class="ring-svg" viewBox="0 0 40 40" aria-hidden="true"><circle class="track" cx="20" cy="20" r="18"/>'
  + '<circle class="fill" cx="20" cy="20" r="18"/></svg>';
const ICON = { done: "#i-check", cached: "#i-check", failed: "#i-alert" };
const live = new Map();

function phaseOf(m) {
  const d = m.data || {};
  if (m.kind === "error" || d.error) return "error";
  if (d.done) return "done";
  return d.phase === "queued" ? "queued" : "running";
}

function percentOf(m) {
  const d = m.data || {};
  if (d.done) return 100;
  return Math.max(0, Math.min(100, Number(d.percent) || 0));
}

export function fmtEta(seconds) {
  if (seconds == null) return t("progress.estimating");
  const s = Math.max(0, seconds);
  if (s < 45) return t("progress.almost");
  const minutes = Math.round(s / 60);
  if (minutes < 60) return t("progress.minLeft", { m: minutes });
  return t("progress.hLeft", { h: Math.floor(minutes / 60), m: minutes % 60 });
}

function ordinal(n) {
  const suffix = n % 10 === 1 && n % 100 !== 11 ? "st" : n % 10 === 2 && n % 100 !== 12 ? "nd"
    : n % 10 === 3 && n % 100 !== 13 ? "rd" : "th";
  return `${n}${suffix}`;
}

export const stageName = (key, fallback) => (key ? t(`stage.${key}`) : fallback);
const stateText = (state) => t(`state.${state}`);

function activeStage(d) {
  const active = (d.stages || []).find((s) => s.state === "active");
  return active ? stageName(active.key) : d.stage;
}

const labelText = (m) => tx(m.data?.label_t, m.content);
const summaryText = (m) => tx(m.data?.summary_t, m.content);

function detailText(d) {
  const lines = d.lines || [];
  return tx(d.detail_t, d.detail || lines[lines.length - 1] || "");
}

function stepsHtml(stages) {
  if (!stages?.length) return "";
  return `<ol class="steps" aria-label="${esc(t("progress.steps"))}">${stages.map((s) => {
    const icon = ICON[s.state] ? `<svg aria-hidden="true"><use href="${ICON[s.state]}"/></svg>` : "";
    const name = stageName(s.key, s.label);
    return `<li class="step ${s.state}" data-key="${esc(s.key)}"
      title="${esc(name)}: ${esc(stateText(s.state))}"><div class="seg"><i></i></div>
      <div class="name">${icon}<span>${esc(name)}</span></div></li>`;
  }).join("")}</ol>`;
}

function headline(m, phase) {
  const d = m.data || {};
  if (phase === "error") return t("progress.failed");
  if (phase === "done") return t("progress.done");
  if (phase === "queued") {
    return d.queue_position ? t("progress.waitingAt", { pos: ordinal(d.queue_position) }) : t("progress.waiting");
  }
  return activeStage(d) || t("progress.starting");
}

export function progressHtml(m) {
  const d = m.data || {};
  const phase = phaseOf(m);
  const p = percentOf(m);
  const lines = d.lines || [];
  const finished = phase === "done" || phase === "error";
  const unknown = !finished && phase === "running" && d.percent == null;
  const number = phase === "queued" ? (d.queue_position ? `#${d.queue_position}` : "…") : unknown ? "" : `${Math.round(p)}%`;
  const glyph = finished ? `<svg class="glyph" aria-hidden="true"><use href="${phase === "done" ? "#i-check" : "#i-alert"}"/></svg>`
    : `<span class="num">${number}</span>`;
  return `<div class="progress ${phase}${unknown ? " queued" : ""}" data-mid="${m.id}">
    <div class="head">
      <div class="ring" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p)}"
        aria-label="${esc(headline(m, phase))}">${CIRCLE}${glyph}</div>
      <div class="titles">
        <div class="stage"><b>${esc(headline(m, phase))}</b><span class="label">${finished ? "" : esc(labelText(m))}</span></div>
        <div class="detail">${esc(finished ? "" : detailText(d))}</div>
      </div>
      <div class="times"><div class="eta">${finished ? "" : esc(phase === "queued" ? t("progress.afterCurrent") : fmtEta(d.eta))}</div>
        <div class="elapsed">${elapsedText(m)}</div></div>
    </div>
    ${phase === "queued" ? "" : `<div class="overall"><i></i></div>`}
    ${stepsHtml(d.stages)}
    ${finished ? `<div class="summary">${esc(summaryText(m))}</div>` : ""}
    ${lines.length ? `<details><summary>${esc(t("progress.log"))}</summary><pre>${esc(lines.join("\n"))}</pre></details>` : ""}
  </div>`;
}

function elapsedText(m) {
  const d = m.data || {};
  if (!d.started) return "";
  const seconds = (d.finished || Date.now() / 1000) - d.started;
  return t(d.finished ? "progress.took" : "progress.elapsed", { t: fmtTs(seconds) });
}

function tweenNumber(box, from, to) {
  const num = box.querySelector(".num");
  if (!num) return;
  const t0 = performance.now(), span = 600;
  const step = (t) => {
    const k = Math.min(1, (t - t0) / span);
    const eased = 1 - (1 - k) ** 3;
    num.textContent = `${Math.round(from + (to - from) * eased)}%`;
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export function patchProgress(m, previous) {
  const box = document.querySelector(`.progress[data-mid="${m.id}"]`);
  if (!box) return false;
  const phase = phaseOf(m);
  const oldPhase = previous ? phaseOf(previous) : phase;
  const structural = phase !== oldPhase || (m.data?.stages?.length || 0) !== (previous?.data?.stages?.length || 0)
    || !box.querySelector(".steps") !== !(m.data?.stages?.length);
  if (structural) return false;
  const d = m.data || {};
  const p = percentOf(m);
  const before = percentOf(previous || m);
  box.style.setProperty("--p", p);
  const ring = box.querySelector(".ring");
  ring.setAttribute("aria-valuenow", String(Math.round(p)));
  ring.setAttribute("aria-label", headline(m, phase));
  box.querySelector(".stage b").textContent = headline(m, phase);
  if (phase === "queued") {
    const num = box.querySelector(".num");
    if (num) num.textContent = d.queue_position ? `#${d.queue_position}` : "…";
  } else if (d.percent != null && Math.round(p) !== Math.round(before)) {
    tweenNumber(box, before, p);
  }
  const lines = d.lines || [];
  const finished = phase === "done" || phase === "error";
  box.querySelector(".detail").textContent = finished ? "" : detailText(d);
  for (const s of d.stages || []) {
    const li = box.querySelector(`.step[data-key="${CSS.escape(s.key)}"]`);
    if (!li) continue;
    if (!li.classList.contains(s.state)) {
      li.className = `step ${s.state}`;
      const icon = ICON[s.state] ? `<svg aria-hidden="true"><use href="${ICON[s.state]}"/></svg>` : "";
      li.querySelector(".name").innerHTML = `${icon}<span>${esc(stageName(s.key, s.label))}</span>`;
      li.title = `${stageName(s.key, s.label)}: ${stateText(s.state)}`;
    }
    li.style.setProperty("--f", Number(s.fraction) || 0);
  }
  let log = box.querySelector("details pre");
  if (!log && lines.length) {
    box.insertAdjacentHTML("beforeend", `<details><summary>${esc(t("progress.log"))}</summary><pre></pre></details>`);
    log = box.querySelector("details pre");
  }
  if (log) {
    const pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 8;
    log.textContent = lines.join("\n");
    if (pinned) log.scrollTop = log.scrollHeight;
  }
  return true;
}

export function trackProgress(m) {
  const phase = phaseOf(m);
  if (phase === "running" || phase === "queued") live.set(m.id, { etaAt: Date.now() / 1000, eta: m.data?.eta ?? null });
  else live.delete(m.id);
  updateTitle();
}

export function noteEta(m) {
  if (live.has(m.id)) live.set(m.id, { etaAt: Date.now() / 1000, eta: m.data?.eta ?? null });
}

export function clearLive() {
  live.clear();
}

function tick() {
  const now = Date.now() / 1000;
  for (const [id, info] of live) {
    const m = state.messages.get(id);
    const box = document.querySelector(`.progress[data-mid="${id}"]`);
    if (!m || !box) continue;
    const phase = phaseOf(m);
    const elapsed = box.querySelector(".elapsed");
    if (elapsed) elapsed.textContent = elapsedText(m);
    const eta = box.querySelector(".eta");
    if (eta && phase === "running") eta.textContent = fmtEta(info.eta == null ? null : info.eta - (now - info.etaAt));
  }
}

export function updateTitle() {
  const chat = currentChat();
  const base = chat ? `${chatTitle(chat.title)} · Clippy` : "Clippy";
  const running = [...live.keys()].map((id) => state.messages.get(id)).find((m) => m && phaseOf(m) === "running");
  const percent = running?.data?.percent != null ? `${Math.round(percentOf(running))}% · ` : "";
  document.title = running ? `${percent}${activeStage(running.data) || t("worker.working")} · ${base}` : base;
}

setInterval(tick, 1000);
