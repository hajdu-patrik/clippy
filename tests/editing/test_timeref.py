import pytest

from clippy.editing.timeref import At, Clock, Edge, Phrase, Span, Word
from clippy.tools import ClippyError


@pytest.fixture
def resolver(ctx):
    return ctx.resolver


def test_absolute_edges_words_and_phrases(resolver):
    assert resolver.at(At(at=4)) == 4
    assert resolver.at(Edge(edge="end", offset=-3)) == 17
    assert resolver.at(Word(word="pénz")) == 5.0
    assert resolver.at(Word(word="pénz", occurrence=2)) == 10.0
    assert resolver.at(Phrase(phrase="nem tud megállítani")) == 0.5


def test_spans_end_at_the_end_of_a_word_or_after_a_length(resolver):
    assert resolver.span(Span(start=Word(word="Pénzt"), end=Word(word="könnyű"))).end == 6.5
    assert resolver.span(Span(start=At(at=18), length=5)).end == 20
    assert resolver.span(Span(start=Edge(edge="end", offset=-3))).start == 17
    assert resolver.span(None) is None


def test_unknown_words_and_times_outside_the_clip_are_errors(resolver):
    with pytest.raises(ClippyError) as missing:
        resolver.at(Word(word="banán"))
    assert missing.value.key == "edit.no_word"
    with pytest.raises(ClippyError) as outside:
        resolver.at(At(at=25))
    assert outside.value.key == "edit.outside"
    with pytest.raises(ClippyError):
        resolver.span(Span(start=At(at=8), end=At(at=3)))


def test_clock_maps_output_time_through_the_kept_intervals():
    clock = Clock(((100.0, 108.0), (110.0, 122.0)))
    assert clock.duration == 20
    assert clock.to_source(3) == 103 and clock.to_source(10) == 112
