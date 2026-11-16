import { api } from "./api.js";
import { attachedAssets, attachedIds, bindAttachments, clearAttachments, uploading } from "./attachments.js";
import { t, tx } from "./i18n.js";
import { openChat, showEmpty } from "./chat.js";
import { clearPending, scrollBottom, showPending, upsertMessage } from "./messages.js";
import { loadChats } from "./sidebar.js";
import { state } from "./state.js";
import { $, toast } from "./util.js";

const MAX_HEIGHT = 220;

export function autoGrow() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, MAX_HEIGHT)}px`;
}

function prefill(text, append = false) {
  const input = $("#input");
  input.value = append && input.value ? `${input.value.trim()} ${text}` : text;
  input.focus();
  autoGrow();
}

const PROMPT_START = /^\s*(🎙|🎮|📣|⚡)/u;
const LINK = /https?:\/\/\S+/g;

function usePrompt(template) {
  const input = $("#input");
  const current = input.value.trim();
  const keep = PROMPT_START.test(current) ? (current.match(LINK) || []).join(" ") : current;
  prefill(template + keep);
  input.setSelectionRange(input.value.length, input.value.length);
  input.scrollTop = input.scrollHeight;
}

async function ensureChat() {
  if (state.current) return state.current;
  const chat = await api.createChat();
  if (!state.chats.some((c) => c.id === chat.id)) state.chats.unshift(chat);
  history.replaceState(null, "", `#/chat/${chat.id}`);
  await openChat(chat.id);
  return chat.id;
}

export async function send() {
  const text = $("#input").value.trim();
  if (!text || state.sending) return;
  if (uploading()) {
    toast(t("attach.wait"));
    return;
  }
  const attachments = attachedIds();
  const shown = attachedAssets();
  state.sending = true;
  $("#send").disabled = true;
  $("#send").classList.add("busy");
  try {
    const chatId = await ensureChat();
    $("#input").value = "";
    autoGrow();
    showEmpty(false);
    showPending(text, shown);
    clearAttachments();
    scrollBottom(true);
    const res = await api.send(chatId, text, attachments);
    if (state.current === chatId) {
      upsertMessage(res.user);
      upsertMessage(res.assistant);
    }
  } catch (err) {
    toast(t("composer.error", { error: tx(err.t, err.message) }));
  } finally {
    clearPending();
    state.sending = false;
    $("#send").disabled = false;
    $("#send").classList.remove("busy");
    await loadChats().catch(() => {});
    scrollBottom(true);
  }
}

const NARROW = matchMedia("(max-width: 560px)");
const TOUCH = matchMedia("(pointer: coarse)");

function fitPlaceholder() {
  $("#input").placeholder = t(NARROW.matches ? "composer.placeholderShort" : "composer.placeholder");
  autoGrow();
}

export function bindComposer() {
  bindAttachments(ensureChat);
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
  $("#input").addEventListener("input", autoGrow);
  NARROW.addEventListener("change", fitPlaceholder);
  fitPlaceholder();
  if (TOUCH.matches) $("#input").setAttribute("enterkeyhint", "enter");
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !TOUCH.matches) {
      e.preventDefault();
      send();
    }
  });
  $("#chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) usePrompt(t(`prompt.${chip.dataset.prompt}`));
  });
  document.addEventListener("clippy:compose", (e) => prefill(e.detail));
}
