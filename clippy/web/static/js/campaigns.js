import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { isActive, onView } from "./views.js";
import { $, esc, toast } from "./util.js";

const RELOAD_MS = 300;

let data = { campaigns: [], presets: [] };
let editing = null;
let timer = 0;

const money = (n) => Number(n || 0).toLocaleString(document.documentElement.lang || undefined,
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const count = (n) => Number(n || 0).toLocaleString(document.documentElement.lang || undefined);
const secure = (url) => (typeof url === "string" && url.startsWith("https://") ? url : "");

function postRow(c, p) {
  const link = secure(p.url);
  const label = link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(link)}</a>` : esc(p.url);
  const problem = p.error_t ? `<span class="hint warn">${esc(tx(p.error_t))}</span>` : "";
  return `<li class="camp-post">
    <span class="camp-clipper">${esc(p.clipper || "-")}</span>
    <span class="camp-link">${label}${problem}</span>
    <span class="camp-num" data-label="${esc(t("camp.views"))}">${count(p.views)}</span>
    <span class="camp-num" data-label="${esc(t("camp.payout"))}">${money(p.payout)}</span>
    <button type="button" class="btn sm ghost" data-camp="removePost" data-id="${c.id}" data-post="${p.id}">${esc(t("camp.removePost"))}</button>
  </li>`;
}

function cardHtml(c) {
  const rate = c.cap > 0 ? t("camp.rate", { cpm: money(c.cpm), cap: money(c.cap) }) : t("camp.rateNoCap", { cpm: money(c.cpm) });
  const chats = Object.keys(c.chat_ids && !Array.isArray(c.chat_ids) ? c.chat_ids : {}).length;
  const posts = c.posts.length
    ? `<ul class="camp-posts">${c.posts.map((p) => postRow(c, p)).join("")}</ul>`
    : `<p class="hint">${esc(t("camp.noPosts"))}</p>`;
  return `<li class="sched-item camp-item${c.active ? "" : " is-live"}">
    <div class="sched-body">
      <div class="sched-head"><strong class="sched-title">${esc(c.name)}</strong>
        ${c.client ? `<span class="hint">${esc(c.client)}</span>` : ""}
        ${c.active ? "" : `<span class="hint">${esc(t("camp.paused"))}</span>`}</div>
      <p class="hint">${esc(rate)} · ${esc(t(`watch.preset.${c.preset}`))}</p>
      ${c.brief ? `<p class="camp-brief">${esc(c.brief)}</p>` : ""}
      <p class="hint">${esc(t("camp.sourcesCount", { n: (c.sources || []).length, chats }))}</p>
      <p class="camp-totals">${esc(t("camp.totals", { views: count(c.views), owed: money(c.owed) }))}</p>
      <div class="sched-actions">
        <button type="button" class="btn sm primary" data-camp="clip" data-id="${c.id}">${esc(t("camp.clip"))}</button>
        <button type="button" class="btn sm" data-camp="refresh" data-id="${c.id}">${esc(t("camp.refresh"))}</button>
        <button type="button" class="btn sm ghost" data-camp="edit" data-id="${c.id}">${esc(t("camp.edit"))}</button>
        <button type="button" class="btn sm ghost" data-camp="delete" data-id="${c.id}">${esc(t("camp.delete"))}</button>
      </div>
      <h4 class="camp-sub">${esc(t("camp.posts"))}</h4>
      ${posts}
      <form class="camp-add" data-id="${c.id}">
        <input class="field" name="clipper" maxlength="60" placeholder="${esc(t("camp.clipper"))}" aria-label="${esc(t("camp.clipper"))}">
        <input class="field" name="url" type="url" required maxlength="500" placeholder="${esc(t("camp.postUrl"))}" aria-label="${esc(t("camp.postUrl"))}">
        <button type="submit" class="btn sm">${esc(t("camp.addPost"))}</button>
      </form>
    </div></li>`;
}

function render() {
  $("#campItems").innerHTML = data.campaigns.length
    ? `<ul class="sched-list">${data.campaigns.map(cardHtml).join("")}</ul>`
    : `<div class="blank"><p>${esc(t("camp.empty"))}</p></div>`;
}

async function load() {
  try {
    data = await api.campaigns();
  } catch (err) {
    toast(tx(err.t, err.message));
  }
  render();
}

function showForm(on, campaign = null) {
  editing = campaign;
  $("#campList").hidden = on;
  $("#campForm").hidden = !on;
  $("#campError").hidden = true;
  if (!on) return;
  $("#campPreset").innerHTML = data.presets.map((k) => `<option value="${esc(k)}">${esc(t(`watch.preset.${k}`))}</option>`).join("");
  $("#campName").value = campaign?.name || "";
  $("#campClient").value = campaign?.client || "";
  $("#campPreset").value = campaign?.preset || "gameplay";
  $("#campBrief").value = campaign?.brief || "";
  $("#campCpm").value = campaign?.cpm ?? 0;
  $("#campCap").value = campaign?.cap ?? 0;
  $("#campSources").value = (campaign?.sources || []).join("\n");
  $("#campName").focus();
}

async function save(event) {
  event.preventDefault();
  const body = {
    name: $("#campName").value.trim(), client: $("#campClient").value.trim(), preset: $("#campPreset").value,
    brief: $("#campBrief").value.trim(), cpm: Number($("#campCpm").value) || 0, cap: Number($("#campCap").value) || 0,
    sources: $("#campSources").value.split(/\s+/).filter(Boolean), active: editing ? editing.active : true,
  };
  const button = $("#campSave");
  button.disabled = true;
  try {
    if (editing) await api.saveCampaign(editing.id, body);
    else await api.addCampaign(body);
    showForm(false);
    await load();
  } catch (err) {
    const box = $("#campError");
    box.textContent = tx(err.t, err.message);
    box.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function onAction(event) {
  const button = event.target.closest("button[data-camp]");
  if (!button) return;
  const id = Number(button.dataset.id);
  const act = button.dataset.camp;
  if (act === "edit") {
    showForm(true, data.campaigns.find((c) => c.id === id));
    return;
  }
  if (act === "delete" && !confirm(t("camp.confirmDelete"))) return;
  button.disabled = true;
  try {
    if (act === "clip") {
      const out = await api.clipCampaign(id);
      toast(out.started ? t("camp.clipped", { n: out.started }) : t("camp.nothingToClip"));
    } else if (act === "refresh") {
      await api.refreshCampaign(id);
      toast(t("camp.refreshed"));
    } else if (act === "delete") {
      await api.deleteCampaign(id);
    } else if (act === "removePost") {
      await api.removeCampaignPost(id, Number(button.dataset.post));
    }
    await load();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

async function onAdd(event) {
  const form = event.target.closest("form.camp-add");
  if (!form) return;
  event.preventDefault();
  try {
    await api.addCampaignPost(Number(form.dataset.id), form.clipper.value.trim(), form.url.value.trim());
    await load();
  } catch (err) {
    toast(tx(err.t, err.message));
  }
}

async function open() {
  showForm(false);
  $("#campItems").innerHTML = `<div class="blank"><p>${esc(t("accounts.loading"))}</p></div>`;
  await load();
}

export function bindCampaigns() {
  onView("campaigns", open);
  $("#campNew").addEventListener("click", () => showForm(true));
  $("#campBack").addEventListener("click", () => showForm(false));
  $("#campForm").addEventListener("submit", save);
  $("#campItems").addEventListener("click", onAction);
  $("#campItems").addEventListener("submit", onAdd);
  onLive((event) => {
    if (event.type !== "campaigns" || !isActive("campaigns") || !$("#campForm").hidden) return;
    clearTimeout(timer);
    timer = setTimeout(load, RELOAD_MS);
  });
}
