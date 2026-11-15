import { api } from "./api.js";
import { t, tx } from "./i18n.js";
import { $, esc, toast } from "./util.js";

let kit = null;

function options(select, values, current, label) {
  select.innerHTML = [`<option value="">${esc(t("kit.default"))}</option>`,
    ...values.map((v) => `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${esc(label(v))}</option>`)].join("");
}

function render() {
  if (!kit) return;
  options($("#kitFont"), kit.fonts, kit.font, (v) => v);
  options($("#kitHighlight"), kit.highlights, kit.highlight, (v) => t(`kit.color.${v}`));
  $("#kitPosition").innerHTML = kit.positions.map((v) =>
    `<option value="${v}" ${v === kit.position ? "selected" : ""}>${esc(t(`kit.pos.${v}`))}</option>`).join("");
  $("#kitUpper").value = kit.uppercase === null ? "" : String(kit.uppercase);
  for (const slot of ["logo", "outro"]) {
    const name = kit[slot]?.name;
    $(`#kit-${slot}-name`).textContent = name || t("kit.none");
    $(`#kit-${slot}-remove`).hidden = !name;
  }
}

export async function loadKit() {
  try { kit = await api.kit(); } catch { return; }
  render();
}

async function save() {
  const button = $("#kitSave");
  button.disabled = true;
  try {
    const upper = $("#kitUpper").value;
    kit = await api.saveKit({
      font: $("#kitFont").value, highlight: $("#kitHighlight").value, position: $("#kitPosition").value,
      uppercase: upper === "" ? null : upper === "true",
    });
    render();
    toast(t("kit.saved"));
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    button.disabled = false;
  }
}

async function upload(slot, input) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    kit = await api.kitFile(slot, file);
    render();
    toast(t("kit.uploaded", { name: file.name }));
  } catch (err) {
    toast(tx(err.t, err.message));
  } finally {
    input.value = "";
  }
}

async function remove(slot) {
  try {
    kit = await api.removeKitFile(slot);
    render();
  } catch (err) {
    toast(tx(err.t, err.message));
  }
}

export function bindKit() {
  $("#kitSave").addEventListener("click", save);
  for (const slot of ["logo", "outro"]) {
    $(`#kit-${slot}-file`).addEventListener("change", (e) => upload(slot, e.target));
    $(`#kit-${slot}-remove`).addEventListener("click", () => remove(slot));
  }
  document.addEventListener("clippy:lang", render);
}
