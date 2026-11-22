import { t } from "./i18n.js";
import { $ } from "./util.js";

export const VIEWS = ["projects", "calendar", "results", "daily", "watched", "campaigns", "brand", "settings"];
const TITLES = { projects: "page.projects", calendar: "sched.title", results: "results.title", daily: "picks.title",
  watched: "watch.title", campaigns: "camp.title", brand: "kit.title", settings: "settings.title" };

let active = null;

export const isActive = (name) => active === name;

export function viewOf(hash) {
  const name = (hash || "").replace(/^#\/?/, "").split("/")[0];
  return VIEWS.includes(name) ? name : null;
}

export function showView(name) {
  active = VIEWS.includes(name) ? name : null;
  $(".main").dataset.view = active ? "page" : "chat";
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== active;
  for (const link of document.querySelectorAll(".nav-item[data-view]")) {
    if (link.dataset.view === active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (!active) return;
  $("#chatTitle").textContent = t(TITLES[active]);
  $("#chatSub").textContent = "";
  document.title = `${t(TITLES[active])} · Clippy`;
  $(`#view-${active}`).scrollTop = 0;
  document.dispatchEvent(new CustomEvent("clippy:view", { detail: active }));
}

export function onView(name, handler) {
  document.addEventListener("clippy:view", (e) => { if (e.detail === name) handler(); });
}

export function go(name) {
  location.hash = name ? `#/${name}` : "";
}
