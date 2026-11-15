from clippy import i18n, playbook
from clippy.captions import CaptionStyle, build_ass
from clippy.editing.planner import EditPlanner, EditRequest, PromptFormatter
from clippy.highlights import Clip, find_highlights, parse_clip
from clippy.pipeline import post_text
from clippy.web import agent

WORDS = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(
    ("Senki nem mondta el, hogy ez így megy. " * 30).split())]


class FakeLLM:
    backend = "fake"

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def describe(self):
        return "fake"

    def chat_json(self, system, user, think=False, schema=None):
        self.prompts.append((system, schema))
        return self.reply


def raw(start, end, score, **extra):
    return {"start": start, "end": end, "score": score, "hook": "Erről senki nem beszél", "hook_type": "tension",
            "alt_hook": "Tíz év hazugság", "question": "Te mit tettél volna?", "title": "Cím", "hashtags": ["#a"],
            "risks": [], "reason": "jó", **extra}


def test_every_hook_type_and_risk_has_a_name_in_both_languages():
    for lang in i18n.SUPPORTED:
        for key in [f"hooktype.{k}" for k in playbook.HOOK_KEYS] + [f"risk.{k}" for k in playbook.RISK_KEYS]:
            assert key in i18n.CATALOG[lang], (lang, key)


def test_hooks_lose_name_labels_and_wrapping_quotes():
    assert playbook.tidy_hook("Danny: „A zene változtatta meg az életemet”") == "A zene változtatta meg az életemet"
    assert playbook.tidy_hook('"Nobody tells you this"') == "Nobody tells you this"
    assert playbook.tidy_hook("Why 9:16 wins") == "Why 9:16 wins"
    assert playbook.tidy_hook("Erről senki nem beszél") == "Erről senki nem beszél"


def test_risks_lower_the_score_and_unknown_values_are_dropped():
    assert playbook.adjusted_score(80, ["misquote", "misquote", "spoiler"]) == 50
    assert playbook.adjusted_score(10, ["defamation"]) == 0
    clip = parse_clip(raw(1, 30, 90, hook_type="clickbait", risks=["misquote", "aliens"], alt_hook="Erről senki nem beszél"))
    assert clip.hook_type == "" and clip.risks == ["misquote"] and clip.score == 70 and clip.alt_hook == ""
    assert parse_clip({"start": "x"}) is None


def test_selection_prompt_carries_the_playbook_and_keeps_the_new_fields():
    llm = FakeLLM({"clips": [raw(0, 25, 90, risks=["defamation"]), raw(40, 65, 75)]})
    clips = find_highlights({"segments": [{"words": WORDS}], "language": "hu"}, llm, max_clips=2, title="Teszt")
    system, schema = llm.prompts[0]
    assert "RELEVANCE (10)" in system and "alt_hook" in system and "open_loop" in system
    assert schema["properties"]["clips"]["items"]["properties"]["hook_type"]["enum"] == list(playbook.HOOK_KEYS)
    assert [c.score for c in clips] == [75, 60]
    assert clips[0].hook_type == "tension" and clips[0].alt_hook == "Tíz év hazugság"
    assert clips[0].question == "Te mit tettél volna?" and clips[1].risks == ["defamation"]


def test_post_text_is_ready_to_paste_in_the_clip_language():
    clip = Clip(10, 40, 88, hook="Erről senki nem beszél", title="Miért hagyta abba?", hashtags=["#zene"],
                reason="erős", hook_type="question", alt_hook="Tíz év hazugság", question="Te abbahagytad volna?",
                risks=["misquote"])
    text = post_text(clip, "https://youtu.be/x", {"duration_out": 28.4, "dead_air_removed": 1.2})
    caption, notes = text.split("\n---\n")
    assert caption.strip() == ("Miért hagyta abba?\n\nTe abbahagytad volna?\n\nA teljes videó: https://youtu.be/x"
                               "\n\n#zene")
    assert "Kitűzött komment: A teljes videó: https://youtu.be/x" in notes
    assert "Nyitó felirat ötlet (kérdés): Erről senki nem beszél" in notes and "B nyitó felirat ötlet (A/B teszt): Tíz év hazugság" in notes
    assert "kiragadva félreérthető" in notes
    english = post_text(Clip(0, 20, 50, hook="Nobody tells you this", title="Why he quit"), "u",
                        {"duration_out": 20, "dead_air_removed": 0})
    assert "Pinned comment: Full video: u" in english and "B opening text" not in english
    whole = post_text(Clip(0, 15, 50, title="Why he quit"), "u", {"duration_out": 15, "dead_air_removed": 0})
    assert "Opening text" not in whole
    teaser = Clip(0, 20, 50, title="He lost it all in 3 days", description="Nobody saw the last call coming.",
                  hashtags=["#story"])
    caption = post_text(teaser, "https://youtu.be/x", {"duration_out": 20, "dead_air_removed": 0},
                        ["TikTok: https://www.tiktok.com/@me", "Instagram: https://www.instagram.com/me/"])
    assert caption.split("\n---\n")[0].strip() == (
        "He lost it all in 3 days\n\nNobody saw the last call coming.\n\nFull video: https://youtu.be/x\n\n"
        "Follow me:\nTikTok: https://www.tiktok.com/@me\nInstagram: https://www.instagram.com/me/\n\n#story")


def test_captions_stay_clear_of_the_right_hand_buttons():
    ass = build_ass(WORDS[:6], 0, 3, CaptionStyle())
    margins = f",{playbook.CAPTION_SIDE_MARGIN},{playbook.CAPTION_SIDE_MARGIN},560,1"
    assert next(line for line in ass.splitlines() if line.startswith("Style: Cap,")).endswith(margins)


def test_editor_sees_the_craft_rules_and_the_unused_ideas(timeline, words, asset_index, ctx):
    req = EditRequest("legyen ütősebb", timeline, tuple(asset_index.assets.values()), (), tuple(words), 1, "Cím",
                      "Tíz év hazugság", "Te mit tettél volna?")
    state = PromptFormatter().state(req)
    assert 'Alternative opening text for an A/B test (not on the video): "Tíz év hazugság"' in state
    assert 'Suggested comment question, usable as a CTA: "Te mit tettél volna?"' in state
    llm = FakeLLM({"reply": "Kész.", "operations": [{"kind": "set_hook", "text": "Tíz év hazugság"}]})
    EditPlanner(llm).plan(req, ctx)
    assert "Craft rules" in llm.prompts[0][0]


def test_chat_agent_answers_strategy_questions_from_the_playbook():
    assert "PLAYBOOK" in agent.SYSTEM_PROMPT and "Creative Center" in agent.SYSTEM_PROMPT
