# Design: the chat-driven editor

Status: proposal · Target: Clippy 2.x · Owner: Clippy contributors

Clippy cuts clips today; this design turns every clip into an **editable timeline** that you change by talking to it –
typed or spoken, in Hungarian or English, with your own images, music, videos and logos attached. *"Put this song
under it quietly, add my intro with a fade, take the black title off, write 'Part 2 tomorrow' at the end and warm up
the colours"* becomes a list of validated edit operations, a new version, a 3-second preview and a final render.

## 1. What comparable tools do (November 2026)

| Capability | OpusClip | Descript | Submagic | CapCut | Captions | VEED / Kapwing | Clippy 1.0 | This design |
|---|---|---|---|---|---|---|---|---|
| Find viral moments, score them | ✓ | ✓ | – | – | – | ✓ (Kapwing) | ✓ | ✓ |
| Speaker-tracking reframe | ✓ | ✓ | – | ✓ | – | ✓ | ✓ | ✓ + 1:1 / 16:9 |
| Animated caption templates, emoji, keyword colours | ✓ | ✓ | ✓ (28 styles) | ✓ | ✓ | ✓ | basic | ✓ template library |
| Silence and filler-word removal | – | ✓ | – | ✓ | – | ✓ (Smart Cut) | ✓ | ✓ |
| Edit by editing the transcript | text + timeline | ✓ (core) | – | – | – | – | – | ✓ |
| Chat / prompt editing | – | ✓ (Underlord) | – | – | ✓ (AI Edit) | ✓ | partial | ✓ full |
| B-roll (own uploads, stock) | ✓ (Pexels) | ✓ | ✓ (+ frequency slider) | ✓ | ✓ | ✓ | – | ✓ own + word-anchored |
| Auto zoom / punch-ins | ✓ | – | ✓ (Magic Zoom) | ✓ | ✓ | – | ✓ | ✓ + on request |
| Background music, ducking | ✓ | ✓ | ✓ | ✓ (beat sync) | ✓ | ✓ | ducking only | ✓ + beat-synced cuts |
| Sound effects at key moments | – | – | ✓ | ✓ | ✓ | – | – | ✓ local library |
| Transitions, intro / outro | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | ✓ |
| Clean audio / noise removal | – | ✓ (Studio Sound) | – | ✓ | ✓ | ✓ | voice chain | ✓ RNNoise / Demucs |
| Colour, LUT, filters | – | some | – | ✓ | – | ✓ | – | ✓ |
| Captions in another language | ✓ | ✓ | ✓ | ✓ | ✓ (dubbing) | ✓ | ✓ (hu ↔ en) | ✓ |
| Brand kit (fonts, colours, logo, intro) | ✓ | ✓ | – | ✓ | – | ✓ | – | ✓ |
| Remembers your editing preferences | – | ✓ | – | – | – | – | – | ✓ |
| Export to Premiere / Resolve (XML / OTIO) | ✓ (reported) | ✓ | – | – | – | – | – | ✓ OTIO |
| Agent / MCP access | – | ✓ (API, MCP) | – | – | ✓ (MCP) | – | – | ✓ MCP server (phase 4) |
| Eye contact, AI avatars, dubbing with voice clone | – | ✓ | – | ✓ | ✓ | ✓ | – | out of scope (phase 4+) |
| Runs locally, free, private | – | – | – | – | – | – | ✓ | ✓ |

Open-source agent editors converge on the same architecture: **a JSON timeline is the source of truth, every agent
edit is a validated operation appended to a log, and the renderer compiles the timeline into an FFmpeg filter graph**
(SynthCut, OpenCardboard, MakeMyClip, ffmpeg-llm, Orkas VideoStudio). This design follows that pattern and adds what
Clippy is good at: word-level timing, the virtual camera and a fully local stack.

## 2. Goals and non-goals

Goals
- Any common short-form edit can be asked for in natural language, Hungarian or English, typed or spoken.
- Attachments (image, logo, audio, video, LUT, font) are first-class inputs that the edit can reference.
- Every edit is a new, undoable version; the change is shown in plain words before and after rendering.
- Edits that do not touch the cut or the camera re-render in seconds, not a minute.
- The LLM never produces FFmpeg syntax: only typed operations that are validated and compiled by code.
- Clean, layered, testable code: one responsibility per class, registries instead of `if` chains, pure compilers.

Non-goals (for now): generative video (AI B-roll generation), face-swap / eye-contact, voice cloning, cloud
publishing, collaborative editing.

## 3. User-facing behaviour

```
You:    🎤 "Tedd alá ezt a zenét halkan, a végére a csatolt outrót fade-del, és vedd le a fekete címet."
        📎 summer.mp3  📎 outro.mp4
Clippy: Done – v3 of clip #2:
        + Music: summer.mp3 at −16 dB, ducked under speech, fades in 1 s / out 2 s
        + Outro: outro.mp4 after the clip, 0.5 s fade
        − Hook text "Elvesztettem magam – miért?"
        [▶ Preview 540p]  [Render final]  [Undo]
```

- **Attachments:** `+` button, drag-and-drop or paste into the composer; chips with thumbnail / waveform; referenced
  as "this song", "the second image", "my logo" (brand kit).
- **Microphone:** hold-to-talk button; local Whisper turns speech into text in the composer for a quick check, then send.
- **Transcript editing:** a "Transcript" tab on a clip card; striking words out cuts them (Descript-style).
- **Change card:** every edit lists added / changed / removed items; one click undo restores the previous version.
- **Preview first:** cheap 540p preview renders automatically after an edit; the final render is one click (or automatic
  when nothing else is queued).

## 4. Architecture

```
                 ┌──────────── web (FastAPI) ─────────────┐
 composer ──────►│ ChatService ─► IntentRouter            │
 mic (webm) ────►│ SpeechService (Whisper small, CPU)     │
 uploads ───────►│ AssetService (sniff, probe, normalise) │
                 └──────────────┬─────────────────────────┘
                                ▼
                 EditPlanner (LLM) ─► list[Operation] ─► OperationValidator ─► AnchorResolver
                                                                          ▼
                 TimelineRepository ◄── Timeline.apply(ops) → Timeline v(n+1) + ChangeSet
                                ▼
                 RenderPlanner ── needs base? ──► BaseRenderer (cut + camera)  ─► base.mkv (cached)
                                └──────────────► FinishingRenderer (layers)    ─► preview.mp4 / final.mp4
                                                     │
                                                     └─ FilterGraphCompiler + LayerCompilers (strategy per layer)
```

### 4.1 Package layout

```
clippy/editing/
  __init__.py
  timeline.py        Timeline, Track, Layer subclasses, TimeRange, Transform, Style   (pure domain model)
  timeref.py         TimeRef (absolute | word | phrase | edge) + AnchorResolver
  operations/
    __init__.py      Operation base class, registry, discriminated union, JSON schema for the LLM
    audio.py         AddMusic, SetVolume, Duck, CleanAudio, AddSoundEffect, RemoveAudio
    text.py          AddText, EditText, RemoveText, CaptionStyle, HideCaptions, TranslateCaptions
    visual.py        AddOverlay, AddBroll, Color, ApplyLut, Zoom, BlurRegion, RemoveBurnedText
    structure.py     CutRange, Trim, Speed, SetIntro, SetOutro, Transition, Reframe, Aspect
    meta.py          Ask (clarifying question), Undo, ApplyPreset, SavePreference
  assets.py          Asset, AssetKind, AssetService (validation, normalisation, probe), AssetRepository
  planner.py         EditPlanner (prompt, schema, self-correction), PlanResult
  validation.py      OperationValidator (semantic checks against timeline + assets)
  changes.py         ChangeSet, Change, ChangeFormatter (i18n)
  presets.py         CaptionTemplate, BrandKit, TransitionCatalog, LutLibrary, PreferenceStore
  render/
    planner.py       RenderPlanner: which stages a new version needs (hash based)
    base.py          BaseRenderer (wraps today's ClipRenderer: cut, camera, speech audio)
    finishing.py     FinishingRenderer: base + layers → mp4 (profiles: preview, final)
    graph.py         FilterGraph builder (labelled pads, escaping), Input registry
    layers.py        LayerCompiler strategies: TextCompiler, OverlayCompiler, ColorCompiler, AudioCompiler,
                     TransitionCompiler, SequenceCompiler (intro / outro)
    profiles.py      RenderProfile (preview 540p NVENC p1, final 1080p NVENC p7 CQ17)
  export/otio.py     OTIO export (optional dependency)
  speech.py          SpeechService: short dictation → text (hu / en)
```

Existing modules keep their role: `pipeline.Project` stays the façade for selection and base rendering;
`render.ClipRenderer` becomes the core of `BaseRenderer`; captions, hook and CTA move from the base render into
finishing layers so restyling them no longer redoes the camera.

### 4.2 Domain model (`timeline.py`)

Immutable pydantic models; every edit returns a new `Timeline`. Times are seconds on the **output** timeline of the
clip (after the tight edit), so "the last 3 seconds" means what the viewer sees.

```python
class TimeRange(Frozen):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

class Transform(Frozen):
    x: float = 0.5            # centre, 0..1 of the 1080×1920 frame
    y: float = 0.5
    width: float = 0.3        # fraction of frame width
    rotation: float = 0
    opacity: float = Field(1, ge=0, le=1)

class Layer(Frozen):
    id: str                   # stable, short ("txt-3"), referenced by later edits
    span: TimeRange | None    # None = the whole clip
    name: str = ""            # what the user called it ("black title", "logo")

class TextLayer(Layer):       kind: Literal["text"];    text: str; style: TextStyle; transform: Transform
class CaptionLayer(Layer):    kind: Literal["captions"]; template: str; language: str | None; visible: bool = True
class ImageLayer(Layer):      kind: Literal["image"];   asset: str; transform: Transform; animation: Animation | None
class VideoLayer(Layer):      kind: Literal["video"];   asset: str; source: TimeRange; transform: Transform | None
class ColorLayer(Layer):      kind: Literal["color"];   adjust: ColorAdjust | None; lut: str | None; strength: float
class AudioLayer(Layer):      kind: Literal["audio"];   asset: str; gain_db: float; duck: Ducking | None; fades: Fades

class Cut(Frozen):            source: TimeRange; removed: list[TimeRange]; speed: list[SpeedRamp]
class Sequence(Frozen):       intro: Bumper | None; outro: Bumper | None    # Bumper = asset + transition + duration

class Timeline(Frozen):
    version: int
    cut: Cut                  # what part of the source, which words removed – drives the BASE render
    camera: CameraSettings    # layout, punch-ins, aspect – drives the BASE render
    layers: tuple[Layer, ...] # drawn in order – drives the FINISHING render
    sequence: Sequence
    audio: AudioMaster        # speech gain, clean-up, loudness target

    def apply(self, ops: Sequence[Operation], ctx: EditContext) -> tuple["Timeline", ChangeSet]: ...
    def base_key(self) -> str:      # hash of cut + camera + source; same key → reuse base.mkv
    def finish_key(self) -> str:    # hash of everything
```

`Layer` is a discriminated union (`kind`); new layer types are new classes, not new branches (Open/Closed).

### 4.3 Time references (`timeref.py`)

Users speak in words, not seconds. A `TimeRef` is one of:

| Form | Example request | Resolved by |
|---|---|---|
| `{"at": 12.5}` | "at 12.5 s" | as is |
| `{"word": "pénz", "occurrence": 1}` | "when he says 'pénz'" | word timings mapped through the cut |
| `{"phrase": "senki nem tud"}` | "from 'senki nem tud'" | fuzzy match over the transcript |
| `{"edge": "end", "offset": -3}` | "the last 3 seconds" | clip duration |
| `{"sentence": 2}` | "during the second sentence" | sentence boundaries |

`AnchorResolver` turns them into output-time seconds and reports unresolvable anchors as validation errors the LLM
can fix ("there is no 'pénz' in clip #2; closest: 'pénzt' at 7.4 s").

### 4.4 Operations (`operations/`)

The **Command** pattern: each operation is a small pydantic model with `kind`, typed fields and an `apply` method.

```python
class Operation(Frozen, ABC):
    kind: str
    clip: int | Literal["all"] = "current"

    @abstractmethod
    def apply(self, tl: Timeline, ctx: EditContext) -> tuple[Timeline, list[Change]]: ...

    def touches_base(self) -> bool:      # does it change the cut or the camera?
        return False

REGISTRY: dict[str, type[Operation]] = {}

def operation(cls):                      # class decorator: one place to register
    REGISTRY[cls.model_fields["kind"].default] = cls
    return cls

@operation
class AddMusic(Operation):
    kind: Literal["add_music"] = "add_music"
    asset: AssetRef
    gain_db: float = Field(-16, ge=-40, le=0)
    duck: bool = True
    fade_in: float = Field(1, ge=0, le=10)
    fade_out: float = Field(2, ge=0, le=10)
    span: TimeRefRange | None = None

    def apply(self, tl, ctx):
        layer = AudioLayer(id=ctx.new_id("music"), asset=ctx.assets.require(self.asset, AssetKind.AUDIO).id, ...)
        return tl.with_layer(layer), [Change.added("music", layer)]
```

`Operation.schema()` builds one JSON schema (a `oneOf` over `REGISTRY`, discriminated by `kind`) that is passed to
Ollama's structured output, so the model can only emit known operations with typed fields. The same models validate
the reply; validation errors go back to the model once for self-correction (section 5).

Catalogue (first three phases):

| Group | Operations |
|---|---|
| Audio | `add_music`, `set_volume`, `duck`, `fade_audio`, `clean_audio` (RNNoise / Demucs voice), `add_sfx` (at a TimeRef), `remove_audio_layer`, `mute_range` (bleep) |
| Text | `add_text`, `edit_text`, `remove_text`, `move_text`, `caption_style` (template, font, colours, position, uppercase, emoji), `hide_captions`, `translate_captions`, `highlight_words`, `set_hook`, `set_cta` |
| Visual | `add_overlay` (image / logo with position, animation), `add_broll` (asset or still at a TimeRef, full / PiP), `color` (brightness, contrast, saturation, temperature, vibrance, gamma), `apply_lut`, `zoom` (punch-in at TimeRef), `blur_region` / `censor`, `vignette`, `sharpen`, `denoise`, `stabilise`, `remove_burned_text` |
| Structure | `cut_range`, `cut_words` (transcript edit), `trim`, `speed`, `set_intro`, `set_outro`, `transition` (between internal jump cuts or bumpers), `reframe` (layout, follow subject), `aspect` (9:16, 1:1, 4:5, 16:9), `progress_bar` |
| Meta | `ask` (clarifying question), `undo`, `apply_preset` (brand kit, caption template), `save_preference` ("always put my logo top right") |

### 4.5 Assets (`assets.py`)

```python
class AssetKind(StrEnum): IMAGE, AUDIO, VIDEO, LUT, FONT

class Asset(Frozen):
    id: str                   # "a3f9c2" – random, never derived from the file name
    kind: AssetKind
    name: str                 # sanitised display name ("summer.mp3")
    path: Path                # data/chats/<id>/assets/<id>.<ext>
    duration: float | None
    size: tuple[int, int] | None

class AssetService:
    """Validates and normalises uploads; nothing reaches the renderer that ffmpeg has not probed."""
    def add(self, chat_id: int, name: str, data: BinaryIO) -> Asset: ...
```

Rules: upload by `PUT /api/chats/{id}/assets` with the raw body (no multipart parser), a size limit per kind,
type from the file's magic bytes (never the extension), a successful `ffprobe` for audio / video, images decoded and
re-encoded to PNG (strips payloads, caps dimensions to stop decompression bombs), `.cube` LUTs parsed and size-checked,
fonts loaded with a bounded parser. Assets live inside the chat folder, so deleting the chat deletes them. A
**brand kit** is a chat-independent set (`data/brand/`) with logo, fonts, colours, intro, outro and caption template.

### 4.6 Planner (`planner.py`)

```python
class EditPlanner:
    def __init__(self, llm: LLM, catalogue: OperationCatalogue, formatter: PromptFormatter): ...

    def plan(self, request: EditRequest) -> PlanResult:
        prompt = self.formatter.build(request)              # state: clips, layers with ids, assets, transcript excerpt
        reply = self.llm.chat_json(SYSTEM, prompt, schema=self.catalogue.schema())
        ops, errors = self.catalogue.parse(reply)
        if errors:                                          # one self-correction round with precise messages
            reply = self.llm.chat_json(SYSTEM, prompt + self.formatter.errors(errors), schema=...)
            ops, errors = self.catalogue.parse(reply)
        return PlanResult(ops=ops, reply=reply.get("reply"), errors=errors)
```

The prompt lists the clip's **layers with ids and names** ("txt-1 hook 'Elvesztettem magam – miért?' top,
captions-1 template 'bold-yellow'"), the attachments of this message and the chat ("a1 summer.mp3 audio 2:41"),
the brand kit, the user's saved preferences and a short transcript excerpt with timings. Few-shot examples cover both
languages and the tricky mappings (black title = hook, "song" = music, "logo" = brand kit image).

`IntentRouter` decides between today's job actions (generate, more clips, delete) and edits; it is the existing agent
extended with one action type, `edit`, that carries the operations.

### 4.7 Validation and resolution (`validation.py`, `timeref.py`)

Pydantic checks types and ranges; `OperationValidator` checks meaning against the current timeline and assets:
the layer id exists, the asset kind fits ("add_music" needs audio), the TimeRef resolves inside the clip, the result is
at least 3 s long, a transition fits between its neighbours, the text fits on screen (measured with the font). Each
error carries a fix hint used in the self-correction round, and an `ask` operation becomes a question in the chat.

### 4.8 Rendering

**Two stages with caching** – the key to fast iteration:

| Stage | Input | Output | Cached by | Typical time (20 s clip) |
|---|---|---|---|---|
| Base | source, cut, camera | `base.mkv`: 1080×1920 video (NVENC CQ 12, near-lossless) + edited speech (FLAC) | `Timeline.base_key()` | 50 s (4K source) |
| Finishing | base + layers + sequence + audio master | `preview.mp4` (540p) or `final.mp4` (1080p) | `Timeline.finish_key()` | 3 s preview, 8–10 s final |

`RenderPlanner` compares keys with the previous version and schedules only what changed. Captions, hook and CTA move
to finishing, so a caption restyle is a finishing-only render.

**FilterGraph builder** (`render/graph.py`): a small fluent builder that owns input numbering, pad labels and
escaping, so no compiler concatenates raw strings:

```python
g = FilterGraph()
video = g.input(base_mkv).video
logo = g.input(asset.path, loop=True).video
video = g.chain(video, Filter("eq", contrast=1.15, saturation=1.1))
video = g.overlay(video, g.chain(logo, Filter("scale", w=320, h=-1), Filter("format", "rgba")),
                  x=Expr.right(margin=48), y=48, enable=Between(2.0, 18.0))
g.output(video, label="v")
args = g.build()          # ["-i", ..., "-filter_complex", "...", "-map", "[v]"]
```

**Layer compilers** (Strategy): one class per layer kind, registered like operations.

```python
class LayerCompiler(Protocol):
    kind: str
    def compile(self, layer: Layer, g: FilterGraph, video: Pad, audio: Pad, ctx: CompileContext) -> tuple[Pad, Pad]: ...
```

| Layer | FFmpeg building blocks (all present in the bundled build) |
|---|---|
| Captions, text, hook, CTA | ASS file (existing `captions.py`, now one event list for all text layers) → `subtitles` / `ass` |
| Image, logo, PiP, B-roll | `overlay` with `enable='between(t,a,b)'`, `scale`, `format=rgba`, fade in / out via `fade=alpha=1` |
| Colour | `eq`, `colortemperature`, `vibrance`, `curves`, `lut3d`, `vignette`, `unsharp`, `hqdn3d`, `deshake` |
| Censor / burned text | `boxblur` / `gblur` in a cropped region, `delogo` for static boxes |
| Music, SFX | `amix` with `weights`, `afade`, `volume`, ducking with `sidechaincompress` keyed by the speech track |
| Clean audio | `arnndn` (RNNoise model shipped as an asset) or the Demucs voice stem that already exists |
| Speed | `setpts` + `atempo` (or `rubberband` for pitch-true speech) |
| Intro / outro, transitions | `xfade` (fade, wipe, slide, circle, zoom…) + `acrossfade`, `tpad` for holds |
| Loudness | existing two-pass `loudnorm` on the final mix |

**Profiles** (`render/profiles.py`): `PREVIEW` (540×960, NVENC p1, CQ 28, no second loudness pass) and `FINAL`
(1080×1920, NVENC p7 CQ 17, two-pass loudness). Aspect variants (1:1, 4:5, 16:9) are a base re-render with a different
camera canvas.

### 4.9 Speech input (`speech.py`)

`POST /api/speech` with the recorded Opus/WebM body → `SpeechService.transcribe(audio, hint=ui_language)` → text.
Uses faster-whisper **small** (or large-v3-turbo) **on the CPU in int8**, so it never fights Qwen for VRAM;
a 10-second command transcribes in about a second. Language restricted to hu / en like the main transcription. The
browser side is a `Recorder` module (MediaRecorder + an AnalyserNode level meter); the Permissions-Policy header
opens the microphone to the app's own origin only.

### 4.10 Transcript editing

The clip card gets a "Transcript" view: words are spans with their output times. Selecting words and pressing
Delete issues `cut_words`; the server maps them to source ranges and re-renders the base. Clicking a word seeks the
player. This is the Descript workflow on top of the word timings Clippy already has.

### 4.11 Preferences and presets

`PreferenceStore` keeps short natural-language rules the user asked to remember ("my logo always top right, 70 %",
"captions in the yellow template") and adds them to every planner prompt; `apply_preset` applies a brand kit or a
caption template. Caption templates are data (`presets/captions/*.json`: font, sizes, colours, outline, animation),
not code.

### 4.12 Export

- MP4 as today (library copy).
- **OTIO** (`export/otio.py`, optional `opentimelineio`): the cut, layers and markers, so the clip can be finished in
  Premiere, Resolve or Final Cut. Assets are referenced by path.
- Later: an **MCP server** (`clippy mcp`) exposing generate / edit / render as tools, so Claude Code or other agents
  can drive Clippy.

## 5. Agent loop in detail

1. **Collect**: message text (or dictated text), attachment ids, selected clip(s) – the card the user has open or "all".
2. **Route**: `IntentRouter` – job (generate / more / delete) or edit.
3. **Plan**: `EditPlanner` with the operation schema → operations + a short reply in the user's language.
4. **Validate and resolve**: `OperationValidator` + `AnchorResolver`; errors → one self-correction round; still
   failing → a clarifying question (`ask`) instead of a guess.
5. **Apply**: `Timeline.apply` → new version + `ChangeSet`; stored before rendering, so the change card appears at once.
6. **Render**: `RenderPlanner` queues base and / or finishing jobs on the existing runner with the live progress card;
   preview first, final next.
7. **Learn**: `save_preference` operations update the `PreferenceStore`.

Safety rules carried over: the LLM output is data until validated; operations can only reference this chat's assets
and clips; nothing outward-facing happens; destructive-looking requests are versions and therefore undoable.

## 6. API additions

| Method and path | Purpose |
|---|---|
| `PUT /api/chats/{id}/assets?name=…` | upload one attachment (raw body, size-limited) → `Asset` |
| `GET /api/chats/{id}/assets`, `DELETE …/assets/{asset}` | list, remove |
| `GET /media/{chat}/assets/{asset}` | thumbnails / previews through `MediaStore` rules |
| `POST /api/speech` | dictation → text |
| `GET /api/clips/{id}/timeline` | the current timeline (for the transcript view and the layer strip) |
| `POST /api/clips/{id}/undo` | previous version becomes current |
| `POST /api/clips/{id}/render` | final render of a previewed version |
| `GET /api/clips/{id}/export.otio` | OTIO export |

All state-changing calls keep the CSRF header and same-origin checks; uploads are written to a temporary file and moved
into place only after validation.

## 7. Frontend additions

ES modules in the existing style: `attachments.js` (chips, drag-and-drop, paste, upload progress), `recorder.js`
(hold-to-talk, level meter, cancel by sliding away on touch), `timeline.js` (layer strip under the player, colour per
layer kind), `transcript.js` (word spans, selection, delete), `changes.js` (change card with undo). All texts through
`i18n.js`; all interactions keyboard-accessible; layouts tested at 320 px.

## 8. Code quality rules for this work

- **Layers:** domain (`timeline`, `operations`) has no I/O; services (`AssetService`, `SpeechService`, planners) do
  I/O through injected collaborators; web is thin. Render compilers are pure functions of (timeline, assets) → args.
- **SOLID in practice:** one class per operation and per layer compiler (single responsibility); registries for both
  (open / closed); `Operation` and `LayerCompiler` are the only abstractions callers see (dependency inversion); LLM,
  repositories and ffmpeg runner are injected (testable without a GPU).
- **Immutability:** timelines and layers are frozen models; `apply` returns new objects; versions are snapshots.
- **No stringly-typed ffmpeg:** filters only through `FilterGraph`; text only through the ASS writer with escaping.
- **Types:** full type hints, pydantic for every boundary (HTTP, LLM, storage), `StrEnum` for kinds.
- **Small units:** functions under ~30 lines, modules under ~300 lines; no comments in code, one-line docstrings
  where the intent is not obvious (project convention).
- **i18n:** every user-facing string is a catalogue key in both languages; `ChangeFormatter` renders changes.
- **Tests:** written with each feature (see below); `pyflakes`, `vulture`, `pytest` and `pip-audit` stay green.

## 9. Testing strategy

| Level | What | How |
|---|---|---|
| Domain | each operation's `apply`, `ChangeSet`, keys | pure unit tests, no files |
| Validation | every error path with its hint; anchors in both languages | table-driven tests |
| Compiler | filter graphs for each layer kind and combinations | golden tests on `FilterGraph.build()` output |
| Planner | Hungarian and English requests → operations | `FakeLLM` replies + schema round-trip; a small eval set of real prompts run against Qwen (like trirouter's routing eval) |
| Render | finishing pass on a 3-second synthetic clip (`testsrc2`, `sine`) | ffprobe asserts: duration, streams, loudness, overlay pixel colour at a known time |
| Assets | malicious and malformed uploads (wrong magic, huge image, broken audio, path tricks) | unit tests with crafted bytes |
| Web | upload, speech, undo, render endpoints incl. CSRF | `TestClient` like `test_web.py` |
| E2E | one scripted chat session per phase | manual checklist in the browser, recorded in the PR |

## 10. Phases

| Phase | Scope | Done when |
|---|---|---|
| **1 – Timeline core** | `timeline`, `operations` (text, captions, hook / CTA, colour, LUT, music, volume, ducking, fades, cut range, intro / outro with transitions, image / logo overlay), assets + upload UI, base / finishing split with caching, change card, undo | the example in section 3 works end to end; a caption or music change re-renders in < 15 s |
| **2 – Talk and anchor** | microphone + `SpeechService`, word / phrase anchors, B-roll at words, SFX library, zoom on request, speed, censor / blur, aspect variants, 540p previews, transcript editing | "a 'pénz' szónál mutasd ezt a képet és legyen pénzes hangeffekt" works by voice |
| **3 – Polish and brand** | caption template library with emoji and keyword colours, brand kit, saved preferences, clean audio (RNNoise), beat-synced cuts, progress bar, burned-in text removal (OCR + delogo / blur), OTIO export | brand kit applied by one sentence; a Premiere-ready OTIO export opens in Resolve |
| **4 – Reach** | MCP server, image-understanding model for visual anchors ("when he lifts the glass"), batch edits across chats | Claude Code can generate and edit clips through `clippy mcp` |

## 11. Risks

| Risk | Mitigation |
|---|---|
| The 14B model mis-maps complex requests | schema-constrained output, validator with fix hints, one self-correction round, `ask` instead of guessing, change card + undo, an eval set run before each release |
| VRAM: Qwen (13 GB) + Whisper + rendering | dictation on CPU; GPU stages unload the LLM as today; previews use NVENC p1 |
| Base cache disk use | near-lossless base per version only when the base changes; purge with the chat; cap per chat |
| Licensing of music and LUTs | only user-provided or bundled CC0 assets; no download by song name |
| FFmpeg filter edge cases | golden tests per compiler; the builder escapes all values; render failures surface in the chat with the ffmpeg tail |

## Sources

- [OpusClip](https://www.opus.pro/homepage), [OpusClip review (The CMO)](https://thecmo.com/tools/opusclip-review/)
- [Descript video editing](https://www.descript.com/video-editing), [Underlord custom instructions](https://www.createwith.com/tool/descript/updates/descripts-underlord-ai-now-learns-your-editing-style-with-custom-instructions)
- [Submagic AI video editor](https://www.submagic.co/ai-video-editor), [Submagic auto video editor](https://www.submagic.co/features/auto-video-editor)
- [CapCut Auto Cut help](https://www.capcut.com/help/auto-cut-in-capcut)
- [Captions: edit with AI](https://captions.ai/features/edit-with-ai)
- [VEED vs Kapwing](https://www.veed.io/learn/veed-vs-kapwing), [Kapwing AI](https://www.kapwing.com/ai)
- [Vizard: best clipping tools 2026](https://vizard.ai/blog/best-ai-video-clipping-tools-2026), [Klap: AI video clipper](https://klap.app/blog/ai-video-clipper)
- Open-source agent editors: [SynthCut](https://github.com/relo-video/synthcut), [MakeMyClip editor](https://github.com/MakeMyClip/editor), [Cutler](https://github.com/rahijamil/cutler), [Orkas VideoStudio](https://github.com/BlueSkyID666/Orkas-VideoStudio), [VibeFrame](https://hn.jakechampion.name/reader/https://github.com/vericontext/vibeframe)
