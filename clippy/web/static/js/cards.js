import { t } from "./i18n.js";
import { state } from "./state.js";
import { $, copyText, esc, fmtTs, toast } from "./util.js";

function whyHtml(d) {
  const rows = [
    d.reason && ["", d.reason],
    d.hook && [t("card.hookIdea"), d.hook],
    d.alt_hook && [t("card.altHook"), d.alt_hook],
    d.question && [t("card.question"), d.question],
    (d.risks || []).length && [t("card.risks"), d.risks.map((r) => t(`risk.${r}`)).join("; ")],
  ].filter(Boolean);
  if (!rows.length) return "";
  const body = rows.map(([label, text]) => `<p>${label ? `<b>${esc(label)}:</b> ` : ""}${esc(text)}</p>`).join("");
  return `<details><summary>${esc(t("card.why"))}</summary>${body}</details>`;
}

function postCaption(c) {
  const fromServer = (c.txt || "").split("\n---\n")[0].trim();
  if (fromServer) return fromServer;
  const d = c.clip || {};
  return [d.description || d.title, d.question, (d.hashtags || []).join(" ")].filter(Boolean).join("\n\n");
}

export function cardHtml(id) {
  const c = state.clips.get(id);
  if (!c) return "";
  const d = c.clip || {}, info = c.info || {};
  const duration = Number(info.duration_out ?? (d.end - d.start)) || 0;
  const removed = info.dead_air_removed
    ? `<span>${esc(t("card.silence", { s: Number(info.dead_air_removed).toFixed(1) }))}</span>` : "";
  const old = c.current === false;
  const fmt = ["16:9", "1:1", "4:5"].includes(info.format) ? info.format : "";
  const kind = d.hook_type ? `<span class="kind">${esc(t(`hooktype.${d.hook_type}`))}</span>` : "";
  const risky = (d.risks || []).length ? `<span class="risk">${esc(t("card.risky"))}</span>` : "";
  return `<article class="card ${old ? "old" : ""} ${c.deleted ? "deleted" : ""}" data-cid="${c.id}" data-num="${c.number}">
    <div class="vid${fmt ? ` fmt-${fmt.replace(":", "x")}` : ""}">
      <video controls preload="metadata" playsinline ${c.jpg ? `poster="${esc(c.jpg)}"` : ""} src="${esc(c.mp4 || "")}"
        aria-label="${esc(t("card.aria", { n: c.number, v: c.version }))}"></video>
      <span class="badge ${old ? "" : "cur"}">#${c.number} · v${c.version}</span>
    </div>
    <div class="meta">
      <h3 class="hook">${esc(d.title || d.hook || "")}</h3>
      ${d.description ? `<p class="desc">${esc(d.description)}</p>` : ""}
      <div class="facts"><span>${fmtTs(d.start)}-${fmtTs(d.end)}</span><span>${duration.toFixed(1)} s</span>
        ${d.score != null ? `<span>${esc(t("card.score", { s: Math.round(d.score) }))}</span>` : ""}${removed}${fmt ? `<span>${esc(fmt)}</span>` : ""}${kind}${risky}</div>
      ${(d.hashtags || []).length ? `<div class="tags">${esc(d.hashtags.join(" "))}</div>` : ""}
      ${whyHtml(d)}
      <div class="btns">
        <a href="${esc(c.mp4 || "#")}" download>${esc(t("card.download"))}</a>
        <button type="button" data-act="copy">${esc(t("card.copy"))}</button>
        <button type="button" data-act="edit">${esc(t("card.edit"))}</button>
      </div>
    </div>
  </article>`;
}

function showCopyDialog(text) {
  const area = $("#copyText");
  area.value = text;
  $("#copyDialog").showModal();
  area.focus();
  area.select();
}

export function bindCopyDialog() {
  $("#copyClose").addEventListener("click", () => $("#copyDialog").close());
}

async function onCardClick(event) {
  const act = event.target.closest("button")?.dataset.act;
  const card = event.target.closest(".card");
  if (!act || !card) return;
  const clip = state.clips.get(Number(card.dataset.cid));
  if (act === "copy") {
    const text = postCaption(clip);
    if (await copyText(text)) {
      toast(t("card.copied"));
    } else {
      showCopyDialog(text);
    }
  } else if (act === "edit") {
    document.dispatchEvent(new CustomEvent("clippy:compose", { detail: t("card.editPrefix", { n: clip.number }) }));
  }
}

export function bindCards(root) {
  if (root.dataset.cardsBound) return;
  root.dataset.cardsBound = "1";
  root.addEventListener("click", onCardClick);
}

export function appendCards(grid, ids) {
  const have = new Set([...grid.querySelectorAll(".card")].map((x) => Number(x.dataset.cid)));
  for (const id of ids) {
    if (!have.has(id)) grid.insertAdjacentHTML("beforeend", cardHtml(id));
  }
}

export function markOldVersions(newClip) {
  for (const c of state.clips.values()) {
    if (c.number === newClip.number && c.id !== newClip.id) c.current = false;
  }
  document.querySelectorAll(`.card[data-num="${newClip.number}"]`).forEach((card) => {
    if (Number(card.dataset.cid) !== newClip.id) {
      card.classList.add("old");
      card.querySelector(".badge")?.classList.remove("cur");
    }
  });
}

export function markDeleted(number) {
  for (const c of state.clips.values()) if (c.number === number) c.deleted = true;
  document.querySelectorAll(`.card[data-num="${number}"]`).forEach((card) => card.classList.add("deleted"));
}
