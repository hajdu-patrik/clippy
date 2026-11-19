import { api } from "./api.js";
import { openChat } from "./chat.js";
import { bindComposer } from "./composer.js";
import { bindDrawer, closeDrawer } from "./drawer.js";
import { applyStatic, loadCatalog } from "./i18n.js";
import { startLive } from "./live.js";
import { askOnce } from "./llm.js";
import { bindSettings, loadSettings, openSettingsFromLink } from "./settings.js";
import { bindPublish } from "./publish.js";
import { bindNotify } from "./notify.js";
import { bindVoice } from "./voice.js";
import { bindCopyDialog } from "./cards.js";
import { bindScroll } from "./messages.js";
import { loadChats, setWorker } from "./sidebar.js";
import { $, store } from "./util.js";

const THEME_KEY = "clippy-theme";
const STATUS_POLL_MS = 4000;

function applyTheme(theme, animate = false) {
  const root = document.documentElement;
  if (animate) {
    root.classList.add("theme-anim");
    setTimeout(() => root.classList.remove("theme-anim"), 350);
  }
  root.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#18171a" : "#f4f3f5";
  store.set(THEME_KEY, theme);
}

function newChat() {
  location.hash = "";
  closeDrawer();
  $("#input").focus();
}

function route() {
  const match = location.hash.match(/^#\/chat\/(\d+)/);
  openChat(match ? Number(match[1]) : null);
}

async function refreshStatus() {
  if (document.hidden) return;
  try { setWorker(await api.status()); } catch { return; }
}

function bindChrome() {
  $("#themeBtn").addEventListener("click", () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true));
  $("#newChat").addEventListener("click", newChat);
  $("#newChatTop").addEventListener("click", newChat);
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "o") {
      e.preventDefault();
      newChat();
    } else if (e.key === "/" && !typing) {
      e.preventDefault();
      $("#input").focus();
    }
  });
  window.addEventListener("hashchange", route);
  window.addEventListener("storage", (e) => {
    if (e.key === THEME_KEY && e.newValue && e.newValue !== document.documentElement.dataset.theme) applyTheme(e.newValue, true);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
  bindDrawer();
  bindScroll();
  bindSettings();
  bindPublish();
  bindNotify();
  bindVoice();
  bindCopyDialog();
}

applyTheme(store.get(THEME_KEY) || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
await loadCatalog(api.i18n);
applyStatic();
bindChrome();
bindComposer();
await loadChats();
await refreshStatus();
await loadSettings();
startLive();
setInterval(refreshStatus, STATUS_POLL_MS);
route();
openSettingsFromLink();
await askOnce();
if (matchMedia("(pointer: fine)").matches) $("#input").focus();
