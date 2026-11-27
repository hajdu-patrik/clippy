# Output quality

What each stage of the pipeline does for the picture, the sound and the captions.

## Stage by stage

| Area | What it does |
|---|---|
| **Source** | Downloads the best SDR quality up to 4K: a 9:16 crop of 4K is downscaled, so it stays sharp. Skips AV1 and HDR (slow decode, washed-out colours). Takes the best Opus audio. |
| **Voice isolation** | Demucs (htdemucs, GPU) splits the soundtrack into voice and music/noise. Transcription and alignment run on the clean voice, so a music bed does not confuse them. `--duck 6` lowers the music by 6 dB under speech in the final mix. |
| **Word alignment** | Forced alignment (torchaudio MMS_FA, as WhisperX does) puts every word on 20 ms acoustic frames. Highlights land on the syllable and cuts land on real word boundaries. |
| **Transcription** | `large-v3` on the GPU. The video title and the `--vocab` names are re-applied as hotwords to every 30 s window. A guard stops repetition loops and invented text in silence, and the VAD keeps 400 ms of padding so word onsets are not clipped. |
| **Clip selection** | The LLM reads the transcript sentence by sentence. On long videos it compares the candidates against each other and polishes the hooks. Cuts snap to word boundaries and never touch the neighbouring word or breath. |
| **Tight edit** | Removes silences longer than 0.45 s and filler words (um, öö). It cuts only where it is actually quiet, so laughter, applause and music after a punchline stay. Cut points are refined on the audio envelope and snapped to the frame grid, and every join has a click-free 6 ms fade. |
| **Camera** | The virtual camera cuts where the source cuts (shot detection). YuNet faces with eye and mouth landmarks give the active speaker from mouth motion (minus head motion), with hysteresis. Two faces share the frame when they fit. Inside a dead zone the camera holds still; beyond it a critically damped spring follows (no shake, no overshoot), with a hard cut on a speaker change. |
| **Layout per shot** | Speaking faces get a tracking crop; B-roll, screens, crowds and gameplay show the full frame on a blurred backdrop. Flash shots never switch the layout. |
| **Zoom** | Tightens wide shots and adds a ~10 % punch-in on every jump cut and every few sentences. The zoom is limited by the source resolution, so the picture never gets soft. |
| **Image** | Sub-pixel Lanczos sampling (smooth pans, no pixel stepping), the correct BT.709 matrix in and out, a light sharpen when upscaling, NVENC p7 HQ with AQ at CQ 17 (or x264 slow CRF 16). |
| **Sound** | Rumble cut below 75 Hz, less mud, more presence (for phone speakers), a 2.5:1 compressor, two-pass linear loudnorm to −14 LUFS / −1 dBTP, 48 kHz AAC 256k. |
| **Captions** | Appear ~2 frames before the voice, so they feel in sync. No blinking on short pauses, a minimum on-screen time, soft outline and a "pop" on the highlighted word. |

## Hook scoring

The LLM scores every candidate from 0 to 100 (the rules are in `clippy/playbook.py`, explained in
[playbook.md](playbook.md)):
- **Hook, 25 pts:** the first sentence grabs attention within 2-3 s: a specific claim, a question, tension, or it
  starts mid-action. It never starts with a filler or bridge word.
- **Standalone, 20 pts:** it makes sense without the rest of the video.
- **Payoff, 20 pts:** a complete mini-arc that ends once the punchline has landed and the laugh or reaction after it is over.
- **Emotion, 15 pts:** humour, conflict or surprise.
- **Quotable, 10 pts:** it has a line people would share.
- **Relevance, 10 pts:** one sharp idea that matters to this audience.

Risks (needs context, misquote, claims about a real person, spoiler, background music) lower the score. The hook
text never repeats the first spoken line; it adds curiosity instead, and the clip must deliver what it promises
(ethical clickbait). Each clip also gets a B hook of another type and a comment question.

## Reactions: peaks without words

In gameplay and streams the best moment is often not something said: a clutch kill, a scream, the chat going wild.
Before picking, Clippy reads the whole video for reactions (`clippy/signals.py`):

| Signal | How it is read | Cost |
| --- | --- | --- |
| **Loud** | the sound's level every 0.5 s against its own surroundings (median and median deviation over 90 s), so a stream that gets louder over hours does not count as one long peak; silence never does | a few seconds per hour |
| **Laughter** | laughing words in the transcript (haha, lol, xd, *(laughs)*, nevet) | free |
| **Action** | how much the picture changes between keyframes (only keyframes are decoded, tiny and grey); skipped for videos with a keyframe in nearly every frame, where it would be slow | seconds for a Twitch VOD |
| **Chat** | a YouTube stream replay's chat (downloaded with the video): messages per 5 s against the stream's usual rate, and what the chat wrote; messages from before the stream are left out | seconds |

The picture is read when the video is already here, or when little is said (less than a third of the time), since
then it is worth waiting for the download. Twitch keeps its VOD chat from downloaders, so Twitch VODs are read by
their sound and picture. The result is cached per video in `data/cache/signals`.

How the picker uses them:
- Transcript lines get a mark like `⟪loud, chat x6⟫`, and the picker is told to start a few seconds before such a
  reaction and end after it lands.
- The strongest reactions that no transcript window proposed become candidates of their own: the build-up first
  (60 %), then the reaction. They compete with the spoken moments in the global re-rank, which sees the reactions and
  the chat's words of every candidate.
- A clip with little or no speech gets its title, description and question written from its reactions and the
  video's title, and its ending is not moved by sentences.
- A long video without any speech no longer stops: its moments come from the reactions alone.

## Where a clip starts and ends

A clip starts where a sentence starts and ends where a thought is complete (`clippy/boundaries.py`):
- **The end is a sentence end:** a sentence end that the next sentence does not carry on. If the next sentence
  starts with a connector ("and", "but", "because", "és", "de", "mert"…), or the clip ends on a question answered
  right away or on a colon, the clip runs on.
- **Without punctuation:** in streams and music without punctuation, a pause of at least 0.7 s counts as an end.
- **Starts:** a clip never starts on a connector without the sentence before it.
- **Length:** if no complete ending fits the length setting, the clip may run up to 12 s longer rather than stop
  mid-sentence.
- **The topic sets the length:** inside the length limits (default 20-90 s) a clip runs from where its topic,
  story or rant starts to where it is finished; 30 s is not a target.
- **Start check:** an LLM check reads the sentences said before each chosen clip. When the clip drops into the
  middle of a story or a rant, its start moves back by whole sentences (up to 4) to where that topic starts, as
  long as the clip stays within the limit and off the other clips; a start on filler moves forward.
- **Payoff check:** an LLM check then looks at each chosen clip's last sentences and what follows. While the same
  topic still goes on, the end moves on by whole sentences (up to 4, in two rounds), never into the middle of one.
  When the recording itself is cut off mid-sentence, the unfinished words at the end are dropped.
- **Chat edits:** moving a clip's start or end in the chat lands on whole sentences too, but never further than
  6 s from where it was asked to be: in fast speech without punctuation or pauses it lands on the nearest word
  instead. A cut given in seconds that falls inside a sentence moves to that sentence's nearer edge, so the
  sentence goes or stays whole. A cut that falls in a pause stays where it is, and a cut given by words stays
  word-exact.
- **Stretched timings:** a word whose timing swallowed a silence (most of a second) does not take away the air
  after the word before it, so a clip never ends on a clipped last word.

On the cached GTA stream transcript, 200 random picks ended mid-thought 60% of the time before this and 0% after.

## Measuring the picks: `clippy eval`

A faster picker is only better when it still finds the moments worth posting. `clippy eval` runs the picker
(`find_highlights`, with the AI set by `clippy llm`) on the cached transcripts of a **golden set**, scores the picks
against the moments labelled good in it and times every step, so two picker versions can be compared on the same
videos. No video is downloaded or transcribed: a video whose transcript is not in `data/cache/transcripts` is skipped.

```bat
clippy eval --bootstrap                       :: first golden set from the clips you kept (data/projects/*/clips.json)
clippy eval                                   :: run it: a table, and a report in data/eval/reports/<time>.json
clippy eval --video Pbho1EO5ou8 --llm none    :: one video, with the heuristic picker
clippy eval --compare before.json after.json  :: two reports side by side
```

### The golden set

`data/eval/golden.json` (per user, not in the repository; `--golden` picks another file). Cache file names are
relative to `data/cache/transcripts` and `data/cache/signals` (an absolute path works too):

```json
{
  "format": 1,
  "videos": [
    {
      "id": "Pbho1EO5ou8",
      "title": "The video's title, as the picker sees it",
      "transcript": "Pbho1EO5ou8.e9f5abd279.aligned.json",
      "signals": "Pbho1EO5ou8.v.json",
      "speakers": "Pbho1EO5ou8.speakers.498.json",
      "clips": 5, "min_len": 20, "max_len": 90,
      "moments": [
        {"start": 5.7, "end": 46.06, "grade": 3, "note": "the only-child story"}
      ]
    }
  ]
}
```

- `transcript` is required; `signals` (reactions) and `speakers` (speaker turns) are optional, `null` leaves them out.
- `clips`, `min_len`, `max_len`: what the picker is asked for (defaults 5, 20, 90).
- `moments`: the good moments, in seconds. `grade` is optional: 3 = must find, 2 = good (the default), 1 = fine.
  The list does not have to be complete, but a pick that matches no label counts against precision.
- `--bootstrap` adds every video of `data/projects/*/clips.json` that has a cached transcript (its newest aligned
  one), with its clips as moments of grade 2. Videos already in the file are never changed, so run it again after
  new projects. Then edit the file: delete the weak moments, grade the rest and add the ones the picker missed.

The tests use a tiny synthetic set in `tests/fixtures/eval/`.

### What it measures

| Metric | Meaning |
|---|---|
| **R@1, R@3, R@5, recall** | share of the labelled moments found among the first 1, 3, 5 or all picks (in the picker's order) |
| **graded recall** | the same over all picks, each moment weighed by its grade |
| **precision** | share of the picks that match a labelled moment (videos with labels only) |
| **start ok** | share of the picks that start where a sentence starts (`boundaries.py`) |
| **end ok** | share of the picks that end where a thought is complete (a complete sentence or a clear pause) |
| **mid-cut** | share of the picks with an edge inside a sentence or inside a word |
| **steps** | LLM calls, seconds and failed calls of the window picks, the re-rank, the start and end checks, the post texts, the hook polish and the proofreading, and the seconds spent without the LLM |

A pick matches a moment when they share at least half of the shorter of the two (`--overlap` changes it). The
total row pools every video, so each label and each pick weighs the same. The rates leave out the landing after the
last word, which the full pipeline adds when it has the sound.
