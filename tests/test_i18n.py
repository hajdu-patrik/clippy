from clippy import i18n
from clippy.cli import build_parser
from clippy.tools import ClippyError
from clippy.transcribe import detect_language

WEB_GROUPS = ("job", "done", "detail", "error", "note", "reply", "chat", "edit", "change", "what", "asset", "hooktype", "risk",
              "post", "daily")


def test_every_web_and_server_text_exists_in_both_languages():
    for lang in i18n.SUPPORTED:
        other = "hu" if lang == "en" else "en"
        missing = [k for k in i18n.CATALOG[lang] if k.split(".")[0] in WEB_GROUPS + ("cli",)
                   and k not in i18n.CATALOG[other]]
        assert not missing, (lang, missing)


def test_detects_the_message_language():
    assert i18n.detect("Csinálj még 3 klipet a vicces részekből") == "hu"
    assert i18n.detect("a 2. klip legyen rövidebb") == "hu"
    assert i18n.detect("make the second clip shorter") == "en"


def test_errors_carry_a_key_and_an_english_message():
    err = ClippyError(key="error.no_clip", clip=4)
    assert str(err) == "There is no clip #4." and err.params == {"clip": 4}
    assert i18n.t("hu", err.key, **err.params) == "Nincs 4. klip."
    assert str(ClippyError("plain")) == "plain" and ClippyError("plain").key is None


def test_cli_help_follows_the_language():
    hu = {a.dest: a.help for a in build_parser("hu")._actions}
    en = {a.dest: a.help for a in build_parser("en")._actions}
    assert hu["clips"].startswith("hány klip") and en["clips"].startswith("how many clips")


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


def test_saved_language_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.delenv("CLIPPY_LANG", raising=False)
    assert i18n.saved_language() is None
    i18n.save_language("HU")
    assert i18n.saved_language() == "hu" and i18n.system_language() == "hu"


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


def test_short_edit_requests_are_detected_too():
    assert i18n.detect("Tedd alá a zenét halkan") == "hu" and i18n.detect("rakd fel a logót") == "hu"
    assert i18n.detect("add music under it quietly") == "en" and i18n.detect("Café scene please") == "en"
