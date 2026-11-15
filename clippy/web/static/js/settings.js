import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { state } from "./state.js";
import { $, toast } from "./util.js";

export async function loadSettings() {
  try { state.settings = await api.settings(); } catch { return; }
}

function open() {
  $("#profileLinks").value = (state.settings.profile_links || []).join("\n");
  $("#settingsDialog").showModal();
  $("#profileLinks").focus();
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

export function bindSettings() {
  $("#settingsBtn").addEventListener("click", open);
  $("#settingsCancel").addEventListener("click", () => $("#settingsDialog").close());
  $("#settingsForm").addEventListener("submit", save);
  onLive((event) => {
    if (event.type === "settings") state.settings = event.settings;
  });
}
