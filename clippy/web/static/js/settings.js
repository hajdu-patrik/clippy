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
import { isActive, onView } from "./views.js";
import { $, esc, toast } from "./util.js";
import { actions, button } from "./ui.js";

export async function loadSettings() {
  try { state.settings = await api.settings(); } catch { return; }
}

function dateText(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(document.documentElement.lang || undefined);
}

let pasting = null;

function accountAction(name, a, here) {
  if (!here) return "";
  const named = (key) => `aria-label="${esc(`${t(key)} ${a.label}`)}"`;
  if (a.connected) {
    return button(t("accounts.disconnect"), { variant: "ghost danger", data: { account: "disconnect", platform: name },
      attrs: named("accounts.disconnect") });
  }
  if (!a.oauth) {
    return pasting === name ? "" : button(t("accounts.pasteToken"), { variant: "", data: { account: "token", platform: name },
      attrs: `aria-label="${esc(t("accounts.pasteTokenFor", { platform: a.label }))}"` });
  }
  return a.configured
    ? button(t("accounts.connect"), { variant: "", data: { account: "connect", platform: name }, attrs: named("accounts.connect") })
    : "";
}

function tokenForm() {
  return `<div class="token-box"><label for="igToken">${esc(t("accounts.tokenLabel"))}</label>
    <p class="hint" id="igTokenHint">${esc(t("accounts.tokenHint"))}</p>
    <input class="field" id="igToken" type="password" autocomplete="off" autocapitalize="off" spellcheck="false"
      maxlength="2000" placeholder="IGAA…" aria-describedby="igTokenHint">
    ${actions(button(t("settings.cancel"), { data: { account: "cancel" } }),
      button(t("accounts.tokenSave"), { variant: "primary", data: { account: "save" } }))}</div>`;
}

function accountCard(name, a, here) {
  const status = a.connected
    ? t("accounts.connectedAs", { name: a.name || a.label })
    : t(a.configured || !a.oauth ? "accounts.notConnected" : "accounts.notConfigured");
  const until = a.connected && a.expires && !a.lasting ? ` · ${t("accounts.until", { date: dateText(a.expires) })}` : "";
  const notice = state.accounts?.notices?.[name];
  const tag = a.posting ? "accounts.canPost" : "accounts.connectOnly";
  const action = accountAction(name, a, here);
  return `<li class="acct ${a.connected ? "on" : ""} ${a.posting ? "" : "no-post"}" data-platform="${name}">
    <div class="acct-head">${platformIcon(name)}<div class="acct-name"><b>${esc(a.label)}</b>
      <span class="acct-tag">${esc(t(tag))}</span></div>
      <span class="acct-status${a.connected ? " on" : ""}">${esc(t(a.connected ? "accounts.on" : "accounts.off"))}</span></div>
    <span class="account-state">${esc(status + until)}</span>
    ${a.posting ? "" : `<span class="acct-note">${esc(t("accounts.noPosting", { platform: a.label }))}</span>`}
    ${notice ? `<span class="account-error" role="alert">${esc(tx(notice, ""))}</span>` : ""}
    ${pasting === name ? tokenForm() : ""}
    ${action ? `<div class="acct-foot">${action}</div>` : ""}</li>`;
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
  if (isActive("settings")) renderAccounts();
}

function openSettings() {
  pasting = null;
  renderAccounts();
  refreshAccounts();
  loadLlm();
  document.dispatchEvent(new CustomEvent("clippy:settingsOpen"));
}

function openBrand() {
  $("#profileLinks").value = (state.settings.profile_links || []).join("\n");
  loadKit();
}

async function saveLinks() {
  const button = $("#linksSave");
  button.disabled = true;
  try {
    state.settings = await api.saveProfileLinks($("#profileLinks").value);
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
  if (account === "token" || account === "cancel") {
    pasting = account === "token" ? platform : null;
    renderAccounts();
    if (pasting) $("#igToken").focus();
    return;
  }
  if (account === "save") {
    await saveToken(button);
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

async function saveToken(button) {
  const token = $("#igToken").value.trim();
  if (!token) {
    $("#igToken").focus();
    return;
  }
  button.disabled = true;
  try {
    await api.instagramToken(token);
    pasting = null;
    toast(t("accounts.saved"));
    await refreshAccounts();
  } catch (err) {
    toast(tx(err.t, err.message));
    button.disabled = false;
  }
}

export function bindSettings() {
  onView("settings", openSettings);
  onView("brand", openBrand);
  onView("daily", loadPicks);
  onView("watched", loadWatches);
  onView("results", loadResults);
  $("#linksSave").addEventListener("click", saveLinks);
  $("#accountList").addEventListener("click", onAccount);
  $("#accountList").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.id === "igToken") {
      e.preventDefault();
      saveToken($('#accountList button[data-account="save"]'));
    }
  });
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
