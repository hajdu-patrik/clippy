import { api } from "./api.js";

const CHANNEL = "clippy-live";
const LOCK = "clippy-live";
const listeners = new Set();

export function onLive(fn) {
  listeners.add(fn);
}

function dispatch(event) {
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
