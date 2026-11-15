import { chatTitle, t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { state } from "./state.js";
import { $, store, toast } from "./util.js";

const KEY = "clippy-notify";
const seen = new Set();

function enabled() {
  return store.get(KEY) === "1" && "Notification" in window && Notification.permission === "granted";
}

function render() {
  const box = $("#notifyToggle");
  if (!box) return;
  box.checked = enabled();
  box.disabled = !("Notification" in window) || !window.isSecureContext;
  $("#notifyHint").hidden = !box.disabled;
}

async function toggle(event) {
  if (!event.target.checked) {
    store.set(KEY, "0");
    return render();
  }
  const answer = await Notification.requestPermission();
  store.set(KEY, answer === "granted" ? "1" : "0");
  if (answer !== "granted") toast(t("notify.denied"));
  render();
}

function finished(event) {
  const m = event.message;
  if (event.type !== "message" || !m?.data?.done || !m.data.job || seen.has(m.id)) return;
  seen.add(m.id);
  if (!enabled() || (!document.hidden && event.chat_id === state.current)) return;
  const chat = state.chats.find((c) => c.id === event.chat_id);
  const note = new Notification(chat ? chatTitle(chat.title) : "Clippy", {
    body: tx(m.data.summary_t, m.content), tag: `clippy-job-${m.id}`, icon: "/favicon.ico",
  });
  note.onclick = () => {
    window.focus();
    location.hash = `#/chat/${event.chat_id}`;
    note.close();
  };
}

export function bindNotify() {
  $("#notifyToggle").addEventListener("change", toggle);
  render();
  onLive((event) => {
    if (event.type === "message" && event.message?.kind === "progress" && !event.message?.data?.done) {
      seen.delete(event.message.id);
    }
    finished(event);
  });
  document.addEventListener("clippy:settingsOpen", render);
}
