import { store } from "./util.js";

const KEY = "clippy-lang";
const SUPPORTED = ["hu", "en"];

const UI = {
  en: {
    "brand.home": "Clippy home",
    "side.close": "Close sidebar",
    "side.open": "Open sidebar",
    "side.chats": "Chats",
    "side.empty": "No chats yet.",
    "side.newChat": "New chat",
    "side.theme": "Light / dark theme",
    "side.lang": "Language",
    "side.rename": "Rename",
    "side.delete": "Delete",
    "side.running": "Running",
    "side.chatTitle": "Chat title",
    "side.renameFailed": "Rename failed: {error}",
    "side.deleteFailed": "Delete failed: {error}",
    "side.confirmDelete": "Delete this chat?\n\n“{title}”\n\nIts drafts and temporary files are removed. Finished clips stay in your video library.",
    "worker.idle": "Idle",
    "worker.working": "Working",
    "worker.queued": "{n} queued",
    "worker.more": " · +{n} queued",
    "worker.parallel": " · +{n} running",
    "empty.title": "What should we clip today?",
    "empty.text": "Paste a video link (YouTube, TikTok, Instagram, Twitch…) and describe the clips you want. Refine them right here in the chat: hook, length, style, CTA. Everything runs locally and free. Hungarian and English videos are both supported.",
    "chip.1": "5 clips with the strongest hooks",
    "chip.2": "3 funny clips, max 30 s",
    "chip.3": "Green highlights and a “Follow!” CTA",
    "chip.4": "Only the parts about GTA",
    "composer.label": "Message",
    "composer.attach": "Attach image, music, video or LUT",
    "attach.drop": "Drop files to attach",
    "attach.uploading": "Uploading…",
    "attach.remove": "Remove {name}",
    "attach.wait": "Wait until the uploads finish",
    "changes.title": "Clip #{n} · version {v}",
    "changes.undo": "Undo",
    "changes.undoing": "Restoring the previous version…",
    "composer.placeholder": "Paste a video link (YouTube, TikTok, Twitch…), or describe what you want…",
    "composer.placeholderShort": "Paste a link or ask…",
    "composer.send": "Send",
    "composer.hintKeys": "Enter to send · Shift+Enter for a new line · ",
    "composer.hint": "Finished clips go to your video library",
    "composer.error": "Error: {error}",
    "thread.toBottom": "Scroll to the newest message",
    "thread.thinking": "Thinking…",
    "thread.thinkingSec": "Thinking… {s}s",
    "thread.thinkingLong": "Still thinking… {s}s (the GPU may be busy)",
    "toast.ready": "Your clips are ready",
    "toast.failed": "The job stopped with an error",
    "title.ready": "✓ Ready",
    "title.error": "⚠ Error",
    "progress.steps": "Steps",
    "progress.log": "Log",
    "progress.failed": "Something went wrong",
    "progress.done": "Done",
    "progress.waiting": "Waiting in line",
    "progress.waitingAt": "Waiting · {pos} in line",
    "progress.afterCurrent": "Starts after the current job",
    "progress.starting": "Starting",
    "progress.estimating": "Estimating…",
    "progress.almost": "Almost done",
    "progress.minLeft": "≈ {m} min left",
    "progress.hLeft": "≈ {h} h {m} min left",
    "progress.took": "took {t}",
    "progress.elapsed": "{t} elapsed",
    "state.done": "done",
    "state.cached": "cached, skipped instantly",
    "state.skipped": "not needed",
    "state.failed": "failed",
    "state.active": "in progress",
    "state.pending": "waiting",
    "stage.download": "Download",
    "stage.audio": "Voice",
    "stage.transcribe": "Transcript",
    "stage.align": "Timing",
    "stage.select": "Picking",
    "stage.edit": "Editing",
    "stage.render": "Render",
    "card.aria": "Clip {n}, version {v}",
    "card.silence": "−{s} s silence",
    "card.score": "score {s}",
    "card.why": "Why this one?",
    "card.download": "Download",
    "card.copy": "Copy text",
    "card.edit": "Edit",
    "card.copied": "Post text copied",
    "card.risky": "check before posting",
    "card.altHook": "B hook (A/B test)",
    "card.question": "Comment question",
    "card.risks": "Check",
    "card.copyFailed": "Could not copy to the clipboard",
    "card.editPrefix": "Clip #{n}: ",
    "header.video": "Video: {title}",
  },
  hu: {
    "brand.home": "Clippy kezdőlap",
    "side.close": "Oldalsáv bezárása",
    "side.open": "Oldalsáv megnyitása",
    "side.chats": "Chatek",
    "side.empty": "Még nincs chat.",
    "side.newChat": "Új chat",
    "side.theme": "Világos / sötét téma",
    "side.lang": "Nyelv",
    "side.rename": "Átnevezés",
    "side.delete": "Törlés",
    "side.running": "Fut",
    "side.chatTitle": "Chat címe",
    "side.renameFailed": "Az átnevezés nem sikerült: {error}",
    "side.deleteFailed": "A törlés nem sikerült: {error}",
    "side.confirmDelete": "Törlöd ezt a chatet?\n\n„{title}”\n\nA piszkozatai és ideiglenes fájljai törlődnek. A kész klipek megmaradnak a videókönyvtáradban.",
    "worker.idle": "Szabad",
    "worker.working": "Dolgozik",
    "worker.queued": "{n} vár a sorban",
    "worker.more": " · +{n} a sorban",
    "worker.parallel": " · +{n} párhuzamosan fut",
    "empty.title": "Mit vágjunk ma?",
    "empty.text": "Illessz be egy videólinket (YouTube, TikTok, Instagram, Twitch…), és írd le, milyen klipeket szeretnél. Utána itt a chatben finomíthatod őket: hook, hossz, stílus, CTA. Minden helyben és ingyen fut. Magyar és angol videókkal is működik.",
    "chip.1": "5 klip a legerősebb hookokkal",
    "chip.2": "3 vicces klip, max. 30 mp",
    "chip.3": "Zöld kiemelés és „Kövess!” CTA",
    "chip.4": "Csak a GTA-ról szóló részek",
    "composer.label": "Üzenet",
    "composer.attach": "Kép, zene, videó vagy LUT csatolása",
    "attach.drop": "Engedd el a csatoláshoz",
    "attach.uploading": "Feltöltés…",
    "attach.remove": "{name} eltávolítása",
    "attach.wait": "Várd meg, amíg befejeződik a feltöltés",
    "changes.title": "#{n} klip · {v}. verzió",
    "changes.undo": "Visszavonás",
    "changes.undoing": "Az előző verzió visszaállítása…",
    "composer.placeholder": "Illessz be egy videólinket (YouTube, TikTok, Twitch…), vagy írd le, mit szeretnél…",
    "composer.placeholderShort": "Link vagy kérés…",
    "composer.send": "Küldés",
    "composer.hintKeys": "Enter: küldés · Shift+Enter: új sor · ",
    "composer.hint": "A kész klipek a videókönyvtáradba kerülnek",
    "composer.error": "Hiba: {error}",
    "thread.toBottom": "Ugrás a legújabb üzenethez",
    "thread.thinking": "Gondolkodom…",
    "thread.thinkingSec": "Gondolkodom… {s} mp",
    "thread.thinkingLong": "Még gondolkodom… {s} mp (a GPU lehet, hogy foglalt)",
    "toast.ready": "Elkészültek a klipjeid",
    "toast.failed": "A feladat hibával leállt",
    "title.ready": "✓ Kész",
    "title.error": "⚠ Hiba",
    "progress.steps": "Lépések",
    "progress.log": "Napló",
    "progress.failed": "Valami hiba történt",
    "progress.done": "Kész",
    "progress.waiting": "Vár a sorban",
    "progress.waitingAt": "Vár · {pos}. a sorban",
    "progress.afterCurrent": "Az aktuális feladat után indul",
    "progress.starting": "Indul",
    "progress.estimating": "Becslés…",
    "progress.almost": "Mindjárt kész",
    "progress.minLeft": "≈ {m} perc van hátra",
    "progress.hLeft": "≈ {h} óra {m} perc van hátra",
    "progress.took": "{t} alatt",
    "progress.elapsed": "{t} telt el",
    "state.done": "kész",
    "state.cached": "mentett, azonnal átugorva",
    "state.skipped": "nem kellett",
    "state.failed": "hiba",
    "state.active": "folyamatban",
    "state.pending": "vár",
    "stage.download": "Letöltés",
    "stage.audio": "Hang",
    "stage.transcribe": "Átirat",
    "stage.align": "Időzítés",
    "stage.select": "Válogatás",
    "stage.edit": "Szerkesztés",
    "stage.render": "Render",
    "card.aria": "{n}. klip, {v}. verzió",
    "card.silence": "−{s} mp csend",
    "card.score": "pontszám {s}",
    "card.why": "Miért ez?",
    "card.download": "Letöltés",
    "card.copy": "Szöveg másolása",
    "card.edit": "Szerkesztés",
    "card.copied": "Poszt szövege kimásolva",
    "card.risky": "posztolás előtt ellenőrizd",
    "card.altHook": "B hook (A/B teszt)",
    "card.question": "Komment-kérdés",
    "card.risks": "Ellenőrizd",
    "card.copyFailed": "Nem sikerült a vágólapra másolni",
    "card.editPrefix": "#{n} klip: ",
    "header.video": "Videó: {title}",
  },
};

const server = { en: {}, hu: {} };
let lang = initial();

function initial(fallback) {
  const saved = store.get(KEY);
  if (SUPPORTED.includes(saved)) return saved;
  if (SUPPORTED.includes(fallback)) return fallback;
  return (navigator.languages || [navigator.language]).some((l) => /^hu\b/i.test(l || "")) ? "hu" : "en";
}

function fill(text, params = {}) {
  const count = params.n ?? params.count;
  const all = { s: Number(count) === 1 ? "" : "s", ...params };
  return String(text).replace(/\{(\w+)\}/g, (m, k) => (all[k] ?? m));
}

export const getLang = () => lang;

export function t(key, params) {
  const text = UI[lang][key] ?? server[lang][key] ?? UI.en[key] ?? server.en[key] ?? key;
  return fill(text, params);
}

export function tx(obj, fallback = "") {
  if (!obj?.key) return fallback;
  const text = server[lang][obj.key] ?? server.en[obj.key];
  return text ? fill(text, obj.params) : fallback;
}

export function chatTitle(title) {
  if (title === "New chat") return t("chat.new");
  if (title === "Processing video…") return t("chat.processing");
  return title;
}

export function applyStatic(root = document) {
  document.documentElement.lang = lang;
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
    for (const pair of el.dataset.i18nAttr.split(";")) {
      const [attr, key] = pair.split(":");
      if (attr && key) el.setAttribute(attr.trim(), t(key.trim()));
    }
  });
}

export async function loadCatalog(fetchJson) {
  try {
    const data = await fetchJson();
    for (const code of SUPPORTED) Object.assign(server[code], data[code] || {});
    lang = initial(data.default);
  } catch {
    return;
  }
}

export function setLang(next) {
  lang = SUPPORTED.includes(next) ? next : "en";
  store.set(KEY, lang);
  applyStatic();
  document.dispatchEvent(new CustomEvent("clippy:lang", { detail: lang }));
}
