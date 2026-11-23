import dataclasses

from clippy.boundaries import COMPLETE_SENTENCE, END_OVERFLOW, OPEN_SENTENCE, Boundaries, end_time
from clippy.highlights import Clip, _check_endings, _check_starts, _snap


def speech(text: str, start: float = 0.0, step: float = 0.5, pause_after: dict | None = None) -> list[dict]:
    """Words of `text`, one every `step` seconds; pause_after {index: seconds} inserts silences."""
    words, t = [], start
    for i, word in enumerate(text.split()):
        words.append({"word": word, "start": round(t, 3), "end": round(t + step * 0.8, 3)})
        t += step + (pause_after or {}).get(i, 0.0)
    return words


def ending(clip: Clip, words: list[dict]) -> tuple[str, str]:
    inside = [w["word"] for w in words if clip.start - 0.01 <= w["start"] <= clip.end]
    after = [w["word"] for w in words if w["start"] > clip.end]
    return " ".join(inside[-3:]), (after[0] if after else "")


GTA = ("Tropical storms are confirmed. The map floods during heists. Honestly, that might be the most GTA sentence "
       "in this whole video. Here is the rest of the rumor. ") * 3


def test_a_short_pick_runs_on_to_the_end_of_its_sentence_not_to_the_minimum():
    words = speech(GTA)
    cut = next(w for w in words if w["word"] == "might")
    clip = _snap(Clip(0.0, cut["end"], 50), words, min_len=8, max_len=40)
    assert ending(clip, words)[0].endswith("whole video.")
    assert clip.end - clip.start > 8


def test_questions_get_their_answer_and_connectors_carry_the_thought():
    words = speech("Why did he quit? Because the money was gone. And then he moved abroad. New topic starts here.")
    bounds = Boundaries(words)
    level = {w["word"]: bounds.level[i] for i, w in enumerate(words)}
    assert level["quit?"] == OPEN_SENTENCE and level["gone."] == OPEN_SENTENCE
    assert level["abroad."] == COMPLETE_SENTENCE
    clip = _snap(Clip(0.0, words[3]["end"], 50), words, min_len=1, max_len=20)
    assert ending(clip, words)[0].endswith("moved abroad.")
    hu = speech("Ez a legjobb rész. De ezt senki nem tudja. Másik téma jön.")
    assert ending(_snap(Clip(0.0, hu[3]["end"], 50), hu, 1, 20), hu)[0].endswith("nem tudja.")


def test_without_punctuation_a_clear_pause_is_the_end():
    words = speech("we push in from the left flank now and grab the car they never saw it coming "
                   "next round we try the bridge", pause_after={16: 1.2})
    clip = _snap(Clip(0.0, 5.0, 50), words, min_len=4, max_len=30)
    assert ending(clip, words) == ("saw it coming", "next")


def test_finishing_the_thought_may_pass_the_maximum_only_a_little():
    words = speech("This is one very long sentence " + "that keeps going " * 20 + "and finally ends here. Next.")
    clip = _snap(Clip(0.0, 10.0, 50), words, min_len=5, max_len=20)
    assert clip.end - clip.start <= 20 + END_OVERFLOW + 0.5
    last, _ = ending(clip, words)
    assert last.split()[-1] in {w["word"] for w in words}


def test_a_start_on_a_connector_takes_the_sentence_before_it():
    words = speech("The car is fast. And it can mark weak points on enemies. Then it ends.")
    i = next(i for i, w in enumerate(words) if w["word"] == "And")
    clip = _snap(Clip(words[i]["start"], words[-1]["end"], 50), words, min_len=3, max_len=30)
    assert clip.start <= words[0]["start"] + 0.01


def test_trim_lands_on_whole_sentences(timeline, ctx):
    from clippy.editing.operations.structure import Trim

    words = speech("Intro words here. " * 30, start=80.0, step=0.7)
    ctx = dataclasses.replace(ctx, source_words=tuple(words))
    tl, _ = Trim(start_shift=-2.0, end_shift=1.0).apply(timeline, ctx)
    start = next(w for w in words if abs(w["start"] - tl.cut.source.start) < 0.25)
    end = next(w for w in words if abs(w["end"] - tl.cut.source.end) < 0.45)
    assert start["word"] == "Intro" and end["word"] == "here."
    assert tl.cut.source.start <= 98.0 and tl.cut.source.end >= 121.0


class Reply:
    def __init__(self, reply):
        self.reply = reply

    def chat_json(self, *args, **kwargs):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_the_payoff_check_moves_endings_only_by_whole_sentences():
    words = speech(GTA)
    bounds = Boundaries(words)
    clips = [_snap(Clip(0.0, 6.0, 50), words, 2, 30, bounds), _snap(Clip(14.0, 22.0, 50), words, 2, 30, bounds)]
    first, second = clips[0].end, clips[1].end
    _check_endings(Reply({"clips": [{"id": 0, "verdict": "extend", "sentences": 1},
                                    {"id": 1, "verdict": "trim"}]}), clips, words, bounds, 2, 30, "English")
    assert clips[0].end > first and ending(clips[0], words)[0].endswith(".")
    assert clips[1].end < second and ending(clips[1], words)[0].endswith(".")
    before = [(c.start, c.end) for c in clips]
    for garbage in ({"clips": [{"id": 7, "verdict": "extend"}, {"id": "x"}, {"verdict": "trim"}]}, "nonsense",
                    RuntimeError("model gone")):
        _check_endings(Reply(garbage), clips, words, bounds, 2, 30, "English")
    assert [(c.start, c.end) for c in clips] == before


class WindowLLM:
    """Answers every transcript window with the same moments; counts the window calls."""

    backend = "ollama"

    def __init__(self, spans):
        self.spans, self.windows, self.systems = spans, 0, []

    def describe(self):
        return "fake"

    def chat_json(self, system, prompt, **kwargs):
        if not prompt.startswith("TRANSCRIPT:"):
            return {}
        self.windows += 1
        self.systems.append(system)
        return {"clips": [{"start": a, "end": b, "score": 90 - i, "hook": "h", "hook_type": "question",
                           "title": f"t{i}", "hashtags": [], "risks": [], "reason": "r"}
                          for i, (a, b) in enumerate(self.spans)]}


def test_more_clips_reuse_the_window_answers_until_they_run_out(tmp_path):
    from clippy.highlights import PickCache, find_highlights

    words = speech(GTA * 4)
    transcript = {"segments": [{"words": words}], "language": "en"}
    llm = WindowLLM([(0.0, 9.0), (20.0, 29.0), (40.0, 49.0)])
    cache = PickCache(tmp_path, "vid")
    first = find_highlights(transcript, llm, max_clips=1, min_len=5, max_len=15, cache=cache)
    asked = llm.windows
    second = find_highlights(transcript, llm, max_clips=1, min_len=5, max_len=15, exclude=first, cache=cache)
    assert llm.windows == asked and second and second[0].start != first[0].start
    assert sorted(p.name.split(".")[0] for p in tmp_path.iterdir()) == ["vid"] * asked
    find_highlights(transcript, llm, max_clips=2, min_len=5, max_len=15, exclude=first + second, cache=cache)
    assert llm.windows == 2 * asked and "already clips" in llm.systems[-1]


def test_all_clips_of_a_video_share_the_shortest_topic():
    from clippy.highlights import one_topic

    clips = [Clip(0, 1, 1, topic="GTA 6 character design"), Clip(0, 1, 1, topic="GTA 6"), Clip(0, 1, 1)]
    one_topic(clips)
    assert [c.topic for c in clips] == ["GTA 6"] * 3


def test_texts_for_existing_clips_take_only_valid_answers():
    from clippy.highlights import write_texts

    words = speech(GTA)
    clips = [Clip(0.0, 8.0, 50, title="old"), Clip(10.0, 18.0, 50, title="old 2")]
    llm = Reply({"clips": [{"id": 1, "title": "Senki nem számított erre", "description": "A vihar mindent  átír."},
                           {"id": 9, "title": "x", "description": "y"}, {"title": "no id"}]})
    assert write_texts(llm, clips, words, "GTA", "Hungarian") == 1
    assert (clips[0].title, clips[1].title, clips[1].description) == (
        "old", "Senki nem számított erre", "A vihar mindent átír.")


def test_an_ending_never_grows_into_another_picked_clip():
    words = speech(GTA)
    bounds = Boundaries(words)
    first = _snap(Clip(0.0, 6.0, 50), words, 2, 30, bounds)
    neighbour = Clip(first.end + 0.5, first.end + 20.0, 50)
    before = first.end
    _check_endings(Reply({"clips": [{"id": 0, "verdict": "extend", "sentences": 2}]}), [first, neighbour], words,
                   bounds, 2, 30, "English")
    assert first.end == before


RANT = ("this is a joke you guys are all going into five man groups getting to level sixty in one week and you farm "
        "four thousand gold after that playing a class everybody can play it makes me sick ")


def test_a_rant_without_punctuation_never_sends_a_trim_far_away():
    words = speech(RANT * 3, step=0.3)
    bounds = Boundaries(words)
    t = words[60]["end"]
    for later in (False, True):
        end = bounds.end_at(t, later=later)
        start = bounds.start_at(t, later=later)
        assert end is not None and abs(end - t) <= 6.5
        assert start is not None and abs(start - t) <= 6.5


def test_a_clip_that_drops_into_a_story_moves_its_start_to_where_the_story_begins():
    words = speech(GTA)
    bounds = Boundaries(words)
    clip = _snap(Clip(14.0, 22.0, 50), words, 2, 40, bounds)
    first_word = next(w["word"] for w in words if w["start"] >= clip.start - 0.01)
    _check_starts(Reply({"clips": [{"id": 0, "verdict": "extend", "sentences": 2}]}), [clip], words, bounds, 2, 40,
                  "English")
    moved = next(w for w in words if w["start"] >= clip.start - 0.01)
    assert moved["word"] != first_word and moved["word"][0].isupper()
    assert clip.end - clip.start <= 40 + END_OVERFLOW
    start = clip.start
    _check_starts(Reply({"clips": [{"id": 0, "verdict": "extend", "sentences": 4}]}), [clip], words, bounds, 2,
                  clip.end - clip.start - END_OVERFLOW, "English")
    assert clip.start == start


def test_an_ending_runs_on_while_the_topic_goes_on():
    words = speech(GTA)
    bounds = Boundaries(words)
    clip = _snap(Clip(0.0, 6.0, 50), words, 2, 60, bounds)
    before = clip.end
    moved = _check_endings(Reply({"clips": [{"id": 0, "verdict": "extend", "sentences": 4}]}), [clip], words,
                           bounds, 2, 60, "English")
    assert moved == [clip] and clip.end > before
    sentences = sum(1 for w in words if before < w["end"] <= clip.end and w["word"].endswith("."))
    assert sentences >= 3


def test_a_recording_cut_off_mid_sentence_loses_the_unfinished_words():
    words = speech("it just makes me sick i am not gonna lie makes me", step=0.4)
    bounds = Boundaries(words)
    clip = Clip(0.0, words[-1]["end"] + 0.4, 50)
    _check_endings(Reply({"clips": [{"id": 0, "verdict": "trim", "words": 2}]}), [clip], words, bounds, 2, 60,
                   "English")
    assert ending(clip, words) == ("not gonna lie", "makes")


def test_the_end_gets_its_air_when_the_next_word_swallowed_a_silence():
    words = [{"word": "lie", "start": 10.0, "end": 10.3}, {"word": "makes", "start": 10.3, "end": 11.3}]
    assert end_time(words, 0) == 10.7
    words[1]["end"] = 10.6
    assert end_time(words, 0) == 10.3


def test_a_recording_that_ends_on_a_finished_sentence_keeps_it():
    words = speech("you have got to be kidding me. it is going to start me from all the way back.", step=0.4)
    bounds = Boundaries(words)
    clip = Clip(0.0, words[-1]["end"] + 0.4, 50)
    _check_endings(Reply({"clips": [{"id": 0, "verdict": "trim", "words": 3}]}), [clip], words, bounds, 2, 60,
                   "English")
    assert ending(clip, words) == ("the way back.", "")


def test_a_pause_inside_a_sentence_is_no_place_to_end_or_start():
    from clippy.boundaries import NONE

    words = speech("So I went to the shop. And then I saw the biggest dog of my life. It was huge.",
                   pause_after={4: 1.2, 13: 1.0})
    bounds = Boundaries(words)
    the = next(i for i, w in enumerate(words) if w["word"] == "the")
    biggest = next(i for i, w in enumerate(words) if w["word"] == "biggest")
    assert bounds.level[the] == NONE and not bounds.opens[the + 1]
    assert bounds.level[biggest - 1] == NONE
    plain = speech("we waited for hours and nobody came we just sat there", pause_after={3: 1.0})
    assert Boundaries(plain).level[3] >= 0


def test_a_lower_case_word_after_a_dramatic_pause_carries_the_sentence_on():
    from clippy.boundaries import NONE

    words = speech("He told me. I could not believe it. And the winner is you. Wow.", pause_after={9: 1.4})
    i = next(i for i, w in enumerate(words) if w["word"] == "is")
    words[i + 1]["word"] = "you."
    bounds = Boundaries(words)
    assert bounds.cased and bounds.level[i] == NONE
    long_pause = speech("He told me. I could not believe it. And the winner is you. Wow.", pause_after={9: 2.5})
    assert Boundaries(long_pause).level[9] == NONE


def test_a_clip_stays_open_until_the_laugh_has_died_down():
    import numpy as np

    from clippy.boundaries import BEAT, MAX_LANDING, landing

    hop, speech_s = 0.01, 2.0
    talk = np.full(int(speech_s / hop), -20.0)
    laugh = np.concatenate([np.full(120, -22.0), np.full(200, -60.0)])
    assert 1.2 <= landing(np.concatenate([talk, laugh]), hop, speech_s, room=5.0) <= 1.6
    quiet = np.concatenate([talk, np.full(300, -60.0)])
    assert landing(quiet, hop, speech_s, room=5.0) == BEAT
    assert landing(np.concatenate([talk, np.full(400, -21.0)]), hop, speech_s, room=5.0) == MAX_LANDING
    assert landing(np.concatenate([talk, laugh]), hop, speech_s, room=0.3) == 0.3


def test_a_second_check_never_takes_back_what_the_first_found_part_of_the_moment():
    words = speech(GTA)
    bounds = Boundaries(words)
    clip = _snap(Clip(0.0, 6.0, 50), words, 2, 60, bounds)
    floors = {}
    moved = _check_endings(Reply({"clips": [{"id": 0, "verdict": "extend", "sentences": 1}]}), [clip], words,
                           bounds, 2, 60, "English", floors=floors)
    floors |= {id(c): c.end for c in moved}
    extended = clip.end
    _check_endings(Reply({"clips": [{"id": 0, "verdict": "trim", "sentences": 1}]}), moved, words, bounds, 2, 60,
                   "English", floors=floors)
    assert clip.end == extended


def test_an_abbreviation_or_an_initial_is_no_sentence_end():
    from clippy.text import ends_sentence
    assert not ends_sentence("dr.") and not ends_sentence("Mrs.") and not ends_sentence("J.")
    assert not ends_sentence("2026.", "november") and ends_sentence("2026.", "Then")
    assert ends_sentence("King.") and ends_sentence("thesis.") and ends_sentence("ID?")
    words = speech("they came to hear dr. King speak that day. Nobody sent invitations.", pause_after={4: 0.8})
    bounds = Boundaries(words)
    assert bounds.level[4] < OPEN_SENTENCE and bounds.level[8] == COMPLETE_SENTENCE


def test_a_comma_before_a_pause_keeps_the_clause_open():
    words = speech("Keep it an absolute bean with you bro. Next, we are taking a classic map back.",
                   pause_after={8: 2.5})
    bounds = Boundaries(words)
    assert bounds.level[8] < OPEN_SENTENCE and not bounds.opens[9]


def test_a_list_of_parallel_lines_is_not_over_after_its_first_line():
    words = speech("Her room was full of people like me. People who could not sit still. People who had to move "
                   "to think. That was the day she found her calling.")
    bounds = Boundaries(words)
    still = next(i for i, w in enumerate(words) if w["word"] == "still.")
    think = next(i for i, w in enumerate(words) if w["word"] == "think.")
    assert bounds.level[still] == OPEN_SENTENCE and bounds.level[think] == COMPLETE_SENTENCE


def test_two_picks_of_one_thought_become_one_clip_or_the_stronger_one():
    from clippy.highlights import join_touching
    first, second, apart = Clip(398.2, 435.9, 95), Clip(435.7, 463.8, 90), Clip(561.1, 588.4, 93)
    kept = join_touching([first, apart, second], 60)
    assert kept == [first, apart] and (first.start, first.end) == (398.2, 463.8)
    long_a, long_b = Clip(0.0, 30.0, 80), Clip(30.5, 58.0, 92)
    assert join_touching([long_a, long_b], 30) == [long_b] and long_b.start == 30.5


def test_a_clip_may_run_past_the_maximum_only_by_a_quarter():
    from clippy.boundaries import overflow
    assert overflow(10) == 3.0 and overflow(30) == 7.5 and overflow(90) == 12.0
