import { api } from "./api.js";
import { chatTitle, t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { POSTING, platformLabel } from "./platforms.js";
import { loadAccounts } from "./publish.js";
import { state } from "./state.js";
import { $, esc, toast } from "./util.js";

const MIN_AHEAD_MS = 60 * 1000;
const QUICK_MORE = 5;
const RELOAD_MS = 300;
const LIVE_EVENTS = new Set(["clip", "accounts", "watches", "settings", "status", "publish", "daily", "chat_deleted", "hello"]);
const NOTES = { planned: "sched.note.planned", uploading: "sched.note.uploading", scheduled: "sched.note.scheduled",
  live: "sched.note.live", kept: "sched.note.kept" };

let data = { items: [], choices: [], slots: [], free: [], saved: false };
let editing = null;
let timer = 0;

const dialog = () => $("#scheduleDialog");
const locale = () => document.documentElement.lang || undefined;
const pad = (n) => String(n).padStart(2, "0");
const dateValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const timeValue = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const atOf = (date, time) => (date && time ? new Date(`${date}T${time}`).getTime() / 1000 : NaN);
const secure = (url) => (typeof url === "string" && url.startsWith("https://") ? url : "");

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function dayName(ts) {
  const at = new Date(ts * 1000), today = new Date();
  const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  if (at.toDateString() === today.toDateString()) return t("sched.today");
  if (at.toDateString() === tomorrow.toDateString()) return t("sched.tomorrow");
  return at.toLocaleDateString(locale(), { weekday: "short", month: "short", day: "numeric" });
}

function coverHtml(item) {
  return item.cover
    ? `<img src="${esc(item.cover)}" alt="" loading="lazy" decoding="async">`
    : `<svg aria-hidden="true"><use href="#i-film"/></svg>`;
}

function editHtml(item) {
  const at = new Date(item.due * 1000);
  return `<div class="sched-edit">
    <div class="post-options">
      <div><label for="editDate">${esc(t("sched.date"))}</label>
        <input class="field" id="editDate" type="date" value="${dateValue(at)}" required></div>
      <div><label for="editTime">${esc(t("sched.time"))}</label>
        <input class="field" id="editTime" type="time" value="${timeValue(at)}" required></div>
    </div>
    <div class="sched-actions">
      <button type="button" class="btn sm ghost" data-sched="cancel">${esc(t("settings.cancel"))}</button>
      <button type="button" class="btn sm primary" data-sched="save" data-id="${item.id}">${esc(t("sched.save"))}</button>
    </div></div>`;
}

function actionsHtml(item) {
  const open = `<a class="btn sm ghost" href="#/chat/${Number(item.chat_id)}" data-close>${esc(t("sched.openClip"))}</a>`;
  if (item.source === "plan") {
    return `<div class="sched-actions">
      <button type="button" class="btn sm ghost" data-sched="edit" data-id="${item.id}">${esc(t("sched.change"))}</button>
      <button type="button" class="btn sm ghost danger" data-sched="remove" data-id="${item.id}">${esc(t("sched.remove"))}</button>
      ${open}</div>`;
  }
  const studio = secure(item.studio), url = secure(item.url);
  const link = (href, key) => `<a class="btn sm ghost" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(t(key))}</a>`;
  const managed = item.state === "scheduled" || item.state === "kept";
  return `<div class="sched-actions">${managed && studio ? link(studio, "sched.studio") : ""}
    ${item.state === "live" && url ? link(url, "sched.watch") : ""}${open}</div>`;
}

function itemHtml(item) {
  const title = item.title || t("sched.clipNumber", { n: item.number });
  const isEdit = item.source === "plan" && editing === item.id;
  return `<li class="sched-item is-${esc(item.state)}">
    <div class="sched-thumb">${coverHtml(item)}</div>
    <div class="sched-body">
      <div class="sched-head"><b class="sched-time">${esc(item.due ? clock(item.due) : t("sched.noTime"))}</b>
        <span class="pill pill-${esc(item.state)}">${esc(t(`sched.state.${item.state}`))}</span></div>
      <b class="sched-title">${esc(title)}</b>
      <span class="account-state">${esc(t("sched.from", { chat: chatTitle(item.chat) || "…", n: item.number }))}</span>
      <span class="pf-list">${item.platforms.map(platformLabel).join("")}</span>
      <p class="hint">${esc(t(NOTES[item.state]))}</p>
      ${isEdit ? editHtml(item) : actionsHtml(item)}
    </div></li>`;
}

function groupHtml(heading, items) {
  return items.length
    ? `<section class="sched-group"><h3 class="day">${esc(heading)}</h3><ul class="sched-list">${items.map(itemHtml).join("")}</ul></section>`
    : "";
}

function renderList() {
  const box = $("#schedItems");
  const upcoming = data.items.filter((i) => i.due && i.state !== "live");
  const undated = data.items.filter((i) => !i.due);
  const past = data.items.filter((i) => i.due && i.state === "live").reverse();
  const days = new Map();
  for (const item of upcoming) {
    const key = dateValue(new Date(item.due * 1000));
    days.set(key, [...(days.get(key) || []), item]);
  }
  const html = [...days.values()].map((items) => groupHtml(dayName(items[0].due), items)).join("")
    + groupHtml(t("sched.undated"), undated) + groupHtml(t("sched.recent"), past);
  box.innerHTML = html || `<div class="blank"><p>${esc(t("sched.none"))}</p></div>`;
}

async function loadSchedule() {
  try {
    data = await api.schedule();
  } catch (err) {
    $("#schedItems").innerHTML = `<div class="blank"><p role="alert">${esc(t("sched.loadFailed", { error: tx(err.t, err.message) }))}</p></div>`;
    return;
  }
  if (!dialog().open) return;
  renderList();
  if (!$("#schedForm").hidden) renderForm(false);
}

function later() {
  const soon = new Date(Date.now() + 3600 * 1000);
  soon.setMinutes(0, 0, 0);
  return soon;
}

function selectedClip() {
  const chat = data.choices.find((c) => c.id === Number($("#schedChat").value));
  const picked = document.querySelector('#schedClips input[name="schedClip"]:checked');
  return chat && picked ? { chat, number: Number(picked.value) } : null;
}

function chosenPlatforms() {
  return [...document.querySelectorAll('#schedPlatforms input[name="platform"]:checked')].map((x) => x.value);
}

function renderClips(number) {
  const chat = data.choices.find((c) => c.id === Number($("#schedChat").value));
  const box = $("#schedClips");
  if (!chat) {
    box.innerHTML = `<p class="hint">${esc(t("sched.noClips"))}</p>`;
    return;
  }
  const keep = chat.clips.some((c) => c.number === number) ? number : chat.clips[0].number;
  box.innerHTML = chat.clips.map((c) => `<label class="clip-pick"><input type="radio" name="schedClip" value="${c.number}"
    ${c.number === keep ? "checked" : ""}><span class="clip-pick-art">${c.cover ? `<img src="${esc(c.cover)}" alt="" loading="lazy">` : ""}</span>
    <span class="clip-pick-name">${esc(c.title || t("sched.clipNumber", { n: c.number }))}<small>#${c.number}</small></span></label>`).join("");
}

function renderPlatforms(keep) {
  const accounts = state.accounts?.accounts || {};
  $("#schedPlatforms").innerHTML = POSTING.map((name) => {
    const a = accounts[name] || {};
    const who = a.connected ? (a.name ? ` <small>${esc(a.name)}</small>` : "") : ` <small>${esc(t("accounts.notConnected"))}</small>`;
    return `<label class="check ${a.connected ? "" : "off"}"><input type="checkbox" name="platform" value="${name}"
      ${a.connected ? "" : "disabled"} ${keep.includes(name) && a.connected ? "checked" : ""}><span>${platformLabel(name)}${who}</span></label>`;
  }).join("");
}

function renderQuick() {
  const box = $("#schedQuick");
  if (!data.slots.length) {
    box.innerHTML = `<p class="hint">${esc(t("sched.noSlots"))} <button type="button" class="link-btn" data-sched="slots">${esc(t("sched.addSlots"))}</button></p>`;
    return;
  }
  const [next, ...more] = data.free;
  const chip = (ts, label, cls = "") => `<button type="button" class="chip ${cls}" data-at="${ts}" aria-pressed="false">${esc(label)}</button>`;
  const label = (ts) => `${dayName(ts)} ${clock(ts)}`;
  box.innerHTML = [next ? chip(next, t("sched.nextFree", { when: label(next) }), "chip-next") : "",
    ...more.slice(0, QUICK_MORE).map((ts) => chip(ts, label(ts)))].join("")
    + `<span class="hint quick-note">${esc(t(data.saved ? "sched.yourSlots" : "sched.watchSlots", { times: data.slots.join(", ") }))}
    <button type="button" class="link-btn" data-sched="slots">${esc(t("sched.editSlots"))}</button></span>`;
}

function setAt(ts) {
  const at = new Date(ts * 1000);
  $("#schedDate").value = dateValue(at);
  $("#schedTime").value = timeValue(at);
  syncForm();
}

function syncForm() {
  const picked = new Set(chosenPlatforms());
  const own = [...picked].some((p) => p !== "youtube");
  $("#schedYoutube").hidden = !picked.has("youtube");
  $("#schedOwn").hidden = !own;
  $("#schedTtBox").hidden = !picked.has("tiktok");
  const at = atOf($("#schedDate").value, $("#schedTime").value);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  $("#schedZone").textContent = Number.isFinite(at)
    ? t("sched.goesOut", { when: `${dayName(at)} ${clock(at)}`, zone })
    : t("sched.zone", { zone });
  document.querySelectorAll("#schedQuick .chip").forEach((chip) => {
    chip.setAttribute("aria-pressed", String(Number(chip.dataset.at) === at));
  });
  $("#schedSend").disabled = !(selectedClip() && picked.size && Number.isFinite(at));
  $("#schedError").hidden = true;
}

function renderForm(fresh, pre = {}) {
  const keepChat = fresh ? pre.chat ?? data.choices[0]?.id : Number($("#schedChat").value);
  const keepClip = fresh ? pre.number : selectedClip()?.number;
  const keepPlatforms = fresh ? [] : chosenPlatforms();
  $("#schedChat").innerHTML = data.choices.map((c) =>
    `<option value="${c.id}">${esc(chatTitle(c.title))} (${c.clips.length})</option>`).join("");
  if (keepChat) $("#schedChat").value = String(keepChat);
  $("#schedChat").disabled = !data.choices.length;
  renderClips(keepClip);
  renderPlatforms(keepPlatforms);
  renderQuick();
  if (fresh) setAt(data.free[0] || later().getTime() / 1000);
  else syncForm();
}

function showForm(on, pre) {
  $("#schedList").hidden = on;
  $("#schedForm").hidden = !on;
  dialog().scrollTop = 0;
  if (on) {
    renderForm(true, pre);
    $("#schedChat").focus();
  } else {
    $("#schedNew").focus();
  }
}

async function open(event) {
  editing = null;
  $("#schedItems").innerHTML = `<div class="blank"><p>${esc(t("accounts.loading"))}</p></div>`;
  $("#schedList").hidden = false;
  $("#schedForm").hidden = true;
  if (!dialog().open) dialog().showModal();
  await Promise.all([loadSchedule(), loadAccounts()]);
  if (event?.detail?.chat) showForm(true, event.detail);
}

function showError(text) {
  const box = $("#schedError");
  box.textContent = text;
  box.hidden = false;
}

async function send(event) {
  event.preventDefault();
  const clip = selectedClip(), platforms = chosenPlatforms();
  const at = atOf($("#schedDate").value, $("#schedTime").value);
  if (!clip || !platforms.length || !Number.isFinite(at)) return;
  if (at * 1000 < Date.now() + MIN_AHEAD_MS) {
    showError(t("sched.past"));
    return;
  }
  const button = $("#schedSend");
  button.disabled = true;
  try {
    await api.publish(clip.chat.id, clip.number, {
      platforms, youtube_privacy: "public", tiktok_mode: $("#schedTt").value, at });
    toast(t("publish.planned", { date: `${dayName(at)} ${clock(at)}` }));
    showForm(false);
    await loadSchedule();
  } catch (err) {
    showError(t("publish.failed", { error: tx(err.t, err.message) }));
  } finally {
    button.disabled = false;
  }
}

async function onItemAction(event) {
  const link = event.target.closest("a[data-close]");
  if (link) {
    dialog().close();
    return;
  }
  const button = event.target.closest("button[data-sched]");
  if (!button) return;
  const id = Number(button.dataset.id);
  const act = button.dataset.sched;
  if (act === "edit" || act === "cancel") {
    editing = act === "edit" ? id : null;
    renderList();
    return;
  }
  button.disabled = true;
  try {
    if (act === "remove") {
      if (!confirm(t("sched.confirmRemove"))) return;
      await api.cancelScheduled(id);
      toast(t("publish.unscheduled"));
    } else if (act === "save") {
      const at = atOf($("#editDate").value, $("#editTime").value);
      if (!Number.isFinite(at) || at * 1000 < Date.now() + MIN_AHEAD_MS) {
        toast(t("sched.past"));
        return;
      }
      await api.reschedule(id, at);
      toast(t("sched.moved", { date: `${dayName(at)} ${clock(at)}` }));
      editing = null;
    }
    await loadSchedule();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

function toSlots() {
  dialog().close();
  document.dispatchEvent(new CustomEvent("clippy:settings", { detail: { page: "general" } }));
}

export function bindSchedule() {
  $("#scheduleBtn").addEventListener("click", () => open());
  document.addEventListener("clippy:schedule", open);
  $("#scheduleClose").addEventListener("click", () => dialog().close());
  $("#schedNew").addEventListener("click", () => showForm(true));
  $("#schedBack").addEventListener("click", () => showForm(false));
  $("#schedForm").addEventListener("submit", send);
  $("#schedForm").addEventListener("change", (e) => {
    if (e.target.id === "schedChat") renderClips();
    syncForm();
  });
  $("#schedForm").addEventListener("input", (e) => { if (e.target.type === "date" || e.target.type === "time") syncForm(); });
  $("#schedForm").addEventListener("click", (e) => {
    const chip = e.target.closest("button[data-at]");
    if (chip) setAt(Number(chip.dataset.at));
    else if (e.target.closest('button[data-sched="slots"]')) toSlots();
  });
  $("#schedItems").addEventListener("click", onItemAction);
  dialog().addEventListener("close", () => { editing = null; });
  onLive((event) => {
    if (!LIVE_EVENTS.has(event.type) || !dialog().open || editing !== null) return;
    clearTimeout(timer);
    timer = setTimeout(() => { loadAccounts().then(loadSchedule); }, RELOAD_MS);
  });
}
