import { api } from "./api.js";

const CHANNEL = "clippy-live";
const LOCK = "clippy-live";
const listeners = new Set();

export function onLive(fn) {
  listeners.add(fn);
}

const PARTS = new URL(import.meta.url).pathname.split("/");
const BUILD = PARTS.length === 5 ? PARTS[2] : "";
const SAFE_CHECK_MS = 2000;
let reloading = false;

function quiet() {
  const input = document.getElementById("input");
  return document.hidden || (!document.querySelector("dialog[open]") && !input?.value.trim());
}

function reloadWhenQuiet() {
  if (reloading) return;
  reloading = true;
  const tick = () => (quiet() ? location.reload() : setTimeout(tick, SAFE_CHECK_MS));
  document.addEventListener("visibilitychange", () => { if (document.hidden) location.reload(); });
  tick();
}

function dispatch(event) {
  if (event.type === "hello" && event.build && BUILD && event.build !== BUILD) reloadWhenQuiet();
  for (const fn of listeners) {
    try { fn(event); } catch (err) { console.error(err); }
  }
}

function connect(channel) {
  const source = api.events();
  source.onmessage = (e) => {
    const event = JSON.parse(e.data);
    dispatch(event);
    channel?.postMessage(event);
  };
  return source;
}

export function startLive() {
  const channel = "BroadcastChannel" in window ? new BroadcastChannel(CHANNEL) : null;
  if (!channel || !navigator.locks) {
    connect(null);
    return;
  }
  channel.onmessage = (e) => dispatch(e.data);
  navigator.locks.request(LOCK, () => new Promise(() => {
    const source = connect(channel);
    window.addEventListener("pagehide", () => source.close());
  }));
}
