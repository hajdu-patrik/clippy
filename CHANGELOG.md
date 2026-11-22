# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/); one line per
change, what changed rather than how; dates are ISO 8601.

## [Unreleased]

### Added
- Campaigns (sidebar): clipping campaigns for a streamer, a podcast or a brand, with a brief, a rate per 1,000 views,
  a cap per post and the moments to clip; Clippy clips every source with the brief, collects the clippers' post links,
  reads their public views and works out the payout.
- Stream clips cut to the streamer's camera full screen for a moment at the biggest reactions.
- An optional title bar: the clip's title in a black box at the top for the whole clip (only when asked).
- ✂️ Edit my video, a fifth ready-made prompt: attach your own video (or give a link) with pictures, a logo, an intro,
  an outro or music, write how to edit it, and Clippy cuts the clip and then puts your edits on it as a new version.
- A video attached to a message is clipped when the message has no link; the other attachments and an edit prompt's
  wishes go on the new clips once they are made.
- Every ready-made prompt ends with what you get.
- Each finished job keeps how long each of its steps took (on the progress card's data and as a `[timing]` line in the
  server log).
- A chat's editor base, its largest file, is removed after a week without use; its next edit makes it again.
- The Scheduler (sidebar): every planned post by day with its cover, chat, platforms and time, scheduling a clip from
  one place (chat, clip, platforms, date and time, your posting times and the next free slot), moving or removing the
  posts that wait in Clippy, and a link to YouTube Studio for videos YouTube holds, which Clippy's upload access cannot
  change; your posting times are set in Settings, General.
- The AI is chosen at the first start, in Settings, AI, or with `clippy llm <mode>`: Ollama on this computer
  (default), OpenRouter with your key, your Claude subscription through Claude Code, your ChatGPT subscription
  through Codex, or none; it applies from the next request.
- A cover YouTube turns down at posting (its thumbnail rate limit) is tried again in the background, less and less
  often, until it is set or eight tries run out.
- Accounts for seven platforms in Settings, as a grid of cards: YouTube, Instagram, TikTok and now Facebook (Page
  Reels) and X (chunked video upload) can be connected and posted to, planned posts included; Twitch and Kick can be
  connected, but neither has an upload API, so their cards say "Connect only" and Clippy never posts there. Each
  platform is one class behind a shared sign-in base (`clippy/publish/`).
- Daily picks: "minden nap 3 GTA VI klipet" (or Settings, Daily picks) clips the most watched streamer moments of a
  game's Twitch and Kick category every morning, one per streamer, in the audience's language, and gives each a time
  to post it for a US audience with its post text; Settings lists the day's picks with their times and texts.
- A one-minute product demo in 4K (`docs/assets/demo.mp4`): an animated preview at the top of the README links to it,
  and the project page plays it under the introduction.
- Every video is transcribed and captioned in its own language (English stays English, Hungarian
  Hungarian, other languages their own); captions change language only when the user asks for it, never because
  of the language they write to the chat in.
- Deleting a chat stops its running job at once (download, transcript or render), so nothing keeps working
  for a chat that is gone.
- Streamers by name: "klippeld N3on legújabb streamjét" or "clip the latest stream of Kai Cenat" finds the newest
  finished stream of one of fifty well-known streamers (or of a `twitch.tv/name`, `kick.com/name` or `@name`) across
  YouTube, Twitch and Kick, skips one that is live now, and clips it with the gameplay and stream kind.
- Comfort: voice input in the chat (local Whisper on the CPU), browser notifications when a job is done, one-step
  installers (`install.ps1`, `install.sh`) and macOS / Linux support (bin/clippy, browser and Ollama start, system
  fonts, bash completion, CI on Windows, Ubuntu and macOS).
- Quality: a native proofreading pass and Hungarian quotation marks for Hungarian texts; speakers told apart by
  voice for picking exchanges; covers with the title written on them plus a B cover and a B title for A/B tests
  (choose A or B when posting); a saved style kit (caption font, colour, capitals, logo, outro) for every new clip;
  streams use the facecam-on-top layout by default.
- Learning from results: views, likes, comments, shares and the share watched are read back from YouTube (with
  YouTube Analytics) and Instagram onto each post; *What works* in Settings compares clips with the creator's usual
  by opening, length, kind of moment and posting time, and the clear patterns steer the picker and the chat.
- Watched channels: a YouTube channel (uploads and past streams), a Twitch channel or a playlist is checked for new
  videos on a schedule, from Settings or the chat ("figyeld ezt a csatornát"); every new video becomes its own chat
  with clips of the chosen kind.
- Planned posts: a clip can be posted at a set time (and cancelled from its card), and a watched channel can post its
  new clips by itself into daily time slots, once the user sets the platforms and the times.
- Gameplay and stream peaks without words: loud moments, laughter, sudden action on screen and the chat of YouTube
  stream replays mark the transcript for the picker and add moments of their own, with titles written from the
  reactions and what the chat wrote. A long video without speech is now clipped by its reactions instead of
  stopping (docs/quality.md).
- Posting to YouTube, Instagram and TikTok from the app: a Post button on every clip (platforms, YouTube visibility,
  TikTok drafts or private) and from the chat in your own words. Accounts are connected once in Settings with your
  own developer apps (docs/publishing.md); the clip card links each post or says what went wrong, one platform
  failing never stops the others, and a retry or a restart never posts twice.
- Post text for every clip: a curiosity title (honest clickbait), a 1-2 sentence teaser description, the comment
  question, the full video's link, your own profile links and the hashtags, in the clip's language. Profile
  links (TikTok, Instagram…) are set once under Settings and stay on this computer (data/web/settings.json).
- Clips made before get the new post texts on request ("írj címet és leírást a klipeknek"): new titles and
  descriptions are written from what is said in each clip; the card, the .txt file and the library copy follow,
  the video stays as it is.
- Live in every window: chats, messages, progress, clips, renames and deletions show up at once in all open
  windows and on paired phones; a browser keeps one live connection that its windows share.
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
  profile / channel / playlist link, no video in the post, no sound, each with what helps.

### Changed
- A stream clip follows the stream's scene switches: when the camera is not where it was found, the shot follows the
  face or shows the whole frame; without any facecam the whole frame sits on a blurred copy of itself.
- Captions already burned into a video are found, and Clippy leaves its own out instead of doubling them.
- 🎮 Facecam stream (was Gameplay & stream): 15-60 s clips with the reaction on screen in the first second, the
  camera on the top two fifths and English captions on the line above the game.
- 🎙️ Podcast clips follow the short-form podcast style: 4 clips of 20-45 s, the first sentence as the hook, the
  opening text in a black box, zooms on cuts, captions with the key word in yellow, no watermark.
- The post text of a clip made from your own file names the file instead of linking a local path.
- Stream layout: the streamer's camera takes the top two fifths and the gameplay the three fifths below, with a thin
  black line between them, and the captions sit on that line.
- Final clips are capped at 18 Mbit/s: about half the size and upload time, with no visible loss after the platforms'
  own re-encode.
- The daily picks ask Twitch and Kick again after a timeout before skipping them for the day, and downloads wait longer
  and retry more on slow connections.
- The server log no longer fills with tracebacks for browser tabs that close their connection, nor with yt-dlp's
  warning about a missing ffprobe.
- Twitch clips are downloaded in their landscape version, not the portrait one Twitch also offers, so the streamer's
  camera is found and the stream layout works.
- Open pages pick up a new version of the app by themselves after a restart (once no dialog is open and nothing
  is typed), and the Scheduler follows changes made in any other tab or by a job as they happen.
- The web app follows the project page's look (shared type scale, radii, pill buttons with one filled button per view)
  and works better on a phone: Settings, Scheduler and Post fill the screen with their buttons pinned, every control is
  at least 44 px, and the sidebar lists the Scheduler and Settings by name.
- A clip posted at a set time to YouTube no longer offers a visibility choice: YouTube makes it public at that time.
- A clip's planned YouTube time is remembered (`posts.publish_at`), and free posting times skip it.
- A streamer's camera is found even when the game shows faces of its own: the face that stays put the longest is the
  camera, so GTA clips keep the stream layout.
- Stream clips give the game more room: the streamer's camera takes the top third (a 16:9 box), the gameplay the rest.
- Gameplay and stream clips (the preset, watched channels and daily picks) get English captions of what the streamer
  says; other languages are translated.
- A stream clip's cover shows the game across the whole picture with the streamer as a framed card on it, under a
  title whose first line is white and the rest yellow.
- Daily picks only make clips; nothing is posted until the owner approves it.
- The web job module is split by responsibility (`texts`, `progress`, `handlers`, `jobs`), routes share two error
  handlers and one upload reader, the platform adapters share one wait loop, and the command line's errors (LLM,
  ffmpeg, files) come from the text catalogue.
- The project page is served with a strict Content-Security-Policy and the usual security headers.
- Clip media is revalidated on every load, so a cover rewritten after a title change shows at once.
- Dependency floors moved past known-vulnerable releases (requests, pillow, starlette); CI runs on the current
  `actions/checkout` and `actions/setup-python`.
- The topic sets a clip's length (default limit 20-90 s): a start check moves a clip that drops into a story or a
  rant back to where it starts, the ending check follows the topic by up to four sentences, and a recording cut off
  mid-sentence loses its unfinished words.
- A planned YouTube post is uploaded at once with its publish time, so YouTube makes it public on time even while
  the computer is off; daily picks can post each clip at its time by themselves (Settings, Automation).
- Brand logos for the seven platforms on the account cards, the connect buttons, the post dialog and the watch form.
- Settings is split into pages (General, Accounts, Style, Automation, Results) with a tab bar, and the style kit
  takes an optional intro (an image stays 1.5 s) next to the logo and the outro.
- The project page plays a 1080p demo on demand (4K behind a link), shares one stylesheet and type scale with the
  legal pages, serves half-size images to phones and has cache rules for its assets.
- English only: the web app, the chat replies and the command line are in English, and the language switch and
  `clippy lang` are gone. Videos still keep their own language for the transcript, the captions and the post text
  (a Hungarian video gets a Hungarian post), and captions can still be translated between English and Hungarian.
- The demo video has a new, original soundtrack with the clips' own voices kept.
- Post text in the style creators post: a teaser ending with an emoji, the comment question with "drop it in the
  comments", the original video, a follow line per profile (YouTube, Instagram, TikTok) and hashtags; the title
  for YouTube Shorts is listed under it. Copy text uses the same text as the .txt file, and changing the profile
  links rewrites every clip's post text. Every clip of a video shares one follow topic ("GTA 6").
- Copy text works on phones too: where the browser has no clipboard API (a phone on the Wi-Fi is not a secure
  page), the text is copied by selection, and if even that is blocked a window shows it selected to copy by hand.
- No long dashes (em / en dash) in anything Clippy writes: the picker, the editor and the post texts are told
  not to use them and a filter replaces any that slip through; the interface and the docs use none either.
- Nicer layout without a facecam: the stream layout follows the action like the automatic one, and a wide shot
  with no face to follow shows its centre square (twice as large on a phone) on the blurred frame instead of a
  small strip.
- A facecam must stay put: faces that move around (game characters, cutscenes) no longer pass as one, so
  such videos get the whole frame instead of an empty crop on top.
- Clips end on a complete thought: they start at a sentence start and end where the sentence is over and the
  next one does not carry it on (or at a clear pause without punctuation), may run a little past the length
  setting to finish, and an LLM payoff check can move the end by whole sentences. Before, a pick shorter than the
  minimum was stretched word by word and could stop mid-sentence. Trim edits in the chat land on whole sentences, and a cut given in seconds never splits a
  sentence: an edge inside one moves to that sentence's nearer edge.
- Self-repair: a failed job is diagnosed and tried again on the same card, up to three times, with the setting
  that caused it changed (GPU out of memory: no voice isolation, then the CPU; hardware encoder: x264; no moment
  fitting the length: looser lengths; network or model hiccups: a pause). Hopeless cases (private video, no
  sound) stop at once with the reason. A clip that fails is tried once more and otherwise skipped, the others
  are delivered; an edit of several clips keeps the ones that worked.
- Jobs survive a restart: a job cut off by a restart or a crash starts again by itself, and `clippy restart`
  waits for running jobs (`--force` restarts at once).
- A media link the site refuses mid-download (HTTP 403, YouTube does it now and then) is resolved again and the
  download retried, up to three times; downloads go in 10 MB chunks.
- Deleting a chat also deletes its clips from the video library (an emptied folder goes too); clips made
  with the command line or by other chats stay.
- A new clip is only the moment, framed vertically, with captions: the opening text and punch-in zooms are off by
  default (`--hook`, `--punch` or a chat request turn them on); the suggested opening texts stay on the card as ideas.
- The hook is the clip's own first seconds (a funny, bold or surprising moment the clip starts on); the text
  over them is now called the opening text. Short videos kept whole (a TikTok, a Reel) get no opening text.
- Edits render in two passes: the camera-framed base is cached per cut, so caption, colour, overlay and music edits
  take about 7 s instead of a full re-render.
- The separate restyle action is gone; style changes are ordinary edits.
- Captions keep 120 px from the sides, clear of the platform buttons on the right.
- Clips rendered side by side no longer log "font skipped" when another render copied the caption font first.
- Undo twice steps back two versions instead of bringing back the undone edit.
- The opening text and the call to action can be moved by asking ("move the opening text to the center").
- Moving a clip's start or end in fast speech without punctuation lands within 6 s of where it was asked, not tens
  of seconds away.

### Performance
- Rendering a clip from a 4K source is about 2.7x faster (45 s -> 16 s for a 28 s clip): the stream layout is
  built by the decoder, so the full 4K frames never pass through Python, and other layouts decode only at the
  resolution the output uses.
- "More clips" reuses the clip picker's earlier answers for the same video, prompt and model (kept with the
  video's cache and removed with it); only when those are used up are the windows asked again, told which
  moments are taken. On a 26-minute video the picking for "3 more clips" went from 231 s to 51 s.
- The sentence boundary model uses binary search: 36,000 words in 24 ms.
- Jobs of different chats run in parallel (three workers); one chat's jobs keep their order, so versions never
  clash. A quick undo no longer waits behind another chat's long generation.
- Up to three clips render side by side and are stored in order. Edits of several clips run side by side too.
- Face detection runs on eight threads with one detector each (same results, a 20 s 4K clip's analysis 11 s → 4 s),
  and when the camera stays in one area only that strip of the 4K frame is sent from the decoder (the pipe, not
  the decoder, was the limit).
- A new clip's render also writes the editor's base in the same pass (pixel-identical to a separate base render),
  so the first edit of every clip skips a ~20-40 s base render.
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

## [1.0.0] - 2026-11-13

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
