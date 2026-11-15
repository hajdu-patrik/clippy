import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { $, esc } from "./util.js";

const ACCEPT = /^(image|audio|video)\//;
const ICONS = { image: "#i-image", audio: "#i-music", video: "#i-film", lut: "#i-palette" };
const items = new Map();
let counter = 0;
let ensureChat = null;

export function kindOf(file) {
  if (/\.cube$/i.test(file.name)) return "lut";
  const family = (file.type || "").split("/")[0];
  return ICONS[family] ? family : null;
}

export function iconFor(kind) {
  return `<svg aria-hidden="true"><use href="${ICONS[kind] || "#i-clip"}"/></svg>`;
}

export const attachedIds = () => [...items.values()].filter((i) => i.asset).map((i) => i.asset.id);
export const attachedAssets = () => [...items.values()].filter((i) => i.asset).map((i) => i.asset);
export const uploading = () => [...items.values()].some((i) => !i.asset && !i.error);

export function clearAttachments() {
  for (const item of items.values()) if (item.preview) URL.revokeObjectURL(item.preview);
  items.clear();
  render();
}

function render() {
  const box = $("#attachments");
  box.hidden = items.size === 0;
  box.replaceChildren(...[...items.entries()].map(([id, item]) => chip(id, item)));
}

function chip(id, item) {
  const el = document.createElement("div");
  el.className = `attachment${item.error ? " failed" : ""}${item.asset ? " ready" : ""}`;
  el.setAttribute("role", "listitem");
  const thumb = item.preview && item.kind === "image"
    ? `<img src="${esc(item.preview)}" alt="">` : iconFor(item.kind);
  const status = item.error ? esc(item.error) : item.asset ? "" : esc(t("attach.uploading"));
  el.innerHTML = `<span class="thumb">${thumb}</span>
    <span class="meta"><span class="name">${esc(item.file.name)}</span>${status ? `<span class="status">${status}</span>` : ""}
      <span class="bar" aria-hidden="true"><i></i></span></span>
    <button type="button" class="icon-btn remove" aria-label="${esc(t("attach.remove", { name: item.file.name }))}">
      <svg aria-hidden="true"><use href="#i-close"/></svg></button>`;
  el.style.setProperty("--p", String(item.progress));
  item.el = el;
  el.querySelector(".remove").addEventListener("click", () => {
    if (item.preview) URL.revokeObjectURL(item.preview);
    items.delete(id);
    render();
  });
  return el;
}

async function add(files) {
  const accepted = [...files].filter((f) => kindOf(f) && (ACCEPT.test(f.type || "") || /\.cube$/i.test(f.name)));
  if (!accepted.length) return;
  const chatId = await ensureChat();
  for (const file of accepted) {
    const id = ++counter;
    const kind = kindOf(file);
    const item = { file, kind, progress: 0, asset: null, error: null,
                   preview: kind === "image" ? URL.createObjectURL(file) : null };
    items.set(id, item);
    render();
    api.upload(chatId, file, (p) => {
      item.progress = p;
      item.el?.style.setProperty("--p", String(p));
    }).then((asset) => { item.asset = asset; item.progress = 1; })
      .catch((err) => { item.error = tx(err.t, err.message); })
      .finally(render);
  }
}

function bindDrop() {
  const overlay = $("#dropOverlay");
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    depth += 1;
    overlay.hidden = false;
  });
  document.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  document.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    add(e.dataTransfer.files);
  });
}

export function bindAttachments(chatProvider) {
  ensureChat = chatProvider;
  const input = $("#fileInput");
  $("#attach").addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    add(input.files);
    input.value = "";
  });
  $("#input").addEventListener("paste", (e) => {
    if (e.clipboardData?.files?.length) {
      e.preventDefault();
      add(e.clipboardData.files);
    }
  });
  bindDrop();
  document.addEventListener("clippy:lang", render);
}
