import io
import subprocess
import wave

import cv2
import numpy as np
import pytest

from clippy.editing import assets as assets_module
from clippy.editing.assets import AssetKind, AssetService, image_dimensions, parse_cube, sniff
from clippy.storage import Storage
from clippy.tools import ClippyError, ffmpeg_path


@pytest.fixture
def service(tmp_path):
    return AssetService(Storage(tmp_path / "data", tmp_path / "library"))


def png(w=40, h=20, alpha=True) -> bytes:
    image = np.zeros((h, w, 4 if alpha else 3), np.uint8)
    image[..., 0] = 200
    return cv2.imencode(".png", image)[1].tobytes()


def wav(seconds=0.5) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(int(16000 * seconds), np.int16).tobytes())
    return out.getvalue()


def cube(size=2) -> str:
    rows = "\n".join("0.0 0.5 1.0" for _ in range(size ** 3))
    return f"TITLE \"warm\"\nLUT_3D_SIZE {size}\n{rows}\n"


def test_images_are_reencoded_to_png_and_keep_their_size(service):
    asset = service.add(1, "C:\\Users\\x\\logo.png", io.BytesIO(png()))
    assert asset.kind == AssetKind.IMAGE and asset.name == "logo.png" and (asset.width, asset.height) == (40, 20)
    assert asset.path.suffix == ".png" and asset.path.parent == service.folder(1)
    assert [a.id for a in service.list(1)] == [asset.id]
    jpg = cv2.imencode(".jpg", np.full((30, 50, 3), 90, np.uint8))[1].tobytes()
    assert service.add(1, "photo.jpg", io.BytesIO(jpg)).path.suffix == ".png"


def test_audio_and_video_are_probed(service, tmp_path):
    audio = service.add(1, "beep.wav", io.BytesIO(wav()))
    assert audio.kind == AssetKind.AUDIO and abs(audio.duration - 0.5) < 0.05
    clip = tmp_path / "outro.mp4"
    subprocess.run([ffmpeg_path(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x284:d=1", "-pix_fmt", "yuv420p",
                    str(clip)], check=True)
    video = service.add(1, "outro.mp4", io.BytesIO(clip.read_bytes()))
    assert video.kind == AssetKind.VIDEO and (video.width, video.height) == (160, 284) and video.duration > 0.9


def test_luts_are_parsed_strictly(service):
    lut = service.add(1, "warm.cube", io.BytesIO(cube().encode()))
    assert lut.kind == AssetKind.LUT and parse_cube(lut.path.read_text()) == 2
    with pytest.raises(ClippyError) as bad:
        service.add(1, "bad.cube", io.BytesIO(cube().replace("0.5", "rm -rf").encode()))
    assert bad.value.key == "asset.broken"


def test_hostile_and_unknown_files_are_rejected(service, monkeypatch):
    with pytest.raises(ClippyError) as unknown:
        service.add(1, "evil.png", io.BytesIO(b"MZ\x90\x00 this is an exe"))
    assert unknown.value.key == "asset.unknown"
    bomb = png()[:16] + (100000).to_bytes(4, "big") + (100000).to_bytes(4, "big") + png()[24:]
    with pytest.raises(ClippyError) as big_image:
        service.add(1, "bomb.png", io.BytesIO(bomb))
    assert big_image.value.key == "asset.broken"
    with pytest.raises(ClippyError) as fake_audio:
        service.add(1, "song.mp3", io.BytesIO(b"ID3" + b"\x00" * 200))
    assert fake_audio.value.key == "asset.broken"
    monkeypatch.setitem(assets_module.LIMITS, AssetKind.IMAGE, 100)
    with pytest.raises(ClippyError) as too_big:
        service.add(1, "large.png", io.BytesIO(png(200, 200)))
    assert too_big.value.key == "asset.too_big"
    assert service.list(1) == [] and not list(service.folder(1).glob(".upload-*"))


def test_remove_and_header_helpers(service):
    asset = service.add(1, "logo.png", io.BytesIO(png()))
    assert service.remove(1, asset.id) and not asset.path.exists() and service.list(1) == []
    assert not service.remove(1, asset.id)
    assert sniff(b"fLaC....") == ("audio", "flac") and sniff(b"\x00\x00\x00\x18ftypM4A ") == ("media", "m4a")
    assert image_dimensions(png(33, 7)) == (33, 7)
    jpg = cv2.imencode(".jpg", np.zeros((12, 34, 3), np.uint8))[1].tobytes()
    assert image_dimensions(jpg) == (34, 12)
