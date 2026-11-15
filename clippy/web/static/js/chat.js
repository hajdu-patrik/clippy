import { api } from "./api.js";
import { cardHtml, markDeleted, markOldVersions } from "./cards.js";
import { t } from "./i18n.js";
import { onLive } from "./live.js";
import { hideTyping, scrollBottom, showTyping, updateProgress, upsertMessage } from "./messages.js";
import { clearLive, fmtEta, stageName } from "./progress.js";
import { loadChats, renderSidebar, setHeader, setWorker, showWorker } from "./sidebar.js";
import { state } from "./state.js";
import { $, toast } from "./util.js";

export function showEmpty(empty) {
  $("#empty").hidden = !empty;
}

const OPEN_CHAT_EVENTS = new Set(["message", "progress", "clip", "deleted", "thinking"]);

function upsertChat(chat) {
  const i = state.chats.findIndex((c) => c.id === chat.id);
  if (i >= 0) state.chats[i] = chat;
  else state.chats.unshift(chat);
  setHeader();
  if (!$("#chatList input")) renderSidebar();
}

async function resync() {
  await loadChats().catch(() => {});
  const id = state.current;
  if (!id) return;
  let data;
  try { data = await api.chat(id); } catch { return; }
  if (state.current !== id) return;
  for (const [clipId, clip] of Object.entries(data.clips)) state.clips.set(Number(clipId), clip);
  data.messages.forEach(upsertMessage);
  showEmpty(!data.messages.length && !state.sending);
}

const HANDLERS = {
  hello: (ev) => { setWorker(ev.status); resync(); },
  message: (ev) => { upsertMessage(ev.message); showEmpty(false); scrollBottom(); },
  thinking: (ev) => {
    if (state.sending) return;
    if (ev.on) { showTyping(); scrollBottom(); } else hideTyping();
  },
  progress: (ev) => {
    updateProgress(ev.message_id, ev.data);
    if (ev.data.phase === "running") {
      const active = (ev.data.stages || []).find((s) => s.state === "active");
      const stage = active ? stageName(active.key) : ev.data.stage;
      showWorker(true, `${stage} ${Math.round(ev.data.percent)}% · ${fmtEta(ev.data.eta)}`, ev.data.percent);
    }
  },
  clip: (ev) => {
    state.clips.set(ev.clip.id, { ...ev.clip, current: true });
    markOldVersions(ev.clip);
    document.querySelectorAll(`.card[data-cid="${ev.clip.id}"]`).forEach((card) => { card.outerHTML = cardHtml(ev.clip.id); });
  },
  deleted: (ev) => markDeleted(ev.number),
  status: (ev) => setWorker(ev.status),
  chat: (ev) => upsertChat(ev.chat),
  chat_deleted: (ev) => {
    state.chats = state.chats.filter((c) => c.id !== ev.chat_id);
    renderSidebar();
    if (state.current === ev.chat_id) {
      location.hash = "";
      toast(t("live.chatDeleted"));
    }
  },
};

onLive((event) => {
  if (OPEN_CHAT_EVENTS.has(event.type) && event.chat_id !== state.current) return;
  HANDLERS[event.type]?.(event);
});

let opening = 0;

export async function openChat(id) {
  const ticket = ++opening;
  clearLive();
  state.current = id;
  state.messages.clear();
  state.clips.clear();
  $("#messages").replaceChildren();
  setHeader();
  if (!id) {
    showEmpty(true);
    renderSidebar();
    return;
  }
  let data;
  try {
    data = await api.chat(id);
  } catch {
    if (ticket === opening) location.hash = "";
    return;
  }
  if (ticket !== opening) return;
  for (const [clipId, clip] of Object.entries(data.clips)) state.clips.set(Number(clipId), clip);
  const list = $("#messages");
  list.classList.add("entering");
  data.messages.forEach(upsertMessage);
  setTimeout(() => list.classList.remove("entering"), 400);
  showEmpty(!data.messages.length);
  setWorker(data.status);
  scrollBottom(true, false);
}
