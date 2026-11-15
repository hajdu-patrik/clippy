import subprocess
from pathlib import Path

import pytest

from clippy.editing.render.graph import Expr, Filter, FilterGraph, escape
from clippy.tools import ffmpeg_path

ARIAL = Path("C:/Windows/Fonts/arial.ttf")


def test_values_are_escaped_for_both_parsing_levels():
    assert escape("a:b") == "a\\\\:b"
    assert escape("x,y;[z]") == "x\\,y\\;\\[z\\]"
    assert escape("it's") == "it\\\\\\'s"
    assert escape(True) == "1" and escape(0.1234567) == "0.123457" and escape(12) == "12"
    with pytest.raises(ValueError):
        Filter.of("eq,format")


def test_graph_numbers_inputs_and_chains_pads():
    g = FilterGraph()
    base = g.input("base.mkv")
    logo = g.input("logo.png", "-loop", "1")
    video = g.chain(base.video, Filter.of("eq", contrast=1.15, saturation=None))
    small = g.chain(logo.video, Filter.of("scale", 320, -1), Filter.of("format", "rgba"))
    video = g.chain([video, small], Filter.of("overlay", x=700, y=48, enable=Expr.between(2, 18)), prefix="v")
    g.output(video)
    assert g.filter_complex() == (
        "[0:v]eq=contrast=1.15[p1];[1:v]scale=320:-1,format=rgba[p2];"
        "[p1][p2]overlay=x=700:y=48:enable='between(t,2.000,18.000)'[v3]")
    assert g.input_args() == ["-i", "base.mkv", "-loop", "1", "-i", "logo.png"]
    assert g.map_args() == ["-map", "[v3]"]


def test_empty_chain_returns_the_source():
    g = FilterGraph()
    pad = g.input("a.mkv").audio
    assert g.chain(pad) == pad and g.filter_complex() == ""


@pytest.mark.skipif(not ARIAL.exists(), reason="needs a Windows system font")
def test_ffmpeg_accepts_hostile_text_and_a_drive_letter_path(tmp_path):
    g = FilterGraph()
    src = g.input("color=c=black:s=320x120:d=0.2", "-f", "lavfi")
    text = "Rész 2: holnap, [új]; it's 100% = jó \\ vége"
    g.output(g.chain(src.video, Filter.of("drawtext", fontfile=ARIAL.as_posix(), text=text, fontsize=20,
                                          fontcolor="white", expansion="none")))
    out = tmp_path / "frame.png"
    cmd = [ffmpeg_path(), "-v", "error", *g.input_args(), "-filter_complex", g.filter_complex(), *g.map_args(),
           "-frames:v", "1", "-y", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr and out.exists()
