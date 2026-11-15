import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { onLive } from "./live.js";
import { $, esc, toast } from "./util.js";

let found = null;

function valueText(f) {
  if (f.dimension === "hook") return t(`hooktype.${f.value}`);
  if (f.dimension === "kind") return t(`results.kind.${f.value}`);
  if (f.dimension === "hour") return t(`results.hour.${f.value}`);
  return f.value;
}

function findingRow(f) {
  const detail = [t("results.count", { n: f.count }),
    f.watched != null ? t("results.watchedShort", { p: Math.round(100 * f.watched) }) : ""].filter(Boolean).join(" · ");
  return `<li class="finding ${f.factor >= 1 ? "up" : "down"}">
    <span>${esc(t(`results.dim.${f.dimension}`, { value: valueText(f) }))}<small>${esc(detail)}</small></span>
    <b>${esc(t("results.factor", { x: new Intl.NumberFormat(document.documentElement.lang || undefined).format(f.factor) }))}</b></li>`;
}

function render() {
  const list = $("#resultsList");
  if (!list || !found) return;
  const summary = $("#resultsSummary");
  if (!found.measured) summary.textContent = t("results.none");
  else if (found.compared < 5) summary.textContent = t("results.few", { n: found.measured });
  else if (!found.findings.length) summary.textContent = t("results.nothing", { n: found.compared });
  else summary.textContent = t("results.summary", { n: found.compared });
  list.innerHTML = found.findings.map(findingRow).join("");
  $("#resultsAnalytics").hidden = found.analytics !== false;
}

export async function loadResults() {
  try { found = await api.results(); } catch { return; }
  render();
}

async function refresh() {
  const button = $("#resultsRefresh");
  button.disabled = true;
  try {
    const answer = await api.refreshResults();
    found = { ...found, ...answer };
    render();
    toast(t("results.read", { n: answer.read }));
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

export function bindResults() {
  $("#resultsRefresh").addEventListener("click", refresh);
  onLive((event) => {
    if (event.type === "results" && $("#settingsDialog").open) loadResults();
  });
  document.addEventListener("clippy:lang", render);
}
