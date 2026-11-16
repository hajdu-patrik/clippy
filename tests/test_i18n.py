from clippy import i18n
from clippy.cli import build_parser
from clippy.text import looks_hungarian
from clippy.tools import ClippyError
from clippy.transcribe import detect_language

def test_errors_carry_a_key_and_an_english_message():
    err = ClippyError(key="error.no_clip", clip=4)
    assert str(err) == "There is no clip #4." and err.params == {"clip": 4}
    assert i18n.t(err.key, **err.params) == "There is no clip #4." and i18n.t("no.such.key") == "no.such.key"
    assert str(ClippyError("plain")) == "plain" and ClippyError("plain").key is None


def test_cli_help_comes_from_the_parser():
    helps = {a.dest: a.help for a in build_parser()._actions}
    assert helps["clips"].startswith("how many clips")


def test_hungarian_post_texts_are_recognised():
    assert looks_hungarian("Csinálj még 3 klipet a vicces részekből") and looks_hungarian("Tedd alá a zenét halkan")
    assert not looks_hungarian("add music under it quietly") and not looks_hungarian("Café scene please")


def test_transcription_keeps_the_video_language_unless_unsure():
    class Model:
        def __init__(self, scores):
            self.scores = scores

        def detect_language(self, audio, **kw):
            return self.scores[0][0], self.scores[0][1], self.scores

    assert detect_language(Model([("fi", 0.5), ("hu", 0.3), ("en", 0.1)]), None) == "hu"
    assert detect_language(Model([("es", 0.92), ("en", 0.05)]), None) == "es"
    assert detect_language(Model([("en", 0.6), ("hu", 0.3)]), None) == "en"

    class Broken:
        def detect_language(self, audio, **kw):
            raise RuntimeError("no speech")

    assert detect_language(Broken(), None) == "en"


def test_translated_captions_keep_the_line_timing():
    from clippy.translate import CaptionTranslator, caption_lines, spread

    words = [{"start": 0.0, "end": 0.4, "word": "Hello"}, {"start": 0.5, "end": 1.0, "word": "everyone."},
             {"start": 1.2, "end": 1.6, "word": "Thanks"}, {"start": 1.7, "end": 2.0, "word": "guys."}]
    assert [len(line) for line in caption_lines(words)] == [2, 2]
    out = spread("Sziasztok mindenki.", words[:2])
    assert out[0]["start"] == 0.0 and out[-1]["end"] == 1.0 and out[0]["end"] == out[1]["start"]

    class LLM:
        def chat_json(self, system, user, think=False, schema=None):
            assert "Hungarian" in system
            return {"hook": "Ezt senki nem mondta", "lines": [{"id": 0, "text": "Sziasztok mindenki."},
                                                            {"id": 1, "text": "Köszi, srácok."}]}

    translated, hook = CaptionTranslator(LLM(), "hu")(words, "Nobody said this")
    assert hook == "Ezt senki nem mondta"
    assert [w["word"] for w in translated] == ["Sziasztok", "mindenki.", "Köszi,", "srácok."]
    assert translated[2]["start"] == 1.2 and translated[-1]["end"] == 2.0

    class Partial:
        def chat_json(self, *a, **k):
            return {"hook": "Szia", "lines": [{"id": 1, "text": "Köszi, srácok."}]}

    partial, _ = CaptionTranslator(Partial(), "hu")(words, "h")
    assert [w["word"] for w in partial] == ["Hello", "everyone.", "Köszi,", "srácok."]


def test_caption_language_is_an_allowed_chat_option():
    from clippy.web import agent

    opts, notes = agent.clean_options({"captions": "hu", "lang": "en"})
    assert opts == {"captions": "hu", "lang": "en"} and not notes
    assert agent.clean_options({"captions": "auto"})[0] == {"captions": None}
    assert "captions" in agent.RENDER_KEYS
    assert agent.clean_options({"hook": "English hook please"})[0] == {}
    assert agent.clean_options({"hook": "igen", "punch": "false"})[0] == {"hook": True, "punch": False}
