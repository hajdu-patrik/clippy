import { api } from "./api.js";
import { getLang, t, tx } from "./i18n.js";
import { $, toast } from "./util.js";

let recorder = null;
let chunks = [];
let started = 0;
const MAX_MS = 120000;

function supported() {
  return Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
}

function setRecording(on) {
  const button = $("#voiceBtn");
  button.classList.toggle("recording", on);
  button.setAttribute("aria-pressed", String(on));
  button.setAttribute("aria-label", t(on ? "voice.stop" : "voice.start"));
  button.title = t(on ? "voice.stop" : "voice.start");
}

async function start() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast(t("voice.denied"));
    return;
  }
  chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  recorder.onstop = () => {
    stream.getTracks().forEach((track) => track.stop());
    send(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
  };
  recorder.start();
  started = Date.now();
  setRecording(true);
  setTimeout(() => { if (recorder?.state === "recording" && Date.now() - started >= MAX_MS) stop(); }, MAX_MS);
}

function stop() {
  if (recorder?.state === "recording") recorder.stop();
  setRecording(false);
}

async function send(blob) {
  recorder = null;
  if (blob.size < 2000) return;
  const button = $("#voiceBtn");
  button.disabled = true;
  button.classList.add("busy");
  try {
    const { text } = await api.dictate(blob, getLang());
    if (!text) {
      toast(t("voice.nothing"));
      return;
    }
    const input = $("#input");
    input.value = input.value.trim() ? `${input.value.trim()} ${text}` : text;
    input.dispatchEvent(new Event("input"));
    input.focus();
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
    button.classList.remove("busy");
  }
}

export function bindVoice() {
  const button = $("#voiceBtn");
  if (!supported()) {
    button.hidden = true;
    return;
  }
  setRecording(false);
  button.addEventListener("click", () => (recorder ? stop() : start()));
  document.addEventListener("clippy:lang", () => setRecording(Boolean(recorder)));
}
