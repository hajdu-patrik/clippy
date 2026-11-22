import { api } from "./api.js";
import { isActive } from "./views.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { LABELS } from "./platforms.js";
import { $, toast } from "./util.js";
import { PlatformPicker, actions, blank, button, row } from "./ui.js";

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
  const tail = actions(button(t("watch.checkNow"), { data: { watch: "check" } }),
    button(t(w.enabled ? "watch.pause" : "watch.resume"), { data: { watch: "toggle" } }),
    button(t("watch.delete"), { variant: "ghost danger", data: { watch: "delete" } }));
  return row({ title: w.title || w.url, state: parts.join(" · ") + paused, error: w.error_t ? tx(w.error_t, "") : "", tail,
    cls: `watch ${w.enabled ? "on" : ""}`, data: { wid: w.id } });
}

let watches = [];
let picker = null;
const platforms = () => (picker ||= new PlatformPicker($("#watchPlatforms"), { name: "watchPlatform" }));

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
  list.innerHTML = watches.length ? watches.map(watchRow).join("") : blank(t("watch.none"), { tag: "li" });
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
      platforms: platforms().selected(),
      slots: $("#watchSlots").value,
      latest: $("#watchLatest").checked,
    });
    $("#watchUrl").value = "";
    $("#watchSlots").value = "";
    $("#watchLatest").checked = false;
    platforms().clear();
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
  platforms().render();
  $("#watchList").addEventListener("click", onWatch);
  $("#watchSave").addEventListener("click", addWatch);
  $("#watchAdd").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches("input")) {
      e.preventDefault();
      addWatch();
    }
  });
  onLive((event) => {
    if (event.type === "watches" && isActive("watched")) loadWatches();
  });
}
