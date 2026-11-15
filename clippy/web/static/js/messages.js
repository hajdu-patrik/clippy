import { appendCards, bindCards, cardHtml } from "./cards.js";
import { api } from "./api.js";
import { iconFor } from "./attachments.js";
import { t, tx } from "./i18n.js";
import { noteEta, patchProgress, progressHtml, trackProgress, updateTitle } from "./progress.js";
import { state } from "./state.js";
import { $, esc, md, toast } from "./util.js";

const AVATAR = '<svg class="avatar" aria-hidden="true"><use href="#mark"/></svg>';
const NEAR_BOTTOM = 240;

function distanceFromBottom() {
  const thread = $("#thread");
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight;
}

export function scrollBottom(force = false, smooth = true) {
  if (!force && distanceFromBottom() > NEAR_BOTTOM) return;
  requestAnimationFrame(() => $("#thread").scrollTo({ top: $("#thread").scrollHeight, behavior: smooth ? "smooth" : "auto" }));
}

export function bindScroll() {
  const button = $("#toBottom");
  $("#thread").addEventListener("scroll", () => { button.hidden = distanceFromBottom() < NEAR_BOTTOM * 1.5; }, { passive: true });
  button.addEventListener("click", () => scrollBottom(true));
}

function attachmentsHtml(list) {
  if (!list?.length) return "";
  return `<div class="sent-attachments" role="list">${list.map((a) => `<span class="sent-attachment" role="listitem">
    ${a.kind === "image" ? `<img src="${esc(a.url)}" alt="">` : iconFor(a.kind)}<span>${esc(a.name)}</span></span>`).join("")}</div>`;
}

const SIGN = { added: "+", changed: "~", removed: "−" };

function changeText(c) {
  return t(`change.${c.action}`, { what: t(`what.${c.subject}`, c.params || {}) });
}

function changesHtml(m) {
  const d = m.data || {};
  const items = (d.changes || []).map((c) => `<li class="${esc(c.action)}"><span class="sign" aria-hidden="true">${SIGN[c.action] || "•"}</span>${esc(changeText(c))}</li>`).join("");
  return `<div class="changes">
    <div class="changes-head"><svg aria-hidden="true"><use href="#i-edit"/></svg>
      <b>${esc(t("changes.title", { n: d.number, v: d.version }))}</b>
      <button type="button" class="undo" data-number="${Number(d.number)}">
        <svg aria-hidden="true"><use href="#i-undo"/></svg>${esc(t("changes.undo"))}</button></div>
    ${m.content ? `<div class="text">${md(m.content)}</div>` : ""}
    <ul>${items}</ul>
  </div>`;
}

function renderMessage(m) {
  if (m.role === "user") return `<div class="bubble">${esc(m.content)}</div>${attachmentsHtml(m.data?.attachments)}`;
  let body;
  if (m.kind === "progress" || m.kind === "error") body = progressHtml(m);
  else if (m.kind === "clips") body = `<div class="clips">${(m.data?.clip_ids || []).map(cardHtml).join("")}</div>`;
  else if (m.kind === "changes") body = changesHtml(m);
  else body = `<div class="text">${md(m.data?.t ? tx(m.data.t, m.content) : m.content)}</div>`;
  return `${AVATAR}<div class="body">${body}</div>`;
}

async function onUndo(event) {
  const button = event.target.closest("button.undo");
  if (!button || button.disabled || !state.current) return;
  button.disabled = true;
  try {
    await api.undo(state.current, Number(button.dataset.number));
    toast(t("changes.undoing"));
  } catch (err) {
    toast(t("composer.error", { error: tx(err.t, err.message) }));
    button.disabled = false;
  }
}

function slotFor(m) {
  const el = document.createElement("div");
  el.id = `m${m.id}`;
  const list = $("#messages");
  const after = [...list.children].find((x) => /^m\d+$/.test(x.id) && Number(x.id.slice(1)) > m.id)
    || document.getElementById("typing");
  list.insertBefore(el, after || null);
  return el;
}

function announceFinish(previous, m) {
  const wasLive = previous && !previous.data?.done;
  if (!wasLive || !m.data?.done) return;
  const ok = !(m.kind === "error" || m.data?.error);
  toast(t(ok ? "toast.ready" : "toast.failed"));
  if (document.hidden) document.title = `${t(ok ? "title.ready" : "title.error")} · ${document.title.replace(/^\d+% · [^·]+ · /, "")}`;
}

export function upsertMessage(m) {
  const previous = state.messages.get(m.id);
  state.messages.set(m.id, m);
  let el = document.getElementById(`m${m.id}`);
  if (!el) {
    if (m.role === "user") document.getElementById("pending-user")?.remove();
    el = slotFor(m);
  }
  const grid = el.querySelector(".clips");
  const isProgress = m.kind === "progress" || m.kind === "error";
  if (previous?.kind === "clips" && m.kind === "clips" && grid) {
    appendCards(grid, m.data?.clip_ids || []);
  } else if (!(isProgress && previous && patchProgress(m, previous))) {
    el.className = `msg ${m.role}`;
    el.innerHTML = renderMessage(m);
    if (isProgress) patchProgress(m, m);
  }
  bindCards(el);
  if (m.kind === "changes" && !el.dataset.undoBound) {
    el.dataset.undoBound = "1";
    el.addEventListener("click", onUndo);
  }
  if (isProgress) {
    announceFinish(previous, m);
    trackProgress(m);
  }
}

export function updateProgress(id, data) {
  const m = state.messages.get(id);
  if (!m) return;
  const next = { ...m, data };
  upsertMessage(next);
  noteEta(next);
  updateTitle();
}

export function showPending(text, attachments = []) {
  const user = document.createElement("div");
  user.className = "msg user";
  user.id = "pending-user";
  user.innerHTML = `<div class="bubble">${esc(text)}</div>${attachmentsHtml(attachments)}`;
  const typing = document.createElement("div");
  typing.className = "msg assistant";
  typing.id = "typing";
  typing.innerHTML = `${AVATAR}<div class="body"><span class="thinking" role="status"><span class="typing" aria-hidden="true">`
    + `<i></i><i></i><i></i></span><span class="think-text">${esc(t("thread.thinking"))}</span></span></div>`;
  $("#messages").append(user, typing);
  const started = Date.now();
  const label = typing.querySelector(".think-text");
  const timer = setInterval(() => {
    if (!typing.isConnected) { clearInterval(timer); return; }
    const s = Math.round((Date.now() - started) / 1000);
    label.textContent = t(s < 4 ? "thread.thinking" : s < 20 ? "thread.thinkingSec" : "thread.thinkingLong", { s });
  }, 1000);
}

export function clearPending() {
  document.getElementById("typing")?.remove();
  document.getElementById("pending-user")?.remove();
}
