import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { $, esc, toast } from "./util.js";

const LABELS = { youtube: "YouTube", instagram: "Instagram", tiktok: "TikTok" };
const PRESET_ICONS = { podcast: "🎙️", gameplay: "🎮", promo: "📣", quick: "⚡" };

const everyText = (minutes) => t(`watch.every${minutes}`);

function watchRow(w) {
  const s = w.settings || {};
  const parts = [
    s.n ? t("watch.perVideo", { n: s.n }) : t("watch.perVideoAuto"),
    s.preset ? `${PRESET_ICONS[s.preset] || ""} ${t(`watch.preset.${s.preset}`)}` : "",
    everyText(s.every || 60),
    (s.platforms || []).length
      ? t("watch.autopostOn", { platforms: s.platforms.map((p) => LABELS[p]).join(", "), times: (s.slots || []).join(", ") })
      : "",
  ].filter(Boolean);
  const paused = w.enabled ? "" : ` · ${t("watch.paused")}`;
  return `<li class="account watch ${w.enabled ? "on" : ""}" data-wid="${w.id}">
    <div><b>${esc(w.title || w.url)}</b><span class="account-state">${esc(parts.join(" · ") + paused)}</span>
      ${w.error_t ? `<span class="account-error">${esc(tx(w.error_t, ""))}</span>` : ""}</div>
    <div class="watch-btns">
      <button type="button" class="ghost" data-watch="check">${esc(t("watch.checkNow"))}</button>
      <button type="button" class="ghost" data-watch="toggle">${esc(t(w.enabled ? "watch.pause" : "watch.resume"))}</button>
      <button type="button" class="ghost" data-watch="delete" aria-label="${esc(t("watch.delete"))}">${esc(t("watch.delete"))}</button>
    </div></li>`;
}

let watches = [];

export async function loadWatches() {
  try {
    watches = (await api.watches()).watches;
  } catch {
    return;
  }
  renderWatches();
}

function renderWatches() {
  const list = $("#watchList");
  if (!list) return;
  list.innerHTML = watches.length ? watches.map(watchRow).join("")
    : `<li class="account"><span class="account-state">${esc(t("watch.none"))}</span></li>`;
}

async function onWatch(event) {
  const button = event.target.closest("button[data-watch]");
  const row = event.target.closest("[data-wid]");
  if (!button || !row) return;
  const id = Number(row.dataset.wid);
  const w = watches.find((x) => x.id === id);
  button.disabled = true;
  try {
    if (button.dataset.watch === "check") {
      const { new: count } = await api.checkWatch(id);
      toast(count ? t("watch.found", { n: count }) : t("watch.nothingNew"));
    } else if (button.dataset.watch === "toggle") {
      await api.toggleWatch(id, !w.enabled);
    } else if (confirm(t("watch.confirmDelete", { title: w.title || w.url }))) {
      await api.deleteWatch(id);
    }
    await loadWatches();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

async function addWatch() {
  const button = $("#watchSave");
  const url = $("#watchUrl").value.trim();
  if (!url) {
    $("#watchUrl").focus();
    return;
  }
  button.disabled = true;
  try {
    await api.addWatch({
      url,
      preset: $("#watchPreset").value,
      n: Number($("#watchN").value) || 0,
      every: Number($("#watchEvery").value),
      platforms: [...document.querySelectorAll("#watchPlatforms input:checked")].map((x) => x.value),
      slots: $("#watchSlots").value,
      latest: $("#watchLatest").checked,
    });
    $("#watchUrl").value = "";
    $("#watchSlots").value = "";
    $("#watchLatest").checked = false;
    document.querySelectorAll("#watchPlatforms input").forEach((x) => { x.checked = false; });
    $("#watchAdd").open = false;
    toast(t("watch.added"));
    await loadWatches();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

export function bindWatches() {
  $("#watchList").addEventListener("click", onWatch);
  $("#watchSave").addEventListener("click", addWatch);
  $("#watchAdd").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches("input")) {
      e.preventDefault();
      addWatch();
    }
  });
  onLive((event) => {
    if (event.type === "watches" && $("#settingsDialog").open) loadWatches();
  });
}
