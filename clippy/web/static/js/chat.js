import { api } from "./api.js";
import { markDeleted, markOldVersions } from "./cards.js";
import { scrollBottom, updateProgress, upsertMessage } from "./messages.js";
import { clearLive, fmtEta, stageName } from "./progress.js";
import { renderSidebar, setHeader, setWorker, showWorker } from "./sidebar.js";
import { state } from "./state.js";
import { $ } from "./util.js";

export function showEmpty(empty) {
  $("#empty").hidden = !empty;
}

const HANDLERS = {
  message: (ev) => { upsertMessage(ev.message); showEmpty(false); scrollBottom(); },
  progress: (ev) => {
    updateProgress(ev.message_id, ev.data);
    if (ev.data.phase === "running") {
      const active = (ev.data.stages || []).find((s) => s.state === "active");
      const stage = active ? stageName(active.key) : ev.data.stage;
      showWorker(true, `${stage} ${Math.round(ev.data.percent)}% · ${fmtEta(ev.data.eta)}`, ev.data.percent);
    }
  },
  clip: (ev) => { state.clips.set(ev.clip.id, { ...ev.clip, current: true }); markOldVersions(ev.clip); },
  deleted: (ev) => markDeleted(ev.number),
  status: (ev) => setWorker(ev.status),
  chat: (ev) => {
    const i = state.chats.findIndex((c) => c.id === ev.chat.id);
    if (i >= 0) state.chats[i] = ev.chat;
    setHeader();
    renderSidebar();
  },
};

function listen(id) {
  const events = api.events(id);
  state.events = events;
  events.onmessage = (e) => {
    if (state.current !== id) return;
    const event = JSON.parse(e.data);
    HANDLERS[event.type]?.(event);
  };
}

let opening = 0;

export async function openChat(id) {
  const ticket = ++opening;
  state.events?.close();
  state.events = null;
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
  listen(id);
  scrollBottom(true, false);
}
