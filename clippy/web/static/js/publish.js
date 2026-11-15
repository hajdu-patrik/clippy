import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { state } from "./state.js";
import { $, esc, toast } from "./util.js";

const PLATFORMS = ["youtube", "instagram", "tiktok"];
const LABELS = { youtube: "YouTube", instagram: "Instagram", tiktok: "TikTok" };
let target = null;

export async function loadAccounts() {
  try { state.accounts = await api.accounts(); } catch { return state.accounts; }
  return state.accounts;
}

export function postsHtml(c) {
  const posts = c.posts || [];
  if (!posts.length) return "";
  const items = posts.map((p) => {
    const label = esc(LABELS[p.platform] || p.platform);
    if (p.state === "failed") {
      return `<li class="post failed"><b>${label}</b> ${esc(tx(p.error_t, t("publish.state.failed")))}</li>`;
    }
    const what = esc(t(`publish.state.${p.state}`));
    const link = p.url ? ` <a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(t("publish.open"))}</a>` : "";
    return `<li class="post ok"><b>${label}</b> ${what}${link}</li>`;
  }).join("");
  return `<ul class="posts" aria-label="${esc(t("publish.posts"))}">${items}</ul>`;
}

function caption(c) {
  return (c.txt || "").split("\n---\n")[0].trim() || c.clip?.description || c.clip?.title || "";
}

function renderPlatforms() {
  const accounts = state.accounts?.accounts || {};
  const box = $("#postPlatforms");
  box.innerHTML = PLATFORMS.map((name) => {
    const a = accounts[name] || {};
    const who = a.connected ? (a.name ? ` <small>${esc(a.name)}</small>` : "") : ` <small>${esc(t("accounts.notConnected"))}</small>`;
    return `<label class="check ${a.connected ? "" : "off"}"><input type="checkbox" name="platform" value="${name}"
      ${a.connected ? "checked" : "disabled"}><span>${esc(LABELS[name])}${who}</span></label>`;
  }).join("");
  const any = PLATFORMS.some((name) => accounts[name]?.connected);
  $("#postNote").textContent = any ? t("publish.note") : t("publish.noAccount");
  $("#postSend").disabled = !any;
  $("#postSettings").hidden = any;
  syncOptions();
}

function chosen() {
  return [...document.querySelectorAll('#postPlatforms input[name="platform"]:checked')].map((x) => x.value);
}

function syncOptions() {
  const picked = new Set(chosen());
  $("#ytOption").hidden = !picked.has("youtube");
  $("#ttOption").hidden = !picked.has("tiktok");
  if (state.accounts) $("#postSend").disabled = !picked.size;
}

export async function openPostDialog(clip) {
  target = clip;
  $("#postTitle").textContent = t("publish.title", { n: clip.number });
  $("#postCaption").value = caption(clip);
  $("#postPlatforms").innerHTML = "";
  $("#ytOption").hidden = $("#ttOption").hidden = $("#postSend").disabled = true;
  if (state.accounts) renderPlatforms();
  $("#postDialog").showModal();
  await loadAccounts();
  if (target === clip) renderPlatforms();
}

async function send(event) {
  event.preventDefault();
  if (!target) return;
  const platforms = chosen();
  if (!platforms.length) return;
  const button = $("#postSend");
  button.disabled = true;
  try {
    await api.publish(state.current, target.number, {
      platforms, youtube_privacy: $("#ytPrivacy").value, tiktok_mode: $("#ttMode").value,
    });
    $("#postDialog").close();
    toast(t("publish.started"));
  } catch (err) {
    toast(t("publish.failed", { error: tx(err.t, err.message) }));
  } finally {
    button.disabled = false;
  }
}

export function bindPublish() {
  $("#postCancel").addEventListener("click", () => $("#postDialog").close());
  $("#postForm").addEventListener("submit", send);
  $("#postPlatforms").addEventListener("change", syncOptions);
  $("#postSettings").addEventListener("click", () => {
    $("#postDialog").close();
    document.dispatchEvent(new CustomEvent("clippy:settings"));
  });
  onLive((event) => {
    if (event.type !== "accounts") return;
    loadAccounts().then(() => { if ($("#postDialog").open && target) renderPlatforms(); });
  });
}
