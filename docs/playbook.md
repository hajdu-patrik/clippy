# Short-form playbook

What works for clips on TikTok, YouTube Shorts and Instagram / Facebook Reels, and where Clippy applies it. The rules
live in one module, [`clippy/playbook.py`](../clippy/playbook.py): clip picking, hook writing, the chat editor and the
chat assistant all read them from there.

Platforms publish no thresholds. Most numbers below come from vendor blogs and single studies (research from
November 2026); treat them as starting points to test, and let your own retention graph decide.

## The first three seconds

Most viewers who leave, leave before the third second (Instagram measures it as *skip rate*). Strong openings:

| Hook type | What it is | Shape |
| --- | --- | --- |
| cold open | starts mid-thought on the strongest, most specific sentence | *[guest] on why [counterintuitive claim]* |
| tension | a position viewers want to argue with (not rage bait) | *most [group] get [X] wrong* |
| open loop | a surprising act and its stake, resolved later | *he did [X] and it cost him [Y]* |
| payoff first | the result before its cause; clips under 20 s | *what happened after [X]* |
| benefit | a direct benefit for a specific viewer | *if you [situation], do this instead* |
| question | a specific question the clip answers, never "did you know" | *why did [name] stop [X]?* |
| reaction | the peak of a reaction or surprise | *nobody expected this* |
| contrarian | the speaker contradicts the common belief | *everyone says [X]; [name] says the opposite* |

Weak openings: title cards, logos, slow zooms, filler and bridge words ("so", "anyway", "szóval", "hát").

**Hook text** adds a layer the speech does not (the captions already repeat it): the why-you-should-care or the open
question, at most 7 words, honestly paid off with the speaker's own words. No quotes the speaker did not say.

**Clippy:** the picker scores the first sentence (HOOK 25 of 100), rejects windows that start with filler, writes the
hook with its type, keeps it to 7 words, and writes a **B hook** of a different type for an A/B test.

## Picking the moment

Score 0–100: hook 25, standalone 20 (no unresolved "he / that / it"), payoff 20 (setup → tension → payoff, ends right
after it), emotion 15, quotable 10, relevance 10. Risks lower the score: needs context (−10), could be misquoted
(−20), claims about a real person or company (−30), spoiler (−10), copyrighted music in the background (−10).

**Length:** the shortest cut that delivers one complete idea. Test 20–45 s; a story may run 60–90 s. One study of
108 000 Shorts found median views peaking at 11–20 s. Under 20 s the payoff may come first; longer narrative clips
open on the tension and stop about 2 s after the payoff.

**Clippy:** the rubric above, sentence-aligned cuts, a global re-rank that avoids duplicates and risky picks, and the
risks shown on the card (*check before posting*).

## Inside the clip

- Captions on (many watch muted), word by word, readable; do not highlight every word. Minimal styles are replacing
  the loud neon look.
- Safe zone: platform buttons cover the bottom ~20% and the right edge. **Clippy** keeps captions 120 px from the sides
  and above the bottom fifth, the hook at the top.
- Pacing: cut dead air and fillers, a visual change every 3–6 s. **Clippy** removes silences and fillers, punches in
  on jump cuts and on new sentences every few seconds, and keeps the face centred.
- Music: talking clips rarely need it; if added, quiet (−18 to −24 dB), ducked under speech, royalty-free.
  **Clippy** adds music at −20 dB with ducking by default.

## The end and the post

- CTA after the payoff, short and low-friction: a comment question or "part 2 on my profile"; the full-episode link
  goes in the pinned comment, not a spoken ad. A last line that flows back into the first can make the clip loop.
- Title: max 70 characters, the guest or topic keyword early (TikTok counts search value). 2–5 specific hashtags.
- Posting: consistency wins; 2–5 posts a week gave a strong lift in a Buffer study of 100 000+ creators, clip
  channels post 1–3 a day. Pick times from your own analytics.

**Clippy:** every clip gets a **comment question**; *Copy text* puts the title, the question and the hashtags on the
clipboard, and the `.txt` next to the video adds the pinned comment with the source link, both hooks, the risks and a
credit reminder. In the chat, "use the B hook" or "put the question at the end" is enough.

## Originality and rights

- Clip your own content, or with permission, or inside an official clipping campaign. YouTube ("inauthentic
  content"), Instagram (aggregator demotion) and TikTok (Creator Rewards: original videos over 1 minute) all reward
  added value: your own edit, commentary, credit to the source.
- Never post another app's watermark to Reels; your own small logo is fine.
- Popular songs may be muted on business accounts and trigger Content ID: copy a trend's format, not its audio.
- Game footage (e.g. GTA VI): Rockstar allows only non-commercial fan use; avoid story spoilers, isolated cutscenes and
  the in-game radio.

## Measuring

A drop before 5 s is a hook problem; a steady drop at 10–30 s is a weak middle. Post the same moment with the two hooks
about a week apart and compare. Watch viewed-vs-swiped (Shorts: Analytics → Content → Shorts), skip rate (Instagram)
and completion (TikTok). For trends, spend 15 minutes a week in TikTok Creative Center (Hungary plus a bigger market)
and YouTube Studio Trends.

Ask the chat any of this ("how long should TikTok clips be?"): the assistant answers from the same playbook.
