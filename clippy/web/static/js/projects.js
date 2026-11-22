import { chatTitle, t } from "./i18n.js";
import { onLive } from "./live.js";
import { state } from "./state.js";
import { isActive, onView } from "./views.js";
import { $, esc } from "./util.js";
import { blank } from "./ui.js";

const locale = () => document.documentElement.lang || undefined;

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString(locale(), { month: "short", day: "numeric", year: "numeric" }) : "";
}

function render() {
  const q = $("#projectSearch").value.trim().toLowerCase();
  const chats = state.chats.filter((c) => !q || `${c.title} ${c.video_title || ""}`.toLowerCase().includes(q));
  $("#projectList").innerHTML = chats.length
    ? chats.map((c) => `<li><a class="project" href="#/chat/${c.id}">
        <strong>${esc(chatTitle(c.title))}</strong>
        <span class="hint">${esc(c.video_title && c.video_title !== c.title ? c.video_title : "")}</span>
        <span class="hint">${esc(when(c.updated || c.created))}</span></a></li>`).join("")
    : blank(t(q ? "projects.none" : "projects.empty"), { tag: "li" });
}

export function bindProjects() {
  onView("projects", render);
  $("#projectSearch").addEventListener("input", render);
  onLive((event) => { if (isActive("projects") && (event.type === "chat" || event.type === "chat_deleted")) render(); });
}
