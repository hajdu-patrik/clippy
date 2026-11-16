import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
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

function accountRow(name, a, here) {
  const status = a.connected
    ? t("accounts.connectedAs", { name: a.name || a.label })
    : t(a.configured ? "accounts.notConnected" : "accounts.notConfigured");
  const until = a.connected && a.expires && !a.lasting ? ` · ${t("accounts.until", { date: dateText(a.expires) })}` : "";
  const notice = state.accounts?.notices?.[name];
  let button = "";
  if (here && a.connected) {
    button = `<button type="button" class="ghost" data-account="disconnect" data-platform="${name}">${esc(t("accounts.disconnect"))}</button>`;
  } else if (here && a.oauth && a.configured) {
    button = `<button type="button" class="primary" data-account="connect" data-platform="${name}">${esc(t("accounts.connect"))}</button>`;
  } else if (here && !a.oauth) {
    button = `<button type="button" class="primary" data-account="token" data-platform="${name}">${esc(t("accounts.pasteToken"))}</button>`;
  }
  return `<li class="account ${a.connected ? "on" : ""}">
    <div><b>${esc(a.label)}</b><span class="account-state">${esc(status + until)}</span>
      ${notice ? `<span class="account-error">${esc(tx(notice, ""))}</span>` : ""}</div>${button}</li>`;
}

function renderAccounts() {
  const data = state.accounts;
  const list = $("#accountList");
  if (!data) {
    list.innerHTML = `<li class="account"><span class="account-state">${esc(t("accounts.loading"))}</span></li>`;
    return;
  }
  list.innerHTML = Object.entries(data.accounts).map(([name, a]) => accountRow(name, a, data.here)).join("");
  $("#accountsHere").hidden = data.here;
}

async function refreshAccounts() {
  await loadAccounts();
  if ($("#settingsDialog").open) renderAccounts();
}

function open() {
  $("#profileLinks").value = (state.settings.profile_links || []).join("\n");
  $("#igTokenBox").hidden = true;
  renderAccounts();
  $("#settingsDialog").showModal();
  $("#profileLinks").focus();
  refreshAccounts();
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
    state.settings = await api.saveSettings($("#profileLinks").value);
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
  $("#settingsBtn").addEventListener("click", open);
  $("#settingsCancel").addEventListener("click", () => $("#settingsDialog").close());
  $("#settingsForm").addEventListener("submit", save);
  $("#accountList").addEventListener("click", onAccount);
  $("#igSave").addEventListener("click", saveToken);
  $("#igToken").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveToken(); }
  });
  document.addEventListener("clippy:settings", open);
  bindPicks();
  bindWatches();
  bindResults();
  bindKit();
  onLive((event) => {
    if (event.type === "settings") state.settings = event.settings;
    if (event.type === "accounts") refreshAccounts();
  });
}
