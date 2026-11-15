# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/); one line per
change, what changed rather than how; dates are ISO 8601.

## [Unreleased]

### Added
- Four ready-made prompts on the start screen (podcast, gameplay & stream, video & promo, quick shorts) in Hungarian
  and English: a button fills the message box, the link goes after "Videó:", and the same preset always gives the
  same settings and picking rules.
- The stream layout falls back to the blurred full frame when the video has no facecam, instead of stopping.
- Full editing from the chat: speed (slow motion or faster, whole clip or a part, pitch kept), aspect ratios (9:16,
  1:1, 4:5, and 16:9 built from the whole source frame), effects at a word or a second (punch-in zoom, shake, flash,
  dip to black, black and white, blur, vignette), colour emoji stickers with motion, built-in sound effects or your
  own file at a moment, B-roll from an attached video, joining other clips of the chat with a transition, animated
  texts (fade, pop, slide, typewriter) and a transition to hide a cut.
- Chat-driven editor: every clip version has a timeline (cut, camera, captions, hook and CTA texts, overlays, colour,
  music, intro / outro), and the chat edits it with validated operations in Hungarian or English, asking back when
  a request is unclear.
- Attachments: images, music, video and `.cube` LUTs through the paperclip button, drag and drop or paste, with
  upload progress; files are checked by content, images are re-encoded and sizes are capped.
- A change card for every edit with the list of changes and one-click undo; undo is a new version, so nothing is lost.
- API: chat assets (upload, list, delete, media), a clip's timeline and undo.
- Short-form playbook ([docs/playbook.md](docs/playbook.md)) built into the app: the clip picker scores relevance
  and risks (misquote, defamation, spoiler, needs context, background music) besides the hook, and labels each hook
  with its type; every clip gets a B hook for A/B tests and a comment question; the chat editor follows the craft
  rules and the assistant answers strategy questions from the playbook.
- Post text ready to paste, in the clip's language: *Copy text* gives title, comment question and hashtags; the
  `.txt` adds the pinned comment with the source link, both hooks, risks and a credit reminder.

- Any public video link, not just YouTube: TikTok, Instagram Reels and posts, Facebook Reels, Twitch clips and VODs,
  Kick and the other sites yt-dlp supports. Browser impersonation (`curl_cffi`) gets past TikTok's bot check;
  `CLIPPY_COOKIES_FROM_BROWSER` (Firefox) or `CLIPPY_COOKIES_FILE` (a `cookies.txt` from any browser) lets the
  web app open videos that need a sign-in; the file is only read, each download works on a private copy.
- Phone and tablet access on the same Wi-Fi: `clippy restart --lan` (or `CLIPPY_LAN=1`) and `clippy pair`, a
  one-time link with a QR code; only paired devices on private addresses get in.
- Caption fixes in the chat editor: a misheard word or phrase is corrected with `fix_caption` ("not X but Y"), kept
  in the timeline, so later edits and undo keep it too.
- Texts quoted in an edit request go on the video letter for letter, even when the model copies them with a typo.
- Short videos (a TikTok, a Reel) become one clip of nearly all of it; a short one without speech becomes one
  clip of all of it.
- Clear download errors in both languages: sign-in needed, deleted or region-locked, rate-limited, live stream,
  profile / channel / playlist link, no video in the post, no sound – each with what helps.

### Changed
- A new clip is only the moment, framed vertically, with captions: the opening text and punch-in zooms are off by
  default (`--hook`, `--punch` or a chat request turn them on); the suggested opening texts stay on the card as ideas.
- The hook is the clip's own first seconds (a funny, bold or surprising moment the clip starts on); the text
  over them is now called the opening text. Short videos kept whole (a TikTok, a Reel) get no opening text.
- Edits render in two passes: the camera-framed base is cached per cut, so caption, colour, overlay and music edits
  take about 7 s instead of a full re-render.
- The separate restyle action is gone; style changes are ordinary edits.
- Captions keep 120 px from the sides, clear of the platform buttons on the right.

### Performance
- Jobs of different chats run in parallel (three workers); one chat's jobs keep their order, so versions never
  clash. A quick undo no longer waits behind another chat's long generation.
- Up to three clips render side by side and are stored in order. Edits of several clips run side by side too.
- Face detection runs on eight threads with one detector each (same results, a 20 s 4K clip's analysis 11 s → 4 s),
  and when the camera stays in one area only that strip of the 4K frame is sent from the decoder (the pipe, not
  the decoder, was the limit).
- A new clip's render also writes the editor's base in the same pass (pixel-identical to a separate base render),
  so the first edit of every clip skips a ~20–40 s base render.
- Together: 3 clips of a 4K video in 46.9 s instead of 73.3 s, with the edit bases included; final clips are
  bit-identical to before.
- A new video's sound is downloaded first: voice isolation, the transcript and clip picking run while the video
  itself keeps downloading in the background; rendering waits for it only when it starts.
- Downloads resolve the link again in the same session (TikTok ties its media URLs to the session's cookies), and
  the sound is fetched first only when it is the video's own separate stream (TikTok's "audio" is the song used).
- Consistency: every cache file, download and clip version is written under a temporary name and renamed when
  complete (a crash can no longer leave a half-written file that looks finished); a version number can be stored
  only once; shared resources (one video's cache, render slots, the GPU) are locked in one fixed order, checked
  at runtime, so parallel jobs cannot deadlock; the GPU runs Demucs, Whisper and the LLM one at a time.

## [1.0.0] – 2026-11-13

First public release.

### Added
- Pipeline: yt-dlp download (≤4K SDR, Opus), Demucs voice isolation, faster-whisper large-v3 with hotwords,
  MMS_FA word alignment, LLM hook scoring with a global re-rank, tight edit, virtual camera, animated captions,
  NVENC encoding and two-pass −14 LUFS mastering.
- Web app: a local, ChatGPT-style chat that generates, edits, restyles and deletes clips, with versions and a
  video library copy of every finished clip.
- Live progress card: queue position, step bar, percentage, current detail and a self-calibrating time-left
  estimate; the tab title and the sidebar show the percentage too.
- Hungarian and English everywhere: spoken-language detection between the two, a chat that answers in the user's
  language, a `HU` / `EN` switch in the web app and `clippy lang hu|en` for the command line.
- Captions in the other language: an English video can get Hungarian captions and hook (and the other way round),
  translated line by line by the LLM and kept in sync with the speech (`captions` option, `--captions`).
- `clippy` command: start, down, restart, status, open, logs, clip, install, lang, with tab completion in
  PowerShell and cmd (Clink).
- Responsive layout down to 320 px, swipe drawer, keyboard shortcuts, reduced-motion support and a new logo.
- Security hardening: host allow-list, CSRF checks, strict Content-Security-Policy, safe media lookup, SSRF
  filtering, pinned model hash and prompt-injection guards.

### Performance
- Rendering a 4K clip takes about half the time: frames are read from ffmpeg straight into reused buffers (the
  pipe copy was the bottleneck), and the next clip's face analysis runs while the current one encodes. The output
  is bit-identical.
- The local LLM is loaded when the server starts, so the first chat answer takes seconds instead of a minute.
- Static files are served from content-hashed URLs with a one-year immutable cache, the page preloads its modules,
  and text responses are gzip-compressed.

### Web presence
- A full favicon set (ICO, PNG, Apple touch icon) and a web app manifest.
- Project page with search and social metadata (Open Graph, JSON-LD software + FAQ), robots.txt, sitemap and
  `llms.txt` for AI search engines.
