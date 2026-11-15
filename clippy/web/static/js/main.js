import { api } from "./api.js";
import { openChat } from "./chat.js";
import { bindComposer } from "./composer.js";
import { bindDrawer, closeDrawer } from "./drawer.js";
import { applyStatic, getLang, loadCatalog, setLang } from "./i18n.js";
import { startLive } from "./live.js";
import { bindSettings, loadSettings } from "./settings.js";
import { bindCopyDialog } from "./cards.js";
import { bindScroll } from "./messages.js";
import { loadChats, setWorker } from "./sidebar.js";
import { $, store } from "./util.js";

const THEME_KEY = "clippy-theme";
const LANG_KEY = "clippy-lang";
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

function showLangButton() {
  const current = getLang();
  const box = $("#langSwitch");
  box.dataset.active = current;
  box.querySelectorAll("[data-lang]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.lang === current)));
}

function bindChrome() {
  $("#langSwitch").addEventListener("click", (e) => {
    const lang = e.target.closest("[data-lang]")?.dataset.lang;
    if (lang && lang !== getLang()) setLang(lang);
  });
  $("#langSwitch").addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    setLang(getLang() === "hu" ? "en" : "hu");
    $(`#langSwitch [data-lang="${getLang()}"]`).focus();
  });
  document.addEventListener("clippy:lang", async () => {
    showLangButton();
    await loadChats();
    await refreshStatus();
    route();
  });
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
    if (e.key === LANG_KEY && e.newValue && e.newValue !== getLang()) setLang(e.newValue);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
  bindDrawer();
  bindScroll();
  bindSettings();
  bindCopyDialog();
}

applyTheme(store.get(THEME_KEY) || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
await loadCatalog(api.i18n);
applyStatic();
showLangButton();
bindChrome();
bindComposer();
await loadChats();
await refreshStatus();
await loadSettings();
startLive();
setInterval(refreshStatus, STATUS_POLL_MS);
route();
if (matchMedia("(pointer: fine)").matches) $("#input").focus();
