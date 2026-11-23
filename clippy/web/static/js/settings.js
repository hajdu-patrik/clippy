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
import { $, copyText, esc, toast } from "./util.js";
import { actions, button } from "./ui.js";

export async function loadSettings() {
  try { state.settings = await api.settings(); } catch { return; }
}

function dateText(seconds) {
  return new Date(seconds * 1000).toLocaleDateString(document.documentElement.lang || undefined);
}

let opened = null;
let pasting = false;

const named = (key, a) => `aria-label="${esc(t(key, { platform: a.label }))}"`;

function accountActions(name, a) {
  const data = (account) => ({ account, platform: name });
  const keys = button(t("accounts.appKeys"), { data: data("setup"), attrs: named("accounts.appKeysFor", a) });
  if (a.connected) {
    return actions(keys, button(t("accounts.disconnect"), { variant: "ghost danger", data: data("disconnect"),
      attrs: named("accounts.disconnectFor", a) }));
  }
  if (a.configured) {
    return actions(keys, button(t("accounts.signIn", { platform: a.label }), { variant: "primary", data: data("signin") }));
  }
  return actions(button(t("accounts.setup"), { variant: "", data: data("setup"), attrs: named("accounts.setupFor", a) }));
}

function keyField(name, f) {
  const label = t(`accounts.field.${f.name}`) + (f.optional ? ` ${t("accounts.optional")}` : "");
  const id = `key-${name}-${f.name}`;
  return `<label class="setup-field" for="${id}"><span>${esc(label)}</span>
    <input class="field" id="${id}" name="${f.name}" type="${f.secret ? "password" : "text"}" autocomplete="off"
      autocapitalize="off" spellcheck="false" maxlength="300" data-required="${f.optional || f.set ? "" : "1"}"
      placeholder="${esc(f.set ? t("accounts.keepSaved") : "")}"></label>`;
}

function tokenForm() {
  return `<div class="acct-setup"><label for="igToken">${esc(t("accounts.tokenLabel"))}</label>
    <p class="hint" id="igTokenHint">${esc(t("accounts.tokenHint"))}</p>
    <input class="field" id="igToken" type="password" autocomplete="off" autocapitalize="off" spellcheck="false"
      maxlength="2000" placeholder="IGAA…" aria-describedby="igTokenHint">
    ${actions(button(t("accounts.back"), { data: { account: "back" } }),
      button(t("accounts.tokenSave"), { variant: "primary", data: { account: "token-save" } }))}</div>`;
}

function setupPanel(name, a) {
  if (pasting) return tokenForm();
  const paste = a.pasteable
    ? `<button type="button" class="link-btn" data-account="token">${esc(t("accounts.pasteInstead"))}</button>` : "";
  const forget = a.saved ? button(t("accounts.forget"), { variant: "ghost danger", data: { account: "forget", platform: name } }) : "";
  return `<div class="acct-setup"><ol class="setup-steps">
    <li><span>${esc(t(`accounts.app.${name}`))}</span>
      <a href="${esc(a.console)}" target="_blank" rel="noopener noreferrer">${esc(t("accounts.openConsole"))}</a></li>
    <li><span>${esc(t("accounts.redirectStep"))}</span>
      <div class="copy-row"><code>${esc(a.redirect).replaceAll("/", "/<wbr>")}</code>${button(t("accounts.copy"), { data: { account: "copy", value: a.redirect } })}</div></li>
    <li><span>${esc(t("accounts.keysStep"))}</span>
      <div class="setup-fields">${a.fields.map((f) => keyField(name, f)).join("")}</div></li></ol>
    ${paste}
    ${actions(forget, button(t("settings.cancel"), { data: { account: "cancel" } }),
      button(t("accounts.saveSignIn"), { variant: "primary", data: { account: "save", platform: name } }))}</div>`;
}

function accountCard(name, a, here) {
  const open = here && opened === name;
  const until = a.connected && a.expires && !a.lasting ? ` · ${t("accounts.until", { date: dateText(a.expires) })}` : "";
  const status = a.connected ? t("accounts.signedInAs", { name: a.name || a.label }) + until
    : t(a.configured ? "accounts.ready" : "accounts.needsSetup");
  const notice = state.accounts?.notices?.[name];
  return `<li class="acct${a.connected ? " on" : ""}${open ? " open" : ""}" data-platform="${name}">
    <div class="acct-head">${platformIcon(name)}<div class="acct-name"><b>${esc(a.label)}</b>
      <span class="acct-tag">${esc(t(a.posting ? "accounts.canPost" : "accounts.connectOnly"))}</span></div>
      <span class="acct-status${a.connected ? " on" : ""}">${esc(t(a.connected ? "accounts.on" : "accounts.off"))}</span></div>
    <p class="acct-state">${esc(status)}</p>
    ${a.posting ? "" : `<p class="acct-note">${esc(t("accounts.noPosting", { platform: a.label }))}</p>`}
    ${notice ? `<p class="account-error" role="alert">${esc(tx(notice, ""))}</p>` : ""}
    ${open ? setupPanel(name, a) : ""}
    ${here && !open ? `<div class="acct-foot">${accountActions(name, a)}</div>` : ""}</li>`;
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
  opened = null;
  pasting = false;
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

function openSetup(platform) {
  opened = platform;
  pasting = false;
  renderAccounts();
  $(`#accountList li[data-platform="${platform}"] .acct-setup input`)?.focus();
}

async function saveAndSignIn(button) {
  const card = button.closest(".acct");
  const inputs = [...card.querySelectorAll(".setup-fields input")];
  const missing = inputs.find((i) => i.dataset.required && !i.value.trim());
  inputs.forEach((i) => (i === missing ? i.setAttribute("aria-invalid", "true") : i.removeAttribute("aria-invalid")));
  if (missing) {
    toast(t("accounts.missingKeys"));
    missing.focus();
    return;
  }
  const values = Object.fromEntries(inputs.map((i) => [i.name, i.value.trim()]));
  const { platform } = card.dataset;
  button.disabled = true;
  try {
    const { configured } = await api.setupAccount(platform, values);
    if (!configured) throw new Error(t("accounts.missingKeys"));
    location.assign((await api.connectAccount(platform)).url);
  } catch (err) {
    toast(tx(err.t, err.message));
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
    opened = null;
    pasting = false;
    toast(t("accounts.saved"));
    await refreshAccounts();
  } catch (err) {
    toast(tx(err.t, err.message));
    button.disabled = false;
  }
}

const LOCAL = {
  setup: (b) => openSetup(b.dataset.platform),
  cancel: () => { opened = null; renderAccounts(); },
  token: () => { pasting = true; renderAccounts(); $("#igToken").focus(); },
  back: () => { pasting = false; renderAccounts(); },
  copy: async (b) => toast(t(await copyText(b.dataset.value) ? "accounts.copied" : "copy.hint")),
  save: saveAndSignIn,
  "token-save": saveToken,
};

const REMOTE = {
  signin: async (platform) => location.assign((await api.connectAccount(platform)).url),
  disconnect: (platform) => api.disconnectAccount(platform),
  forget: async (platform) => { await api.forgetAccountApp(platform); opened = null; },
};

async function onAccount(event) {
  const button = event.target.closest("button[data-account]");
  if (!button) return;
  const { account, platform } = button.dataset;
  if (LOCAL[account]) {
    await LOCAL[account](button);
    return;
  }
  button.disabled = true;
  try {
    await REMOTE[account](platform);
    if (account !== "signin") await refreshAccounts();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
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
    if (e.key !== "Enter" || e.target.tagName !== "INPUT") return;
    e.preventDefault();
    e.target.closest(".acct").querySelector('button[data-account="token-save"], button[data-account="save"]')?.click();
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
