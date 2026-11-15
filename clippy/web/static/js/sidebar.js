import { api } from "./api.js";
import { currentChat, state } from "./state.js";
import { closeDrawer } from "./drawer.js";
import { chatTitle, t, tx } from "./i18n.js";
import { fmtEta, stageName, updateTitle } from "./progress.js";
import { $, toast } from "./util.js";

export function setHeader() {
  const chat = currentChat();
  $("#chatTitle").textContent = chat ? chatTitle(chat.title) : "Clippy";
  $("#chatSub").textContent = chat?.video_title ? t("header.video", { title: chat.video_title }) : (chat?.source || "");
  updateTitle();
}

export async function loadChats() {
  state.chats = await api.chats();
  renderSidebar();
  setHeader();
}

function busyChats() {
  const ids = new Set((state.status?.queue || []).map((q) => q.chat_id));
  for (const job of state.status?.running || []) ids.add(job.chat_id);
  if (state.status?.current) ids.add(state.status.current.chat_id);
  return ids;
}

function chatItem(chat, busy) {
  const el = document.createElement("div");
  el.className = "chat-item" + (chat.id === state.current ? " active" : "");
  el.setAttribute("role", "listitem");
  if (busy) {
    const dot = document.createElement("span");
    dot.className = "running";
    dot.title = t("side.running");
    el.append(dot);
  }
  const title = document.createElement("a");
  title.className = "t";
  title.href = `#/chat/${chat.id}`;
  title.textContent = chatTitle(chat.title);
  const actions = document.createElement("span");
  actions.className = "actions";
  for (const [act, label, icon] of [["rename", t("side.rename"), "#i-edit"], ["delete", t("side.delete"), "#i-trash"]]) {
    const btn = document.createElement("button");
    btn.className = "icon-btn";
    btn.dataset.act = act;
    btn.title = label;
    btn.setAttribute("aria-label", `${label}: ${chatTitle(chat.title)}`);
    btn.innerHTML = `<svg aria-hidden="true"><use href="${icon}"/></svg>`;
    actions.append(btn);
  }
  el.append(title, actions);
  el.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    if (act === "rename") renameChat(chat, el);
    else if (act === "delete") deleteChat(chat);
    else closeDrawer();
  });
  el.addEventListener("dblclick", () => renameChat(chat, el));
  return el;
}

let renderedBusy = "";

export function renderSidebar() {
  const list = $("#chatList");
  const busy = busyChats();
  renderedBusy = [...busy].sort().join(",");
  list.replaceChildren(...state.chats.map((chat) => chatItem(chat, busy.has(chat.id))));
  if (!state.chats.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = t("side.empty");
    list.append(empty);
  }
}

function renameChat(chat, el) {
  const input = document.createElement("input");
  input.value = chat.title;
  input.maxLength = 120;
  input.setAttribute("aria-label", t("side.chatTitle"));
  el.querySelector(".t").replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (save && title && title !== chat.title) {
      try { await api.renameChat(chat.id, title); } catch (err) { toast(t("side.renameFailed", { error: err.message })); }
    }
    await loadChats();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
}

async function deleteChat(chat) {
  const ok = confirm(t("side.confirmDelete", { title: chatTitle(chat.title) }));
  if (!ok) return;
  try { await api.deleteChat(chat.id); } catch (err) { toast(t("side.deleteFailed", { error: err.message })); return; }
  if (state.current === chat.id) location.hash = "";
  await loadChats();
}

export function setWorker(status) {
  state.status = status;
  const current = status?.current;
  const queued = status?.queue?.length || 0;
  const parallel = Math.max(0, (status?.running?.length || 0) - 1);
  const more = (parallel ? t("worker.parallel", { n: parallel }) : "") + (queued ? t("worker.more", { n: queued }) : "");
  if (current?.percent != null) {
    const stage = stageName(current.stage_key, current.stage) || t("worker.working");
    showWorker(true, `${stage} ${Math.round(current.percent)}% · ${fmtEta(current.eta)}${more}`, current.percent);
  } else {
    const label = status?.busy ? tx(current?.label_t, current?.label || t("worker.working")) + more
      : (queued ? t("worker.queued", { n: queued }) : t("worker.idle"));
    showWorker(Boolean(status?.busy), label, 0);
  }
  if ([...busyChats()].sort().join(",") !== renderedBusy && !$("#chatList input")) renderSidebar();
}

export function showWorker(busy, label, percent) {
  const text = $("#workerText");
  $("#worker").classList.toggle("busy", busy);
  text.textContent = label;
  text.title = label;
  $("#workerBar").style.transform = `scaleX(${Math.max(2, percent) / 100})`;
}
