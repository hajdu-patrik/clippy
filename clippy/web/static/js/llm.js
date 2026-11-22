import { api } from "./api.js";
import { go } from "./views.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { state } from "./state.js";
import { $, esc, store, toast } from "./util.js";

const ASKED_KEY = "clippy-llm-asked";
const WITH_MODEL = ["ollama", "openrouter", "claude", "codex"];
const CUSTOM = "__custom";
const draft = { mode: "", models: {} };
const offered = {};
let checking = 0;

const current = () => state.llm?.modes.find((m) => m.id === draft.mode);

function fillDraft() {
  draft.mode = state.llm.mode;
  draft.models = Object.fromEntries(state.llm.modes.map((m) => [m.id, m.model]));
}

function modeCard(mode) {
  const on = mode.id === draft.mode;
  const readOnly = mode.id === "openai" || !state.llm.here;
  return `<li class="acct ${on ? "on" : ""}"><label class="llm-mode">
    <input type="radio" name="llmMode" value="${mode.id}" ${on ? "checked" : ""} ${readOnly ? "disabled" : ""}>
    <span class="acct-name"><b>${esc(t(`llm.mode.${mode.id}`))}</b>
    <span class="acct-tag">${esc(t(`llm.tag.${mode.id}`))}</span></span></label>
    <span class="acct-note">${esc(t(`llm.about.${mode.id}`))}</span></li>`;
}

function placeholderOf(mode) {
  return mode.default || t(`llm.placeholder.${mode.id}`);
}

function renderFields() {
  const mode = current();
  if (!mode) return;
  const here = state.llm.here;
  const hasModel = WITH_MODEL.includes(draft.mode);
  $("#llmModelBox").hidden = !hasModel;
  for (const id of ["llmKeyBox", "llmKeyHint", "llmModelHint"]) $(`#${id}`).hidden = draft.mode !== "openrouter";
  if (hasModel) renderModelPick(mode);
  $("#llmKey").value = "";
  $("#llmKey").placeholder = mode.key ? t("llm.keySaved", { key: mode.key }) : "sk-or-…";
  for (const id of ["llmModelPick", "llmModel", "llmKey", "llmTest", "llmSave"]) $(`#${id}`).disabled = !here || draft.mode === "openai";
}

function renderModelPick(mode) {
  const value = draft.models[draft.mode] || "";
  const list = (offered[draft.mode] || []).filter((m) => m !== mode.default);
  const custom = Boolean(value) && value !== mode.default && !list.includes(value);
  const label = mode.default ? t("llm.default", { model: mode.default }) : t("llm.defaultCli");
  $("#llmModelPick").innerHTML = [`<option value="">${esc(label)}</option>`,
    ...list.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`),
    `<option value="${CUSTOM}">${esc(t("llm.other"))}</option>`].join("");
  $("#llmModelPick").value = custom ? CUSTOM : value === mode.default ? "" : value;
  $("#llmModel").hidden = !custom;
  $("#llmModel").value = custom ? value : "";
  $("#llmModel").placeholder = placeholderOf(mode);
}

async function loadModels(mode) {
  if (!WITH_MODEL.includes(mode) || offered[mode]) return;
  try { offered[mode] = (await api.llmModels(mode)).models; } catch { offered[mode] = []; }
  if (draft.mode === mode && current()) renderModelPick(current());
}

function pickModel() {
  const value = $("#llmModelPick").value;
  const custom = value === CUSTOM;
  $("#llmModel").hidden = !custom;
  draft.models[draft.mode] = custom ? $("#llmModel").value.trim() : value;
  if (custom) $("#llmModel").focus();
  else check();
}

function render() {
  if (!state.llm) return;
  $("#llmModes").innerHTML = state.llm.modes.map(modeCard).join("");
  $("#llmHere").hidden = state.llm.here;
  $("#llmFirst").hidden = state.llm.chosen;
  renderFields();
  loadModels(draft.mode);
}

function say(text, bad = false) {
  const line = $("#llmStatus");
  line.textContent = text;
  line.classList.toggle("warn", bad);
}

async function check() {
  const ticket = ++checking;
  if (draft.mode === "openai") return say("");
  say(t("llm.checking"));
  try {
    const { problem } = await api.llmProblem(body());
    if (ticket === checking) say(problem ? tx(problem, problem.message) : t("llm.ready"), Boolean(problem));
  } catch (err) {
    if (ticket === checking) say(tx(err.t, err.message), true);
  }
}

function body() {
  return { mode: draft.mode, model: draft.models[draft.mode] || "", key: $("#llmKey").value.trim() };
}

async function test() {
  const button = $("#llmTest");
  button.disabled = true;
  checking += 1;
  say(t("llm.testing"));
  try {
    const done = await api.testLlm(body());
    say(t("llm.testOk", { seconds: done.seconds, llm: done.llm }));
  } catch (err) {
    say(tx(err.t, err.message), true);
  } finally {
    button.disabled = false;
  }
}

async function save() {
  const button = $("#llmSave");
  button.disabled = true;
  try {
    state.llm = { ...(await api.saveLlm(body())), here: true };
    store.set(ASKED_KEY, "1");
    fillDraft();
    render();
    toast(t("llm.saved"));
    check();
  } catch (err) {
    say(tx(err.t, err.message), true);
  } finally {
    button.disabled = false;
  }
}

function pick(event) {
  if (event.target.name !== "llmMode") return;
  draft.mode = event.target.value;
  render();
  $("#llmModes input:checked").focus();
  check();
}

export async function loadLlm() {
  try { state.llm = await api.llm(); } catch { return; }
  fillDraft();
  render();
  check();
}

export async function askOnce() {
  if (store.get(ASKED_KEY)) return;
  await loadLlm();
  if (state.llm?.chosen) store.set(ASKED_KEY, "1");
  if (!state.llm || state.llm.chosen || !state.llm.here) return;
  store.set(ASKED_KEY, "1");
  go("settings");
}

export function bindLlm() {
  $("#llmModes").addEventListener("change", pick);
  $("#llmModelPick").addEventListener("change", pickModel);
  $("#llmModel").addEventListener("input", (e) => { draft.models[draft.mode] = e.target.value.trim(); });
  $("#llmModel").addEventListener("change", check);
  $("#llmKey").addEventListener("change", check);
  $("#llmTest").addEventListener("click", test);
  $("#llmSave").addEventListener("click", save);
  onLive((event) => {
    if (event.type !== "llm" || !state.llm) return;
    state.llm = { ...event.llm, here: state.llm.here };
    fillDraft();
    render();
  });
}
