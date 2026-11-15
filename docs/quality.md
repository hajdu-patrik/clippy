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
- **Hook, 25 pts:** the first sentence grabs attention within 2–3 s: a specific claim, a question, tension, or it
  starts mid-action. It never starts with a filler or bridge word.
- **Standalone, 20 pts:** it makes sense without the rest of the video.
- **Payoff, 20 pts:** a complete mini-arc that ends right after the punchline.
- **Emotion, 15 pts:** humour, conflict or surprise.
- **Quotable, 10 pts:** it has a line people would share.
- **Relevance, 10 pts:** one sharp idea that matters to this audience.

Risks (needs context, misquote, claims about a real person, spoiler, background music) lower the score. The hook
text never repeats the first spoken line; it adds curiosity instead, and the clip must deliver what it promises
(ethical clickbait). Each clip also gets a B hook of another type and a comment question.

## Where a clip starts and ends

A clip starts where a sentence starts and ends where a thought is complete (`clippy/boundaries.py`):
- **The end is a sentence end:** a sentence end that the next sentence does not carry on. If the next sentence
  starts with a connector ("and", "but", "because", "és", "de", "mert"…), or the clip ends on a question answered
  right away or on a colon, the clip runs on.
- **Without punctuation:** in streams and music without punctuation, a pause of at least 0.7 s counts as an end.
- **Starts:** a clip never starts on a connector without the sentence before it.
- **Length:** if no complete ending fits the length setting, the clip may run up to 12 s longer rather than stop
  mid-sentence.
- **Payoff check:** an LLM check then looks at each chosen clip's last sentences and what follows. It may move the
  end by whole sentences to land the punchline, never into the middle of one.
- **Chat edits:** moving a clip's start or end in the chat lands on whole sentences too.

On the cached GTA stream transcript, 200 random picks ended mid-thought 60% of the time before this and 0% after.
