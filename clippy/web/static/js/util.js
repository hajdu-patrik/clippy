export const $ = (selector) => document.querySelector(selector);

const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ENTITIES[c]);

export function md(text) {
  const inline = (t) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])[_*]([^_*\n]+)[_*](?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
    .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
  return esc(text).trim().split(/\n{2,}/).map((block) => {
    const lines = block.split("\n");
    if (lines.every((l) => /^\s*[-•]\s+/.test(l))) {
      return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-•]\s+/, ""))}</li>`).join("") + "</ul>";
    }
    return `<p>${lines.map(inline).join("<br>")}</p>`;
  }).join("");
}

export function fmtTs(seconds) {
  const sec = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60), h = Math.floor(m / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m % 60)}:${pad(s)}` : `${m}:${pad(s)}`;
}

let toastTimer;
export function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return copyBySelection(text);
    }
  }
  return copyBySelection(text);
}

function copyBySelection(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.readOnly = true;
  area.className = "copy-buffer";
  document.body.append(area);
  area.select();
  area.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  area.remove();
  return ok;
}

export const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { return undefined; } },
};
