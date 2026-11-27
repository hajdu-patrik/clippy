import os
import subprocess
import sys
from pathlib import Path

import pytest

from clippy import analysis, tools
from clippy.analysis import Decoder, decode_cmd, use_nvdec

VIDEO = Path("source.mp4")


def _vf(cmd: list[str]) -> str:
    return cmd[cmd.index("-vf") + 1]


def test_the_cpu_decode_keeps_its_scaler_strip_and_grid():
    cmd = decode_cmd(VIDEO, 12.5, 30.0, 1920, 1080, "30000/1001", "bt709", "tv", columns=(640, 1280))
    assert "-hwaccel" not in cmd
    assert _vf(cmd) == "scale=1920:1080:flags=lanczos:in_color_matrix=bt709:in_range=tv,format=bgr24,crop=640:1080:640:0"
    assert cmd[cmd.index("-ss") + 1] == "12.500" and cmd[cmd.index("-t") + 1] == "30.000"
    assert cmd[cmd.index("-fps_mode") + 1] == "cfr" and cmd[cmd.index("-r") + 1] == "30000/1001"
    assert cmd[-3:] == ["-f", "rawvideo", "-"] and cmd[cmd.index("-avioflags") + 1] == "direct"


REPACK = "scale_cuda=format=yuv420p,hwdownload,format=yuv420p,"


@pytest.mark.parametrize("kw", [{}, {"columns": (1304, 2536)}, {"graph": "split=2[a][b];[a][b]vstack"}])
def test_nvdec_only_decodes_and_every_filter_after_it_is_the_cpu_one(kw):
    cpu = decode_cmd(VIDEO, 3.0, 10.0, 3840, 2160, "60", "bt601", "pc", **kw)
    hw = decode_cmd(VIDEO, 3.0, 10.0, 3840, 2160, "60", "bt601", "pc", hw=True, **kw)
    assert hw[hw.index("-hwaccel") + 1] == "cuda" and hw[hw.index("-hwaccel_output_format") + 1] == "cuda"
    assert hw.index("-hwaccel") < hw.index("-i")
    assert _vf(hw) == REPACK + _vf(cpu)
    assert [a for a in hw if a not in ("-hwaccel", "-hwaccel_output_format", "cuda", _vf(hw))] ==         [a for a in cpu if a != _vf(cpu)]


def test_a_fixed_layout_graph_keeps_its_conversion():
    cpu = _vf(decode_cmd(VIDEO, 0.0, 5.0, 1080, 1920, "30", graph="split=2[a][b];[a][b]vstack"))
    assert cpu == "split=2[a][b];[a][b]vstack,scale=in_color_matrix=bt709:in_range=tv,format=bgr24"


UHD = {"codec": "h264", "pix_fmt": "yuv420p", "rotated": False, "height": 2160}


@pytest.mark.parametrize("change, height, expected", [
    ({}, 760, True),
    ({}, 360, True),
    ({}, 1080, True),
    ({}, 2160, False),
    ({}, 1920, False),
    ({"height": 1080}, 360, False),
    ({"codec": "prores"}, 760, False),
    ({"pix_fmt": "yuv420p10le"}, 760, False),
    ({"pix_fmt": "yuv422p"}, 760, False),
    ({"rotated": True}, 760, False),
])
def test_nvdec_only_takes_large_sources_shrunk_to_half_in_formats_it_has(monkeypatch, change, height, expected):
    monkeypatch.setattr(analysis, "ffmpeg_features", lambda: {"nvdec": True})
    monkeypatch.setattr(analysis, "gpu_busy", lambda: False)
    assert use_nvdec({**UHD, **change}, height) is expected


def test_nvdec_waits_out_a_busy_gpu_and_a_build_without_it(monkeypatch):
    monkeypatch.setattr(analysis, "ffmpeg_features", lambda: {"nvdec": True})
    monkeypatch.setattr(analysis, "gpu_busy", lambda: True)
    assert not use_nvdec(UHD, 760)
    monkeypatch.setattr(analysis, "ffmpeg_features", lambda: {"nvdec": False})
    monkeypatch.setattr(analysis, "gpu_busy", lambda: False)
    assert not use_nvdec(UHD, 760)


def _frames_cmd(first: int, count: int, code: int) -> list[str]:
    """A stand-in decoder: frames numbered from `first`, 6 bytes each, then the exit code."""
    script = ("import sys\n"
              f"for i in range({first}, {first + count}):\n"
              "    sys.stdout.buffer.write(bytes([i]) * 6)\n"
              "sys.stdout.buffer.flush()\n"
              f"sys.exit({code})\n")
    return [sys.executable, "-c", script]


def _read_all(dec: Decoder) -> list[int]:
    got, view = [], memoryview(bytearray(6))
    while dec.read(view):
        assert len(set(view)) == 1
        got.append(view[0])
    return got


def test_a_failing_nvdec_decode_goes_on_from_the_same_frame_on_the_cpu(monkeypatch):
    def cmd(*args, hw=False):
        return _frames_cmd(0, 3, 1) if hw else _frames_cmd(0, 7, 0)

    logged: list[str] = []
    monkeypatch.setattr(analysis, "decode_cmd", cmd)
    monkeypatch.setattr(analysis, "log", logged.append)
    with Decoder(VIDEO, 0.0, 1.0, 2, 1, "30", hw=True) as dec:
        assert _read_all(dec) == [0, 1, 2, 3, 4, 5, 6]
        assert not dec.hw and dec.frames == 7
    assert len(logged) == 1 and "after 3 frames" in logged[0]


def test_a_clean_end_of_an_nvdec_decode_is_the_end(monkeypatch):
    calls: list[bool] = []

    def cmd(*args, hw=False):
        calls.append(hw)
        return _frames_cmd(0, 4, 0)

    monkeypatch.setattr(analysis, "decode_cmd", cmd)
    with Decoder(VIDEO, 0.0, 1.0, 2, 1, "30", hw=True) as dec:
        assert _read_all(dec) == [0, 1, 2, 3]
    assert calls == [True]


def test_a_failing_cpu_decode_is_not_retried(monkeypatch):
    calls: list[bool] = []

    def cmd(*args, hw=False):
        calls.append(hw)
        return _frames_cmd(0, 2, 1)

    monkeypatch.setattr(analysis, "decode_cmd", cmd)
    with Decoder(VIDEO, 0.0, 1.0, 2, 1, "30") as dec:
        assert _read_all(dec) == [0, 1]
    assert calls == [False]


def test_the_frame_pipe_carries_frames_both_ways():
    echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"]
    r, w = tools.frame_pipe()
    try:
        proc, sink = tools.spawn_piped(echo, out=False, stdout=w)
    finally:
        os.close(w)
    payload = bytes(range(256)) * 8192
    sink.write(payload)
    sink.close()
    with open(r, "rb") as back:
        assert back.read() == payload[::-1]
    assert proc.wait() == 0
    proc, source = tools.spawn_piped([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 3000000)"],
                                     out=True)
    buf = memoryview(bytearray(3000000))
    assert tools.read_exact(source, buf) == 3000000 and source.read() == b""
    source.close()
    assert proc.wait() == 0


@pytest.mark.parametrize("out, busy", [("12\n", False), ("50\n", True), ("3\n97\n", True), ("", True)])
def test_the_gpu_counts_as_busy_from_half_load(monkeypatch, out, busy):
    monkeypatch.setattr(tools.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(tools.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    assert tools.gpu_busy() is busy


def test_without_nvidia_smi_the_gpu_is_free(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    assert not tools.gpu_busy()


def test_the_probe_reads_codec_pixel_format_and_rotation(monkeypatch):
    info = ("Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':\n  Duration: 00:01:00.00, start: 0.0, bitrate: 1 kb/s\n"
            "  Stream #0:0[0x1](und): Video: hevc (Main 10) (hvc1 / 0x31637668), yuv420p10le(tv, bt709), "
            "3840x2160, 40000 kb/s, 59.94 fps, 59.94 tbr\n      displaymatrix: rotation of -90.00 degrees\n")
    monkeypatch.setattr(tools, "_ffmpeg_info", lambda path: info)
    meta = tools.probe_video(Path("probe-rotated-hevc.mp4"))
    assert (meta["codec"], meta["pix_fmt"], meta["rotated"]) == ("hevc", "yuv420p10le", True)
    assert (meta["width"], meta["height"]) == (2160, 3840)
    plain = info.replace("hevc (Main 10) (hvc1 / 0x31637668), yuv420p10le(tv, bt709)", "h264 (High), yuv420p")
    plain = plain.replace("      displaymatrix: rotation of -90.00 degrees\n", "")
    monkeypatch.setattr(tools, "_ffmpeg_info", lambda path: plain)
    meta = tools.probe_video(Path("probe-plain-h264.mp4"))
    assert (meta["codec"], meta["pix_fmt"], meta["rotated"]) == ("h264", "yuv420p", False)


@pytest.mark.parametrize("hw", [False, True])
def test_a_render_pumps_every_frame_through_the_frame_pipes(tmp_path, monkeypatch, hw):
    from clippy import render
    from clippy.analysis import ClipAnalysis

    src = tmp_path / "src.mp4"
    subprocess.run([tools.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
    monkeypatch.setattr(render, "use_nvdec", lambda meta, height: hw)
    monkeypatch.setattr(render, "ffmpeg_features", lambda: {"subtitles": True, "nvenc": False, "nvdec": hw})
    words = [{"word": f"w{i}", "start": i * 0.4, "end": i * 0.4 + 0.35} for i in range(7)]
    an = ClipAnalysis(25.0, 75, 640, 360, [[] for _ in range(75)], [0], "haar")
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "clip.mp4"
    settings = render.RenderSettings(layout="center", encoder="x264", skip_burned=False, reactions=False,
                                     tighten=False)
    info = render.ClipRenderer(src, words, 0.0, 3.0, settings).render(out, work, render.prepare_fonts(tmp_path / "f"),
                                                                       an)
    assert info["frames"] == 75
    assert tools.probe_media(out)["video"] == (1080, 1920)
