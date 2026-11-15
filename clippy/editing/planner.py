"""The LLM side of an edit: describe the clip, ask for operations in the catalogue's schema, dry-run them, and give
the model one chance to fix what failed (with the exact messages)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .. import i18n, playbook
from ..text import ends_sentence, no_dashes
from ..tools import ClippyError, log
from .assets import Asset, describe
from .editor import Editor, EditResult
from .operations import EditContext, Operation, OperationCatalogue
from .timeline import Timeline

MAX_TRANSCRIPT_CHARS = 1800
MAX_OPERATIONS = 10
QUOTED = re.compile(r'"([^"]{2,200})"|„([^”"]{2,200})[”"]|“([^”]{2,200})”')
VERBATIM_FIELDS = ("text", "title", "replace", "question")
SYSTEM_PROMPT = """You are the video editor inside "Clippy". You change ONE vertical short clip by returning edit
operations; the app validates and renders them. Return ONLY JSON: {"reply": "...", "operations": [...]}.

- "reply": 1-2 friendly sentences in {language} saying what you changed (or asking back).
- "operations": in the order to apply them. Use ONLY layer ids and attachment ids listed in the state.
- Times are seconds of the clip as the viewer sees it. When the user refers to words or a moment, use a time ref:
  {"type": "word", "word": "pénz"}, {"type": "phrase", "phrase": "senki nem tud"}, {"type": "edge", "edge": "end",
  "offset": -3} (the last 3 s), {"type": "at", "at": 4.5}. A span is {"start": ref, "end": ref} or {"start": ref,
  "length": seconds}; no span = the whole clip.
- The big text over the first seconds is the OPENING TEXT (layer "hook"; users may call it hook, title, "cím",
  "nyitó felirat", "felirat az elején", "fekete szöveg"). The hook itself is the clip's first 3 seconds: to make it
  stronger, start the clip on the funny, bold or surprising moment.
  The text at the end is the CTA (layer "cta"). Captions are the word-by-word subtitles (layer "captions").
- A misheard word in the captions ("nem X-et mond, hanem Y-t", "javítsd a feliratban") -> fix_caption with the
  words as they are written in the transcript below and what is really said; one operation per wrong word or phrase.
- To remove anything use remove_layer with its id ("intro" / "outro" for the bumpers).
- Music, logos, intros, B-roll videos and LUTs must come from the attachments; never invent ids. If the user wants
  music, a picture or a video but nothing fitting is attached, use {"kind": "ask", ...} to ask them to attach it.
  Built-in sound effects (pop, ding, boom, click, whoosh, riser, bleep), emoji stickers and effects need nothing.
- "Gyorsítsd fel / lassítsd le / slow motion" -> set_speed (2 = twice as fast, 0.5 = half speed) with a span when
  only a part is meant. "Fekvő / YouTube / négyzet / 16:9 / 1:1" -> set_format.
- "Fűzd hozzá a 3. klipet / tedd utána" -> join_clip with that clip number (only the other clips listed in the
  state). Several clips can be joined one after the other.
- Moments ("when he says X", "at the punchline") are time refs: put a zoom, flash, shake or sound effect there.
- "Csipogd ki / bleep" a word -> add_sound "bleep" at that word.
- Texts you write on the video use the captions' language unless the user asks otherwise; keep them short.
- Unclear or impossible request -> one {"kind": "ask", "question": "..."} operation and a reply that asks.
- Change only what was asked: usually 1-4 operations, never repeat the same operation.

OPERATIONS (each is an object with "kind" plus its fields; leave out fields you do not change):
{operations}"""

EXAMPLES = """Examples (ids are illustrative):
User: "Tedd alá ezt a zenét halkan, és vedd le a fekete címet" (attachment a1b2c3d4e5 audio "summer.mp3")
{"reply": "Betettem halkan a zenét, és levettem a hookot.", "operations": [
 {"kind": "add_music", "asset": "a1b2c3d4e5", "gain_db": -20}, {"kind": "remove_layer", "layer": "hook"}]}
User: "write 'Part 2 tomorrow' in the last 3 seconds and make the captions green"
{"reply": "Added the end text and switched the captions to green.", "operations": [
 {"kind": "add_text", "text": "Part 2 tomorrow", "position": "bottom",
  "span": {"start": {"type": "edge", "edge": "end", "offset": -3}}},
 {"kind": "style_captions", "highlight": "green"}]}
User: "vágd ki onnan, hogy 'na mindegy' addig, hogy 'szóval'"
{"reply": "Kivágtam azt a részt.", "operations": [
 {"kind": "cut_range", "span": {"start": {"type": "phrase", "phrase": "na mindegy"},
  "end": {"type": "word", "word": "szóval"}}}]}
User: "a feliratban nem Pistának, hanem Petinek mondja, és a hook legyen: Peti titka"
{"reply": "Javítottam a nevet a feliratban, és átírtam a hookot.", "operations": [
 {"kind": "fix_caption", "find": "Pistának", "replace": "Petinek"}, {"kind": "set_hook", "text": "Peti titka"}]}
User: "amikor azt mondja, hogy pénz, legyen ráközelítés és egy dörrenés, a végét lassítsd le, és tegyél fel egy 🔥-t"
{"reply": "Ráközelítettem és dörrenést tettem a „pénz” szóhoz, lelassítottam a végét, és felkerült a 🔥.",
 "operations": [{"kind": "add_effect", "effect": "zoom", "span": {"start": {"type": "word", "word": "pénz"}}},
 {"kind": "add_sound", "sound": "boom", "at": {"type": "word", "word": "pénz"}},
 {"kind": "set_speed", "factor": 0.5, "span": {"start": {"type": "edge", "edge": "end", "offset": -2}}},
 {"kind": "add_sticker", "emoji": "🔥", "position": "top-right", "motion": "bounce"}]}
User: "make it 16:9 for YouTube and put clip 3 after it"
{"reply": "Switched to 16:9 and joined clip 3 at the end.", "operations": [
 {"kind": "set_format", "format": "16:9"}, {"kind": "join_clip", "clip": 3}]}
User: "melegebb színek és egy kis kontraszt"
{"reply": "Melegebbek lettek a színek, és kicsit erősebb a kontraszt.", "operations": [
 {"kind": "color", "temperature": 5200, "contrast": 1.12}]}"""


@dataclass(frozen=True)
class EditRequest:
    text: str
    timeline: Timeline
    assets: tuple[Asset, ...]
    attached: tuple[str, ...] = ()
    words: tuple[dict, ...] = ()
    number: int = 1
    title: str = ""
    alt_hook: str = ""
    question: str = ""
    others: tuple[int, ...] = ()
    hook_idea: str = ""

    @property
    def language(self) -> str:
        return i18n.detect(self.text)


@dataclass
class PlanResult:
    reply: str
    result: EditResult | None
    operations: list[Operation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def control(self, kind: str) -> Operation | None:
        if self.result is None:
            return None
        return next((op for op in self.result.controls if op.kind == kind), None)


class PromptFormatter:
    def state(self, req: EditRequest) -> str:
        tl = req.timeline
        layers = [f"- {self._layer(layer, req)}" for layer in tl.layers] or ["- none"]
        parts = [f"CLIP #{req.number}, version {tl.version}, post title: {json.dumps(req.title, ensure_ascii=False)}",
                 *self._ideas(req), "Layers:", *layers,
                 f"Intro: {self._bumper(tl.intro, req)}. Outro: {self._bumper(tl.outro, req)}.",
                 f"Format: {tl.format}. Speed: {self._speed(tl)}. Joined after it: "
                 f"{', '.join(f'clip #{j.clip}' for j in tl.joins) or 'nothing'}. Other clips in this chat: "
                 f"{', '.join(f'#{n}' for n in req.others) or 'none'}.",
                 f"Camera: layout {tl.camera.layout}, punch-ins {'on' if tl.camera.punch else 'off'}, "
                 f"dead-air cutting {'on' if tl.camera.tighten else 'off'}. Sound: {tl.audio.mode}.",
                 "Attachments (use these ids):", self._assets(req) or "- none",
                 "Transcript (seconds as the viewer sees them):", self._transcript(req.words) or "(none)"]
        return "\n".join(parts)

    @staticmethod
    def _ideas(req: EditRequest) -> list[str]:
        shown = req.timeline.text_with_role("hook") is not None
        ideas = [("Suggested opening text (not on the video; set_hook puts it there)", "" if shown else req.hook_idea),
                 ("Alternative opening text for an A/B test (not on the video)", req.alt_hook),
                 ("Suggested comment question, usable as a CTA", req.question)]
        return [f"{label}: {json.dumps(text, ensure_ascii=False)}" for label, text in ideas if text]

    @staticmethod
    def _layer(layer, req: EditRequest) -> str:
        span = f" {layer.span.start:.1f}-{layer.span.end:.1f} s" if layer.span else ""
        if layer.kind == "text":
            where = {"hook": " (first 3 s)", "cta": " (last 3 s)"}.get(layer.role, "")
            return f"{layer.id}: {layer.role} text {json.dumps(layer.text, ensure_ascii=False)}{where}{span}"
        if layer.kind == "captions":
            state = "visible" if layer.visible else "hidden"
            return (f"{layer.id}: captions {state}, highlight {layer.highlight}, "
                    f"{'UPPERCASE' if layer.uppercase else 'normal case'}, language {layer.language or 'spoken'}")
        if layer.kind == "image":
            return f"{layer.id}: image {json.dumps(layer.name)} opacity {layer.transform.opacity}{span}"
        if layer.kind == "audio":
            return f"{layer.id}: music {json.dumps(layer.name)} {layer.gain_db:+g} dB{' ducked' if layer.duck else ''}{span}"
        if layer.kind == "sticker":
            return f"{layer.id}: sticker {layer.emoji} ({layer.motion}){span}"
        if layer.kind == "effect":
            return f"{layer.id}: effect {layer.effect}{span or ' whole clip'}"
        if layer.kind == "sound":
            return f"{layer.id}: sound effect {layer.sound or json.dumps(layer.name)} at {layer.at:.1f} s"
        if layer.kind == "video":
            return f"{layer.id}: B-roll {json.dumps(layer.name)} {'full frame' if layer.full else 'small'}{span}"
        a = layer.adjust
        return (f"{layer.id}: colour contrast {a.contrast}, saturation {a.saturation}, brightness {a.brightness}, "
                f"temperature {a.temperature} K, vibrance {a.vibrance}{', LUT ' + layer.lut if layer.lut else ''}")

    @staticmethod
    def _speed(tl: Timeline) -> str:
        parts = [f"×{r.factor:g}" + (f" {r.span.start:.1f}-{r.span.end:.1f} s" if r.span else " whole clip")
                 for r in tl.speed]
        return ", ".join(parts) or "normal"

    @staticmethod
    def _bumper(bumper, req: EditRequest) -> str:
        if bumper is None:
            return "none"
        name = next((a.name for a in req.assets if a.id == bumper.asset), bumper.asset)
        return f"{json.dumps(name)} with {bumper.transition.kind}"

    @staticmethod
    def _assets(req: EditRequest) -> str:
        lines = describe(req.assets).splitlines()
        return "\n".join(f"- {line}{'  [attached to this message]' if line.split()[0] in req.attached else ''}"
                         for line in lines)

    @staticmethod
    def _transcript(words) -> str:
        lines, cur, start = [], [], None
        for w in words:
            if start is None:
                start = w["start"]
            cur.append(w["word"])
            if ends_sentence(w["word"]) or len(cur) >= 18:
                lines.append(f"[{start:.1f}] {' '.join(cur)}")
                cur, start = [], None
        if cur:
            lines.append(f"[{start:.1f}] {' '.join(cur)}")
        text = "\n".join(lines)
        return text if len(text) <= MAX_TRANSCRIPT_CHARS else text[:MAX_TRANSCRIPT_CHARS] + " …"


class EditPlanner:
    def __init__(self, llm, catalogue: OperationCatalogue | None = None, formatter: PromptFormatter | None = None,
                 editor: Editor | None = None):
        self.llm = llm
        self.catalogue = catalogue or OperationCatalogue()
        self.formatter = formatter or PromptFormatter()
        self.editor = editor or Editor()

    @staticmethod
    def schema() -> dict:
        """Only the envelope is constrained: a full union schema makes constrained decoding derail the model, while
        the catalogue in the prompt plus validation and one correction round keep the operations right."""
        return {"type": "object", "properties": {
            "reply": {"type": "string"},
            "operations": {"type": "array", "maxItems": MAX_OPERATIONS,
                           "items": {"type": "object", "additionalProperties": True}},
        }, "required": ["reply", "operations"]}

    def plan(self, req: EditRequest, ctx: EditContext) -> PlanResult:
        system = (SYSTEM_PROMPT.replace("{language}", i18n.NAMES[req.language])
                  .replace("{operations}", self.catalogue.describe()) + "\n\n" + playbook.editor_rules()
                  + "\n" + playbook.DASH_RULE
                  + "\n\n" + EXAMPLES)
        prompt = f"STATE:\n{self.formatter.state(req)}\n\nUSER REQUEST:\n{req.text}"
        attempt = self._attempt(system, prompt, req, ctx)
        if attempt.errors:
            log(f"[edit] fixing {len(attempt.errors)} problem(s) in the plan")
            fix = (prompt + "\n\nYOUR PREVIOUS OPERATIONS:\n" + json.dumps([op.model_dump(mode="json") for op in
                   attempt.operations], ensure_ascii=False) + "\n\nTHEY FAILED:\n" + "\n".join(attempt.errors)
                   + "\nReturn corrected JSON for the whole request.")
            attempt = self._attempt(system, fix, req, ctx)
        return attempt

    def _attempt(self, system: str, prompt: str, req: EditRequest, ctx: EditContext) -> PlanResult:
        log("[edit] planning the edit")
        try:
            reply = self.llm.chat_json(system, prompt, think=False, schema=self.schema())
        except ClippyError as exc:
            return PlanResult(reply="", result=None, errors=[str(exc)])
        reply = reply if isinstance(reply, dict) else {}
        items = verbatim(written(unique(reply.get("operations"))[:MAX_OPERATIONS]), req.text)
        ops, errors = self.catalogue.parse(items)
        result = self.editor.run(req.timeline, ops, ctx) if ops else None
        if result is not None:
            errors += [issue.for_model() for issue in result.issues]
        return PlanResult(reply=str(reply.get("reply") or "").strip(), result=result, operations=ops, errors=errors)


def written(items: list) -> list:
    """Texts the model writes for the video or the post have no long dashes (see text.no_dashes); quotes the user
    gave are restored letter for letter afterwards by `verbatim`."""
    return [{**item, **{k: no_dashes(item[k]) for k in ("text", "title") if isinstance(item.get(k), str)}}
            if isinstance(item, dict) else item for item in items]


def unique(items) -> list:
    """Drops repeated operations (constrained decoding sometimes loops on one)."""
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def verbatim(items: list, request: str) -> list:
    """Texts the user quoted go on the video letter for letter: a small model copying a quote sometimes changes a
    letter or two ("lebánult" for "lebénult"), so a near copy of a quote is replaced by the quote itself."""
    quotes = [next(g for g in m.groups() if g) for m in QUOTED.finditer(request)]
    if not quotes:
        return items
    out = []
    for item in items:
        if isinstance(item, dict):
            item = dict(item)
            for key in VERBATIM_FIELDS:
                value = item.get(key)
                if isinstance(value, str) and value not in quotes:
                    best = max(quotes, key=lambda q: SequenceMatcher(None, q.lower(), value.lower()).ratio())
                    if SequenceMatcher(None, best.lower(), value.lower()).ratio() >= 0.85:
                        item[key] = best
        out.append(item)
    return out
