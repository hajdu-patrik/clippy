import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { LABELS, POSTING } from "./platforms.js";
import { state } from "./state.js";
import { $, esc, toast } from "./util.js";

let target = null;

export async function loadAccounts() {
  try { state.accounts = await api.accounts(); } catch { return state.accounts; }
  return state.accounts;
}

function whenText(seconds) {
  return new Date(seconds * 1000).toLocaleString(document.documentElement.lang || undefined,
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const count = (n) => new Intl.NumberFormat(document.documentElement.lang || undefined, { notation: "compact" }).format(n || 0);

function statsHtml(s) {
  if (!s) return "";
  const parts = [
    t("results.views", { n: count(s.views) }),
    s.likes ? `❤ ${count(s.likes)}` : "",
    s.comments ? `💬 ${count(s.comments)}` : "",
    s.shares ? `↗ ${count(s.shares)}` : "",
    s.watched != null ? t("results.watched", { p: Math.round(100 * s.watched) }) : "",
  ].filter(Boolean);
  return `<span class="post-stats">${esc(parts.join(" · "))}</span>`;
}

function plannedHtml(c) {
  return (c.scheduled || []).map((s) => `<li class="post planned"><b>🕒 ${esc(whenText(s.due))}</b>
    ${esc(s.platforms.map((p) => LABELS[p] || p).join(", "))}
    <button type="button" class="link-btn" data-act="unschedule" data-sid="${s.id}">${esc(t("publish.unschedule"))}</button></li>`).join("");
}

export function postsHtml(c) {
  const posts = c.posts || [];
  const planned = plannedHtml(c);
  if (!posts.length && !planned) return "";
  const items = posts.map((p) => {
    const label = esc(LABELS[p.platform] || p.platform);
    if (p.state === "failed") {
      return `<li class="post failed"><b>${label}</b> ${esc(tx(p.error_t, t("publish.state.failed")))}</li>`;
    }
    const what = esc(t(`publish.state.${p.state}`));
    const link = p.url ? ` <a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(t("publish.open"))}</a>` : "";
    return `<li class="post ok"><b>${label}</b> ${what}${link}${statsHtml(p.stats)}</li>`;
  }).join("");
  return `<ul class="posts" aria-label="${esc(t("publish.posts"))}">${planned}${items}</ul>`;
}

function caption(c) {
  return (c.txt || "").split("\n---\n")[0].trim() || c.clip?.description || c.clip?.title || "";
}

function renderPlatforms() {
  const accounts = state.accounts?.accounts || {};
  const box = $("#postPlatforms");
  box.innerHTML = POSTING.map((name) => {
    const a = accounts[name] || {};
    const who = a.connected ? (a.name ? ` <small>${esc(a.name)}</small>` : "") : ` <small>${esc(t("accounts.notConnected"))}</small>`;
    return `<label class="check ${a.connected ? "" : "off"}"><input type="checkbox" name="platform" value="${name}"
      ${a.connected ? "checked" : "disabled"}><span>${esc(LABELS[name])}${who}</span></label>`;
  }).join("");
  const any = POSTING.some((name) => accounts[name]?.connected);
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
  const d = clip.clip || {};
  $("#variantOption").hidden = !d.alt_title;
  $("#postVariant").innerHTML = [["a", d.title], ["b", d.alt_title]].filter(([, x]) => x)
    .map(([v, x]) => `<option value="${v}">${esc(t(v === "a" ? "publish.variantA" : "publish.variantB", { title: x }))}</option>`).join("");
  $("#postWhen").value = "now";
  $("#postAt").value = "";
  syncWhen();
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
  const later = $("#postWhen").value === "later";
  const at = later ? new Date($("#postAt").value).getTime() / 1000 : null;
  if (later && !at) {
    $("#postAt").focus();
    return;
  }
  const button = $("#postSend");
  button.disabled = true;
  try {
    await api.publish(state.current, target.number, {
      platforms, youtube_privacy: $("#ytPrivacy").value, tiktok_mode: $("#ttMode").value, at,
      variant: $("#postVariant").value || "a",
    });
    $("#postDialog").close();
    toast(later ? t("publish.planned", { date: whenText(at) }) : t("publish.started"));
  } catch (err) {
    toast(t("publish.failed", { error: tx(err.t, err.message) }));
  } finally {
    button.disabled = false;
  }
}

export async function cancelPlanned(id) {
  try {
    await api.cancelScheduled(id);
    toast(t("publish.unscheduled"));
  } catch (err) {
    toast(tx(err.t, err.message));
  }
}

function syncWhen() {
  const later = $("#postWhen").value === "later";
  $("#postAtBox").hidden = !later;
  if (later && !$("#postAt").value) {
    const soon = new Date(Date.now() + 3600 * 1000);
    soon.setMinutes(0, 0, 0);
    $("#postAt").value = new Date(soon.getTime() - soon.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  $("#postSend").textContent = t(later ? "publish.schedule" : "publish.send");
}

export function bindPublish() {
  $("#postWhen").addEventListener("change", syncWhen);
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
