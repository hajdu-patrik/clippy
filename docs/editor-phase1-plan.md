# Editor phase 1 – implementation plan

Companion to [editor-design.md](editor-design.md). Phase 1 = the timeline core: every clip version gets a timeline,
the chat edits it with validated operations, renders split into a cached base and a fast finishing pass, and
attachments (image, audio, video, LUT) can be used. Work top to bottom; every step ends green
(`pyflakes`, `pytest`) and is its own commit.

## Definition of done

The example of design section 3 works in the browser: attach a song and an outro, ask in Hungarian for music under
clip 2, the outro with a fade and the hook removed → a change card, a preview within ~5 s, a final within ~15 s,
one-click undo. Caption / colour / music edits never redo the camera. 58 + new tests pass.

## Steps

| # | Step | Files | Tests |
|---|---|---|---|
| 1 | **Domain model**: `Frozen` base (pydantic, frozen, extra=forbid), `TimeRange`, `Transform`, `TextStyle`, `ColorAdjust`, `Fades`, `Ducking`, layer classes (`TextLayer`, `CaptionLayer`, `ImageLayer`, `VideoLayer`, `ColorLayer`, `AudioLayer`) as a discriminated union, `Cut`, `CameraSettings`, `Bumper`, `Sequence`, `AudioMaster`, `Timeline` with `with_layer`, `without_layer`, `replace_layer`, `base_key()`, `finish_key()` | `clippy/editing/timeline.py` | `tests/editing/test_timeline.py`: immutability, keys change only with their inputs, JSON round-trip |
| 2 | **Timeline from today's clips**: `Timeline.from_clip(clip, options)` builds cut + camera + caption / hook / CTA layers from the existing `Clip` + `Options`, so old versions get a timeline lazily | `timeline.py` | old clip dict → timeline → same hook, CTA, caption style |
| 3 | **Storage**: `clips.timeline` JSON column (ALTER migration like `video_id`), `TimelineRepository` (get current, save version, previous for undo) | `clippy/web/db.py`, `clippy/editing/repository.py` | migration on an old DB file; save / load / undo order |
| 4 | **Operations core**: `Operation` ABC, `@operation` registry, `EditContext` (ids, assets, resolver), `Change` / `ChangeSet`, `OperationCatalogue.schema()` and `.parse()` with error messages | `clippy/editing/operations/__init__.py`, `clippy/editing/changes.py` | schema contains every registered kind; parse errors are readable |
| 5 | **Phase-1 operations**: text (`add_text`, `edit_text`, `remove_text`, `set_hook`, `set_cta`, `caption_style`, `hide_captions`), visual (`add_overlay`, `color`, `apply_lut`), audio (`add_music`, `set_volume`, `duck`, `fade_audio`, `remove_audio_layer`), structure (`cut_range`, `trim`, `set_intro`, `set_outro`, `transition`), meta (`ask`, `undo`) | `clippy/editing/operations/*.py` | one test per operation: apply → expected layer and change; `touches_base()` correct |
| 6 | **Time references**: `TimeRef` (at, edge + offset; word / phrase in phase 2 but the type exists now), `AnchorResolver` over output-time words | `clippy/editing/timeref.py` | edge offsets, out-of-range errors |
| 7 | **Editor (validation as a dry run)**: operations applied in order, failures collected as issues with readable messages (shown to the user, fed back to the LLM); one version per successful request | `clippy/editing/editor.py` | failing op keeps the old timeline; issues in both languages |
| 8 | **Assets**: `AssetKind`, `Asset`, `AssetService` (magic-byte sniffing, size limits, ffprobe, image re-encode to PNG via OpenCV with a pixel cap, `.cube` parse), `AssetRepository` under `data/chats/<id>/assets/`, purge with the chat | `clippy/editing/assets.py`, `clippy/storage.py` | crafted bad files rejected; good files probed; purge removes assets |
| 9 | **FilterGraph builder**: inputs, pads, `Filter`, `Expr`, `Between`, escaping, `build()` → argv | `clippy/editing/render/graph.py` | golden strings; escaping of quotes, colons, commas |
| 10 | **Layer compilers**: text / captions / hook / CTA through one ASS writer (extend `captions.py` to take any text layer), overlay, colour + LUT, audio (amix, afade, sidechaincompress), sequence (xfade + acrossfade for intro / outro) | `clippy/editing/render/layers.py`, `clippy/captions.py` | golden graphs per layer and for a combined timeline |
| 11 | **Render split**: `BaseRenderer` = today's `ClipRenderer` without captions, writing `base.mkv` (NVENC CQ 12 + FLAC speech); `FinishingRenderer` with `PREVIEW` / `FINAL` profiles; `RenderPlanner` decides base and / or finishing by keys; base files cached per chat | `clippy/editing/render/*.py`, `clippy/render.py`, `clippy/pipeline.py` | synthetic 3 s clip (`testsrc2` + `sine`): base reused when only a layer changes; ffprobe duration / streams / loudness; pixel check of a logo overlay |
| 12 | **Planner**: `PromptFormatter` (clip layers with ids, assets, brand kit placeholder, transcript excerpt), `EditPlanner.plan()` with one self-correction round, HU / EN few-shot examples | `clippy/editing/planner.py` | `FakeLLM` replies: valid, invalid → corrected, ambiguous → `ask` |
| 13 | **Agent integration**: new `edit` action in `agent.py` (routes to the planner), `EditJob` in `jobs.py` (apply → save version → preview → final), progress stages for base / finishing, change card message kind | `clippy/web/agent.py`, `clippy/web/jobs.py`, `clippy/web/chats.py` | web tests: edit message → new version + change card; undo endpoint |
| 14 | **API**: `PUT/GET/DELETE …/assets`, asset media, `GET /api/clips/{id}/timeline`, `POST …/undo`, `POST …/render` | `clippy/web/server.py`, `clippy/web/media.py` | CSRF, size limit, wrong type, foreign chat ids |
| 15 | **Frontend**: `attachments.js` (+ button, drag-and-drop, paste, chips, upload progress), `changes.js` (change card + undo), preview / final buttons on cards, i18n keys (hu + en) | `clippy/web/static/js/*`, `index.html`, `style.css`, `i18n.js` | node syntax check, i18n parity check, browser run at 320 px and desktop |
| 16 | **Docs and release notes**: README section "Editing in the chat", `docs/cli.md` if the CLI gets `--edit`, CHANGELOG `[Unreleased]` | docs | – |
| 17 | **Live check**: the definition-of-done scenario on a real chat, Hungarian and English | – | recorded in the commit message |

## Progress

- Done (2026-11-14): steps 1–11 – timeline, factory, storage column + repository, 18 operations with catalogue and
  schema, time references, editor, assets service, filter graph builder (two-level escaping verified against ffmpeg),
  layer compilers (colour + LUT, image overlays, ASS text / captions, music with ducking, intro / outro with xfade),
  base renderer (mezzanine mode of the existing renderer, `exclude` cuts), finishing renderer with preview / final
  profiles and two-pass loudness, base cache + timeline renderer. The old render path stays bit-identical (PSNR inf).
  Real 4K clip: base 45 s once, then preview 6.4 s, final 6.8 s, caption restyle 6.9 s; −14.1 LUFS. 93 tests green.
- Done (2026-11-14): steps 12–13 – planner (state with layer ids, attachments, transcript; operation catalogue
  generated from the registry; light envelope schema because a full union schema derails constrained decoding;
  dedupe + one correction round), new ops set_title / set_sound / caption language, one agent `edit` action that only
  picks clips (the request is the user's own words), EditService + EditJob with versions and change cards.
  Real Qwen check: 5/5 Hungarian / English requests correct in 1.4–5.5 s; live web edit rendered a v2. 100 tests.
- Done (2026-11-14): steps 14–16 – asset API (streamed upload with a size cap, list, delete, media through
  `MediaStore`), timeline and undo endpoints, attachments in the composer (button, drag and drop, paste, chips with
  upload progress), attachments on sent messages, change card with undo, hu / en UI texts, README and CHANGELOG.
  102 tests green.
- Done (2026-11-14): step 17 – live check in the browser: a logo PNG and a WAV attached with the file picker, a
  Hungarian request ("logo top right, this music quietly under it") → planner reply, edit job done in 19 s with the
  cached base, change card "#1 · v3" (+ image, + music); the frame shows the logo top right, the music is in the mix
  (−20 dB, ducked); undo → v4 = v2 in 2 s; no horizontal overflow at 320 px. Phase 1 is complete.
- Next (phase 2): preview / final buttons on cards, voice input, word / phrase time anchors in the UI.

## Order and checkpoints

- Steps 1–7 are pure Python without I/O – fast to build and test, no server restart needed.
- Steps 8–11 touch files and ffmpeg; the existing renderer must keep producing bit-identical output for clips without
  edits (compare with PSNR like the performance work did).
- Steps 12–15 change the running app: restart only when no job is running.

## Conventions to keep

English code and docs, no comments, one-line docstrings, every user-facing text in both languages, strict CSP (CSS
variables set from JS, no inline styles), uploads never trusted, the LLM output is data until validated, and every new
user-visible behaviour gets a test.
