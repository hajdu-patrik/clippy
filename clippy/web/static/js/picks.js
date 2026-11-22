import { api } from "./api.js";
import { isActive } from "./views.js";
import { showCopyDialog } from "./cards.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { LABELS } from "./platforms.js";
import { $, copyText, esc, toast } from "./util.js";
import { PlatformPicker, actions, blank, button, row } from "./ui.js";

let daily = [];
let games = [];

function clock(due) {
  const at = new Date(due * 1000);
  const time = at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const today = new Date();
  const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  if (at.toDateString() === today.toDateString()) return t("picks.today", { time });
  if (at.toDateString() === tomorrow.toDateString()) return t("picks.tomorrow", { time });
  return `${at.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
}

let picker = null;
const platforms = () => (picker ||= new PlatformPicker($("#picksPlatforms"), { name: "picksPlatform" }));

function pickRow(p) {
  const head = p.clip_title || p.title || p.streamer;
  const views = t("picks.views", { n: Number(p.views || 0).toLocaleString(undefined) });
  return `<li class="account pick" data-chat="${p.chat_id}">
    <div class="pick-time"><b>${esc(clock(p.due))}</b><span>${esc(t("picks.ny", { time: p.ny }))}</span></div>
    <div class="pick-body"><b>${esc(head)}</b>
      <span class="account-state">${esc(`${p.streamer} · ${p.platform} · ${views}`)}</span>
      <p class="pick-text${p.failed ? " failed" : ""}">${esc(p.text || t(p.failed ? "picks.failed" : "picks.making"))}</p></div>
    ${actions(button(t("picks.open"), { data: { pick: "open" } }), p.text && button(t("picks.copy"), { data: { pick: "copy" } }))}</li>`;
}

function dailyRow(d) {
  const s = d.settings || {};
  const names = (s.platforms || []).map((p) => LABELS[p] || p).join(", ");
  const posts = names ? ` · ${t("picks.posts", { platforms: names })}` : "";
  const state = t("picks.summary", { n: s.n || 3, at: s.at || "09:00", times: (s.times || []).join(", ") }) + posts
    + (d.enabled ? "" : ` · ${t("picks.paused")}`);
  const tail = actions(button(t("picks.runNow"), { data: { daily: "run" } }),
    button(t(d.enabled ? "picks.pause" : "picks.resume"), { data: { daily: "toggle" } }),
    button(t("picks.delete"), { variant: "ghost danger", data: { daily: "delete" } }));
  return row({ title: d.name, state, error: d.error_t ? tx(d.error_t, "") : "", tail, cls: `watch ${d.enabled ? "on" : ""}`,
    data: { did: d.id } }) + (d.picks || []).map(pickRow).join("");
}

function render() {
  const list = $("#picksList");
  if (!list) return;
  list.innerHTML = daily.length ? daily.map(dailyRow).join("") : blank(t("picks.none"), { tag: "li" });
  if (!platforms().box.children.length) platforms().render();
  const select = $("#picksGame");
  if (select && select.options.length !== games.length) {
    select.innerHTML = games.map((g) => `<option value="${esc(g.key)}">${esc(g.name)}</option>`).join("");
  }
}

export async function loadPicks() {
  try {
    ({ daily, games } = await api.daily());
  } catch {
    return;
  }
  render();
}

async function onClick(event) {
  const pickButton = event.target.closest("button[data-pick]");
  if (pickButton) {
    const row = pickButton.closest("[data-chat]");
    const p = daily.flatMap((d) => d.picks || []).find((x) => x.chat_id === Number(row.dataset.chat));
    if (pickButton.dataset.pick === "open") {
      location.hash = `#/chat/${row.dataset.chat}`;
    } else if (p && (await copyText(p.text))) {
      toast(t("card.copied"));
    } else if (p) {
      showCopyDialog(p.text);
    }
    return;
  }
  const button = event.target.closest("button[data-daily]");
  const row = event.target.closest("[data-did]");
  if (!button || !row) return;
  const d = daily.find((x) => x.id === Number(row.dataset.did));
  button.disabled = true;
  try {
    if (button.dataset.daily === "run") {
      const { new: count } = await api.runDaily(d.id);
      toast(count ? t("picks.found", { n: count }) : t("picks.nothing"));
    } else if (button.dataset.daily === "toggle") {
      await api.toggleDaily(d.id, !d.enabled);
    } else if (confirm(t("picks.confirmDelete", { game: d.name }))) {
      await api.deleteDaily(d.id);
    }
    await loadPicks();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

async function addDaily() {
  const button = $("#picksSave");
  button.disabled = true;
  try {
    await api.addDaily({
      game: $("#picksGame").value,
      n: Number($("#picksN").value) || 3,
      at: $("#picksAt").value || "09:00",
      times: $("#picksTimes").value,
      platforms: platforms().selected(),
      now: true,
    });
    $("#picksAdd").open = false;
    toast(t("picks.added"));
    await loadPicks();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

export function bindPicks() {
  $("#picksList").addEventListener("click", onClick);
  $("#picksSave").addEventListener("click", addDaily);
  onLive((event) => {
    if ((event.type === "daily" || event.type === "clip") && isActive("daily")) loadPicks();
  });
}
