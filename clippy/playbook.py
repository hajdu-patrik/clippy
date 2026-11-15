"""Short-form best practices (docs/playbook.md) as data: the one source for every prompt about clips."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HookType:
    key: str
    rule: str
    shape: str


HOOK_TYPES: tuple[HookType, ...] = (
    HookType("cold_open", "the clip starts mid-thought on the speaker's strongest, most specific sentence",
             "[guest] on why [counterintuitive claim]"),
    HookType("tension", "a position viewers want to argue with (real tension, not rage bait)",
             "most [group] get [X] wrong"),
    HookType("open_loop", "a surprising act and its stake, resolved later in the clip",
             "he did [X] and it cost him [Y]"),
    HookType("payoff_first", "the result or reaction before its cause; best for clips under 20 s",
             "what happened after [X]"),
    HookType("benefit", "a direct benefit for a specific viewer", "if you [situation], do this instead"),
    HookType("question", "a specific question the clip answers, never a generic 'did you know'",
             "why did [name] stop [X]?"),
    HookType("reaction", "the peak of a reaction, outburst or surprise that the clip shows",
             "nobody expected this"),
    HookType("contrarian", "the speaker contradicts the common belief", "everyone says [X]; [name] says the opposite"),
)
HOOK_KEYS = tuple(h.key for h in HOOK_TYPES)

RISK_PENALTY = {"needs_context": 10, "misquote": 20, "defamation": 30, "spoiler": 10, "copyrighted_music": 10}
RISK_KEYS = tuple(RISK_PENALTY)
RISK_RULES = {
    "needs_context": "a new viewer needs earlier parts of the video to follow it",
    "misquote": "cut out of context, the speaker would seem to say something they did not mean",
    "defamation": "makes damaging claims about a real, named person or company",
    "spoiler": "reveals a plot ending, game story or result people may not want spoiled",
    "copyrighted_music": "a song or game radio plays clearly under the speech",
}

CONNECTORS = frozenset({
    "and", "but", "because", "cause", "so", "which", "or", "then", "plus", "although", "though", "unless", "until",
    "és", "de", "mert", "hogy", "szóval", "ugyanis", "viszont", "tehát", "vagyis", "azaz", "meg", "aztán", "pedig",
    "hiszen", "mivel", "ezért", "így", "illetve", "valamint", "sőt", "amikor", "ami", "amit", "aki", "ahol",
})
HOOK_MAX_WORDS = 7
QUESTION_MAX_WORDS = 12
PAYOFF_FIRST_MAX_SECONDS = 20
CAPTION_SIDE_MARGIN = 120


def adjusted_score(score: float, risks: list[str] | tuple[str, ...]) -> float:
    """The LLM's score minus the risk penalties, so a risky clip only wins when it is clearly better."""
    return max(0.0, score - sum(RISK_PENALTY.get(r, 0) for r in set(risks)))


def known_risks(values) -> list[str]:
    return [v for v in dict.fromkeys(str(x).strip().lower() for x in values or ()) if v in RISK_PENALTY]


def known_hook_type(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in HOOK_KEYS else ""


_LABEL = re.compile(r"^[^:\n]{1,30}:\s+(?=\S)")
_QUOTES = "\"'„“”«»‚‘’"


def tidy_hook(text: str) -> str:
    """Hooks are the viewer's curiosity, not a label or a quote: drop a "Name:" prefix and wrapping quotes."""
    text = _LABEL.sub("", (text or "").strip(), count=1).strip()
    if len(text) > 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text


def hook_guide() -> str:
    return "\n".join(f"  - {h.key}: {h.rule} (shape: \"{h.shape}\")" for h in HOOK_TYPES)


def risk_guide() -> str:
    return "\n".join(f"  - {key}: {rule}" for key, rule in RISK_RULES.items())


def selection_rules(min_len: int, max_len: int) -> str:
    """The rubric and rules the clip-picking prompt scores candidates with."""
    return f"""Score each candidate 0-100 with this rubric:
- HOOK (25): the hook is the clip's own first 2-3 seconds, what is said or shown there: a funny moment, a bold
  or surprising statement, a specific number, real tension, a direct question, the middle of an action or a
  demonstration. Most viewers who leave, leave before the third second, so the clip starts ON that moment.
- STANDALONE (20): a new viewer understands it without the rest of the video; no unresolved "he/that/it".
- PAYOFF (20): a complete arc (setup -> tension -> payoff, punchline or insight) that ends right after the payoff.
- EMOTION (15): strong emotion, humour, conflict, vulnerability or a surprising twist.
- QUOTABLE (10): a line people would share, comment on or stitch.
- RELEVANCE (10): matters to the people who watch this kind of video; one sharp idea, not a summary.

Rules:
- Length between {min_len} and {max_len} seconds, long enough for the whole arc: strong clips usually run
  25-45 s, a story may use the maximum. Never drop the setup or the payoff to make a clip shorter.
- Under {PAYOFF_FIRST_MAX_SECONDS} s the payoff may come first; longer narrative clips open on the tension and
  land the payoff later, then stop within about 2 seconds.
- Reject windows that start with filler or bridge words ("so", "anyway", "as I said", "um", "szóval", "hát", "na",
  "ugye", "és"), start or end mid-sentence, need earlier context, or whose payoff falls after the cut.
- Never pick a moment only because it is loud; the promise of the opening must be paid off inside the clip.
- The clip ENDS on the punchline, the conclusion or the answer, at the end of a sentence. Never end on a sentence
  that sets up, lists or asks something that comes after it. A transcript line may be only part of a long sentence:
  then the clip ends where that sentence ends, not where the line ends."""


def writing_rules(text_language: str) -> str:
    """How the prompt should write the opening text, its A/B pair, the comment question, the title and the hashtags."""
    return f"""On-screen and post texts, written by a native {text_language} social media editor:
- "hook": the opening TEXT shown over the first 3 seconds, supporting the hook moment the clip opens on; at most
  {HOOK_MAX_WORDS} words (ideally 4-6). It adds a
  DIFFERENT layer than the first spoken line (the captions already repeat the speech): the why-you-should-care or
  the open question. Honest: the clip pays it off with the speaker's own words (ethical clickbait, no lies, no
  quotes the speaker did not say). Do NOT restate the video title: every hook is specific to its own clip's
  payoff. Do NOT prefix it with the speaker's or show's name ("Danny: ...") and do not wrap it in quotes; no emojis.
- "hook_type": which kind of hook the clip's opening is, one of:
{hook_guide()}
- "alt_hook": a second opening text for an A/B test, same rules, a different angle than "hook".
- "question": one short question (max {QUESTION_MAX_WORDS} words) addressed to the VIEWER about their own
  opinion or experience, tied to this clip, that they want to answer in the comments ("Would you have taken the
  deal?", "Te mit tettél volna a helyében?"); not a quiz about the speaker; never "like and follow".
- "title": the post title, max 70 characters: a curiosity-gap headline people must click (honest clickbait: a
  bold claim, a surprising number, an unfinished story, "nobody expected…"), the topic or guest keyword early (it
  is searchable); it never promises what the clip does not show.
- "description": 1-2 short sentences under the title that tease what happens and why it matters, without giving
  away the payoff; no hashtags, no links, no emojis.
- "hashtags": 3-5 specific ones (topic, guest, niche); no generic #fyp / #viral.
- "risks": the risks that apply, from:
{risk_guide()}
  An empty list when none applies."""


def editor_rules() -> str:
    """Craft rules for the clip editor: what a good change looks like when the request leaves room."""
    return f"""Craft rules (apply them when the request leaves room, e.g. "make it better / more viral"):
- Hook: the clip's first 3 s themselves (start on the funny, bold or surprising moment, cut what comes before).
- Opening text (layer "hook"): optional, at most {HOOK_MAX_WORDS} words, adds curiosity the speech does not, honestly
  paid off; a short video that already opens strongly (a TikTok, a selfie video) needs none.
- CTA: after the payoff, short and low-friction: a comment question or "Part 2 on my profile"; never a hard sell.
- Text placement: platform buttons cover the bottom ~20% and the right edge; keep texts at the top or centre and
  never over the face.
- Title cards and logo intros in the first seconds lose viewers; keep an intro under 1.5 s or skip it.
- Music: talking clips rarely need it; if added, quiet (about -18 to -24 dB), ducked under speech, royalty-free.
- Logos: small, in a corner; your own logo is fine, other apps' watermarks hurt reach.
- End about 2 s after the payoff; trimming a slow start helps more than any effect."""


def advice() -> str:
    """The playbook the chat agent answers strategy questions from."""
    return """PLAYBOOK (for strategy questions; numbers are mostly vendor estimates – say so – and the user's own
retention graph decides):
- First 3 s decide: open on a specific claim, tension or mid-action; no title cards, logos or slow intros.
  Hook text adds curiosity the speech does not, max 7 words, honestly paid off.
- Length: shortest that delivers one complete idea; test 20-45 s, stories up to 60-90 s. One Shorts study: median
  views peak at 11-20 s. TikTok Creator Rewards pays only for original videos over 1 minute.
- Captions on (many watch muted), word by word, readable, out of the bottom ~20% and right ~15%; do not highlight
  every word. Minimal styles are replacing the loud neon look.
- Pacing: cut dead air and fillers; a visual change (punch-in, second speaker, B-roll) every 3-6 s.
- CTA after the payoff: a comment question or "part 2 on my profile"; the full-episode link in the pinned comment.
- Posting: consistency wins; 2-5 posts a week gave a strong lift in a Buffer study, clip channels do 1-3 a day;
  2-5 specific hashtags, the guest/topic keyword in the title; best times from your own analytics.
- Originality: clip your own content or with permission (or an official clipping campaign); add real editing or
  commentary and credit the source. Plain re-uploads lose reach on YouTube, Instagram and TikTok; never post
  another app's watermark to Reels.
- Music: popular songs may be muted on business accounts; copy a trend's format, not its audio; music triggers
  Content ID.
- Measure: a drop before 5 s = hook problem, a steady drop at 10-30 s = weak middle. Post the same moment with the
  two hooks (A/B) about a week apart. Watch viewed-vs-swiped (Shorts), skip rate (Instagram), completion (TikTok).
- Trends: 15 minutes a week in TikTok Creative Center (Hungary plus a bigger market) and YouTube Studio Trends.
- Game footage (e.g. GTA VI): Rockstar allows only non-commercial fan use; avoid story spoilers, isolated
  cutscenes and the in-game radio."""
