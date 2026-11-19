import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from clippy.covers import b_cover, frame_of, make
from clippy.storage import Storage
from clippy.text import post_clean
from clippy.tools import ClippyError
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.handlers import post_of
from clippy.web.jobs import Runner
from clippy.web.kit import StyleKit
from clippy.web.media import MediaStore
from clippy.web.server import create_app


def png(color=(200, 30, 60)):
    out = io.BytesIO()
    Image.new("RGB", (120, 60), color).save(out, "PNG")
    out.seek(0)
    return out


def test_the_kit_sets_the_caption_look_and_checks_its_files(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    kit = StyleKit(storage)
    assert kit.render_options() == {} and kit.operations(1) == []
    kit.save(font="Impact", highlight="green", uppercase=False, position="bottom-left", width=0.9)
    assert kit.render_options() == {"font": "Impact", "highlight": "green", "uppercase": False}
    assert kit.load()["width"] == 0.4
    kit.save(font="Comic Sans", highlight="pink")
    assert kit.render_options() == {}
    kit.put("logo", "logo.png", png())
    with pytest.raises(ClippyError) as caught:
        kit.put("outro", "x.png", io.BytesIO(b"not an image at all"))
    assert caught.value.key in ("asset.unknown", "error.kit_kind")
    [overlay] = kit.operations(7)
    assert overlay.kind == "add_overlay" and overlay.position == "top-right"
    copied = list(storage.chat_assets(7).iterdir())
    assert {p.suffix for p in copied} == {".png", ".json"}
    meta = json.loads(next(p for p in copied if p.suffix == ".json").read_text(encoding="utf-8"))
    assert Path(meta["path"]).parent == storage.chat_assets(7)
    kit.put("logo", "new.png", png((0, 0, 0)))
    assert len(list((storage.web / "kit").glob("*.png"))) == 1
    kit.remove("logo")
    assert kit.operations(8) == [] and not list((storage.web / "kit").glob("*.png"))


def test_an_intro_image_from_the_kit_opens_every_clip_briefly(tmp_path):
    kit = StyleKit(Storage(tmp_path / "data", tmp_path / "library"))
    kit.put("intro", "intro.png", png())
    [intro] = kit.operations(3)
    assert (intro.kind, intro.duration) == ("set_intro", 1.5)
    assert kit.present()["intro"]["name"] == "intro.png"
    kit.remove("intro")
    assert kit.operations(4) == []


def test_covers_carry_the_titles_and_are_made_again_from_the_clean_frame(tmp_path):
    cover = tmp_path / "clip_01_v1.jpg"
    Image.new("RGB", (540, 960), (20, 40, 60)).save(cover, "JPEG")
    assert make(cover, "Ezt senki nem várta", "A B cím más szögből")
    assert frame_of(cover).is_file() and b_cover(cover).is_file()
    first = cover.read_bytes()
    assert make(cover, "Új cím", "")
    assert cover.read_bytes() != first and not b_cover(cover).exists()
    with Image.open(frame_of(cover)) as clean:
        assert max(abs(a - b) for a, b in zip(clean.getpixel((270, 120)), (20, 40, 60))) <= 3


def test_hungarian_quotes_and_the_b_title_for_posting(tmp_path):
    assert post_clean('Azt mondta: "ez az" és “kész”', "hu") == "Azt mondta: „ez az” és „kész”"
    assert post_clean('He said "yes"', "en") == 'He said "yes"'
    row = {"clip": {"start": 0, "end": 10, "score": 50, "title": "A cím", "alt_title": "B cím",
                    "description": "Leírás", "hashtags": ["#x"]}}
    assert post_of(row).title == "A cím" and post_of(row, "b").title == "B cím"


def test_kit_routes(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    client = TestClient(create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False),
                        headers={"X-Clippy-Request": "1"})
    assert client.get("/api/kit").json()["fonts"] == ["Arial Black", "Impact", "Arial"]
    saved = client.put("/api/kit", json={"font": "Impact", "highlight": "cyan", "uppercase": True}).json()
    assert (saved["font"], saved["highlight"], saved["uppercase"]) == ("Impact", "cyan", True)
    added = client.put("/api/kit/logo?name=logo.png", content=png().read()).json()
    assert added["logo"]["name"] == "logo.png"
    assert client.put("/api/kit/banner?name=x.png", content=png().read()).status_code == 422
    assert client.delete("/api/kit/logo").json()["logo"] is None


def test_a_cover_title_is_in_capitals_without_hashtags(tmp_path, monkeypatch):
    from clippy import covers

    seen = []
    monkeypatch.setattr(covers, "_wrap", lambda draw, text, font, width: seen.append(text) or [text])
    covers.draw_title(Image.new("RGB", (540, 960)), "xQc steals his first car #shorts", None)
    assert seen[0] == "XQC STEALS HIS FIRST CAR"


def test_the_cover_frame_is_taken_where_nobody_speaks():
    from clippy.render import quiet_time

    words = [{"start": 0.1, "end": 1.0}, {"start": 1.1, "end": 2.0}, {"start": 3.0, "end": 4.0}]
    assert quiet_time(words, 20.0) == 2.5
    assert quiet_time(words[:2], 20.0) == round(min(1.0, 20.0 / 3), 2)
    assert quiet_time([], 20.0) == 1.0


def test_a_stream_cover_shows_the_game_big_with_the_streamer_on_it():
    from clippy import covers
    from clippy.compositing import OUT_H, OUT_W, StreamCompositor

    frame = Image.new("RGB", (OUT_W, OUT_H), (200, 40, 40))
    frame.paste((40, 200, 40), (0, StreamCompositor.TOP_H + 3, OUT_W, OUT_H))
    frame.paste((0, 0, 0), (0, StreamCompositor.TOP_H - 3, OUT_W, StreamCompositor.TOP_H + 3))
    assert covers.stream_split(frame) == StreamCompositor.TOP_H
    assert covers.stream_split(Image.new("RGB", (OUT_W, OUT_H), (90, 90, 90))) is None
    cover = covers.stream_cover(frame, StreamCompositor.TOP_H)
    assert cover.size == frame.size
    assert cover.getpixel((40, 60))[1] > 150 and cover.getpixel((40, OUT_H - 60))[1] > 150
    assert cover.getpixel((OUT_W // 2, round(OUT_H * covers.CARD_CENTER)))[0] > 150
