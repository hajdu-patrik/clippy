import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { bindLlm, loadLlm } from "./llm.js";
import { onLive } from "./live.js";
import { platformIcon } from "./platforms.js";
import { loadAccounts } from "./publish.js";
import { state } from "./state.js";
import { bindKit, loadKit } from "./kit.js";
import { bindResults, loadResults } from "./results.js";
import { bindPicks, loadPicks } from "./picks.js";
import { bindWatches, loadWatches } from "./watches.js";
import { $, esc, toast } from "./util.js";

export async function loadSettings() {
  try { state.settings = await api.settings(); } catch { return; }
}

function dateText(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(document.documentElement.lang || undefined);
}

function accountButton(name, a, here) {
  const label = (key) => esc(t(key));
  const aria = esc(t("accounts.pasteTokenFor", { platform: a.label }));
  if (!here) return "";
  if (a.connected) {
    return `<button type="button" class="btn ghost" data-account="disconnect" data-platform="${name}" aria-label="${label("accounts.disconnect")} ${esc(a.label)}">${platformIcon(name, "sm")}${label("accounts.disconnect")}</button>`;
  }
  if (!a.oauth) {
    return `<button type="button" class="btn ghost" data-account="token" data-platform="${name}" aria-label="${aria}">${platformIcon(name, "sm")}${label("accounts.pasteToken")}</button>`;
  }
  return a.configured
    ? `<button type="button" class="btn ghost" data-account="connect" data-platform="${name}" aria-label="${label("accounts.connect")} ${esc(a.label)}">${platformIcon(name, "sm")}${label("accounts.connect")}</button>`
    : "";
}

function accountCard(name, a, here) {
  const status = a.connected
    ? t("accounts.connectedAs", { name: a.name || a.label })
    : t(a.configured ? "accounts.notConnected" : "accounts.notConfigured");
  const until = a.connected && a.expires && !a.lasting ? ` · ${t("accounts.until", { date: dateText(a.expires) })}` : "";
  const notice = state.accounts?.notices?.[name];
  const tag = a.posting ? "accounts.canPost" : "accounts.connectOnly";
  return `<li class="acct ${a.connected ? "on" : ""} ${a.posting ? "" : "no-post"}" data-platform="${name}">
    <div class="acct-head">${platformIcon(name)}<div class="acct-name"><b>${esc(a.label)}</b>
      <span class="acct-tag">${esc(t(tag))}</span></div></div>
    <span class="account-state">${esc(status + until)}</span>
    ${a.posting ? "" : `<span class="acct-note">${esc(t("accounts.noPosting", { platform: a.label }))}</span>`}
    ${notice ? `<span class="account-error" role="alert">${esc(tx(notice, ""))}</span>` : ""}
    <div class="acct-foot">${accountButton(name, a, here)}</div></li>`;
}

function renderAccounts() {
  const data = state.accounts;
  const list = $("#accountList");
  if (!data) {
    list.innerHTML = `<li class="acct wide"><span class="account-state">${esc(t("accounts.loading"))}</span></li>`;
    return;
  }
  list.innerHTML = Object.entries(data.accounts).map(([name, a]) => accountCard(name, a, data.here)).join("");
  $("#accountsHere").hidden = data.here;
}

async function refreshAccounts() {
  await loadAccounts();
  if ($("#settingsDialog").open) renderAccounts();
}

const PAGES = ["general", "ai", "accounts", "style", "auto", "results"];
let page = "general";

function showPage(name, focus = false) {
  page = PAGES.includes(name) ? name : "general";
  for (const p of PAGES) {
    const tab = $(`#tab-${p}`), on = p === page;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(`#page-${p}`).hidden = !on;
    if (on) tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (on && focus) tab.focus();
  }
  const general = page === "general";
  $("#settingsSave").hidden = !general;
  $("#settingsCancel").textContent = t(general ? "settings.cancel" : "settings.close");
  $("#settingsDialog").scrollTop = 0;
}

function onTabKey(event) {
  const i = PAGES.indexOf(page), n = PAGES.length;
  const to = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[event.key];
  if (to === undefined) return;
  event.preventDefault();
  showPage(PAGES[to], true);
}

function open(event) {
  $("#profileLinks").value = (state.settings.profile_links || []).join("\n");
  $("#igTokenBox").hidden = true;
  renderAccounts();
  showPage(event?.detail?.page || page);
  $("#settingsDialog").showModal();
  $(`#tab-${page}`).focus();
  refreshAccounts();
  loadLlm();
  loadPicks();
  loadWatches();
  loadResults();
  loadKit();
  document.dispatchEvent(new CustomEvent("clippy:settingsOpen"));
}

async function save(event) {
  event.preventDefault();
  const button = $("#settingsSave");
  button.disabled = true;
  try {
    state.settings = await api.saveProfileLinks($("#profileLinks").value);
    $("#settingsDialog").close();
    toast(t("settings.saved"));
  } catch (err) {
    toast(t("settings.failed", { error: tx(err.t, err.message) }));
  } finally {
    button.disabled = false;
  }
}

async function onAccount(event) {
  const button = event.target.closest("button[data-account]");
  if (!button) return;
  const { account, platform } = button.dataset;
  if (account === "token") {
    $("#igTokenBox").hidden = false;
    $("#igToken").focus();
    return;
  }
  button.disabled = true;
  try {
    if (account === "connect") {
      const { url } = await api.connectAccount(platform);
      location.assign(url);
      return;
    }
    await api.disconnectAccount(platform);
    await refreshAccounts();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

async function saveToken() {
  const field = $("#igToken");
  const button = $("#igSave");
  if (!field.value.trim()) return;
  button.disabled = true;
  try {
    await api.instagramToken(field.value.trim());
    field.value = "";
    $("#igTokenBox").hidden = true;
    toast(t("accounts.saved"));
    await refreshAccounts();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

export function openSettingsFromLink() {
  if (location.hash !== "#settings") return;
  history.replaceState(null, "", location.pathname);
  open();
}

export function bindSettings() {
  $("#settingsBtn").addEventListener("click", () => open());
  $("#settingsTabs").addEventListener("click", (e) => {
    const tab = e.target.closest("[role=tab]");
    if (tab) showPage(tab.id.slice(4));
  });
  $("#settingsTabs").addEventListener("keydown", onTabKey);
  $("#settingsCancel").addEventListener("click", () => $("#settingsDialog").close());
  $("#settingsForm").addEventListener("submit", save);
  $("#accountList").addEventListener("click", onAccount);
  $("#igSave").addEventListener("click", saveToken);
  $("#igToken").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveToken(); }
  });
  document.addEventListener("clippy:settings", open);
  bindLlm();
  bindPicks();
  bindWatches();
  bindResults();
  bindKit();
  onLive((event) => {
    if (event.type === "settings") state.settings = event.settings;
    if (event.type === "accounts") refreshAccounts();
  });
}
