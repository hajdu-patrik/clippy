import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clippy.highlights import Clip
from clippy.publish import Tokens
from clippy.publish.accounts import AccountStore
from clippy.publish.stats import (INSTAGRAM_GRAPH, YOUTUBE_ANALYTICS, YOUTUBE_API, hold_at, instagram_media,
                                  post_of_link, youtube_retention, youtube_stats)
from clippy.storage import Storage
from clippy.tools import ClippyError
from clippy.web import results
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import Runner
from clippy.web.media import MediaStore
from clippy.web.server import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "platforms"
DAY = 86400.0
NOW = 1_800_000_000.0
TOKENS = Tokens(access_token="tok", expires_at=9e12)
VIDEO = "AbCdEfGhIj0"
PUBLISHED = datetime.fromisoformat("2026-10-01T17:30:00+00:00").timestamp()
REEL_2 = datetime.fromisoformat("2026-11-18T18:00:00+00:00").timestamp()
REEL_OLD = datetime.fromisoformat("2026-09-02T09:15:00+00:00").timestamp()


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.headers = status, data, {}
        self.content = json.dumps(data).encode() if data is not None else b""

    def json(self):
        return self._data


class API:
    """Answers like the platforms: the Analytics report by what it is asked for, everything else by its URL."""

    def __init__(self, routes, retention=None):
        self.routes, self.retention, self.calls = routes, retention, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params or {}))
        if url == YOUTUBE_ANALYTICS and (params or {}).get("dimensions") == "elapsedVideoTimeRatio":
            return self.retention or Resp(200, {"columnHeaders": [], "rows": []})
        return self.routes[url]


def youtube_api(retention=None):
    return API({f"{YOUTUBE_API}/videos": Resp(200, recorded("youtube_videos")),
                YOUTUBE_ANALYTICS: Resp(200, recorded("youtube_by_video"))},
               Resp(200, recorded("youtube_retention")) if retention is None else retention)


@pytest.mark.parametrize("url, expected", [
    ("https://www.youtube.com/shorts/AbCdEfGhIj0?feature=share", ("youtube", VIDEO)),
    ("https://youtube.com/watch?v=AbCdEfGhIj0&t=3", ("youtube", VIDEO)),
    ("https://youtu.be/AbCdEfGhIj0", ("youtube", VIDEO)),
    ("https://m.youtube.com/shorts/AbCdEfGhIj0", ("youtube", VIDEO)),
    ("https://www.instagram.com/reel/DAAAAAAAAA1/?igsh=xyz", ("instagram", "")),
    ("https://instagram.com/someone/reel/DAAAAAAAAA1", ("instagram", "")),
    ("https://www.instagram.com/p/DAAAAAAAAA1/", ("instagram", "")),
])
def test_a_post_link_tells_its_platform_and_id(url, expected):
    platform, remote_id, clean = post_of_link(url)
    assert (platform, remote_id) == expected
    assert clean in (f"https://www.youtube.com/shorts/{VIDEO}", "https://www.instagram.com/reel/DAAAAAAAAA1/")


@pytest.mark.parametrize("url", ["https://www.tiktok.com/@a/video/1", "https://www.youtube.com/@channel",
                                 "https://youtube.com.evil.example/shorts/AbCdEfGhIj0", "ftp://youtu.be/AbCdEfGhIj0",
                                 "https://www.instagram.com/someone/", "not a link"])
def test_other_links_are_refused(url):
    with pytest.raises(ClippyError) as err:
        post_of_link(url)
    assert err.value.key == "error.post_link"


def test_the_retention_curve_and_the_hold_at_three_seconds():
    http = youtube_api()
    curve = youtube_retention(http, TOKENS, VIDEO)
    assert len(curve) == 100 and curve[0] == [0.01, 0.919] and curve[-1] == [1.0, 0.54]
    params = http.calls[0][1]
    assert (params["filters"], params["metrics"], params["ids"]) == (f"video=={VIDEO}", "audienceWatchRatio",
                                                                    "channel==MINE")
    assert hold_at(curve, 3.0, 30.0) == 0.485
    assert 0.414 <= hold_at(curve, 3.0, 20.0) < hold_at(curve, 3.0, 30.0)
    assert hold_at(curve, 0.1, 30.0) == 0.919 and hold_at(curve, 3.0, 2.0) == 0.54
    assert hold_at([], 3.0, 30.0) is None and hold_at(curve, 3.0, 0) is None


def test_youtube_stats_bring_the_curve_and_the_time_it_went_up():
    http = youtube_api()
    found = youtube_stats(http, TOKENS, [VIDEO], {VIDEO: 30.0})[VIDEO]
    assert (found.views, found.likes, found.comments, found.shares, found.watched) == (15230, 611, 42, 19, 0.844)
    assert found.hold == 0.485 and len(found.curve) == 100
    assert found.published == PUBLISHED and "published" not in found.to_json()
    assert http.calls[0][1]["part"] == "statistics,snippet"
    assert youtube_stats(youtube_api(), TOKENS, [VIDEO])[VIDEO].hold is None
    turned_down = youtube_api(Resp(403, {"error": {"code": 403, "message": "Forbidden"}}))
    assert youtube_stats(turned_down, TOKENS, [VIDEO], {VIDEO: 30.0})[VIDEO].to_json()["watched"] == 0.844
    no_scope = youtube_api()
    no_scope.routes[YOUTUBE_ANALYTICS] = Resp(403, {"error": {"code": 403, "message": "insufficient scope"}})
    assert youtube_stats(no_scope, TOKENS, [VIDEO], {VIDEO: 30.0})[VIDEO].hold is None
    assert len(no_scope.calls) == 2


def test_instagram_posts_are_found_by_their_shortcode_page_by_page():
    first = f"{INSTAGRAM_GRAPH}/me/media"
    http = API({first: Resp(200, recorded("instagram_media_1")),
                f"{INSTAGRAM_GRAPH}/me/media?after=QVFI2": Resp(200, recorded("instagram_media_2"))})
    found = instagram_media(http, TOKENS, {"DAAAAAAAAA2", "C_reel_old1"})
    assert found == {"DAAAAAAAAA2": ("17900000000000002", REEL_2),
                     "C_reel_old1": ("17900000000000003", REEL_OLD)}
    assert len(http.calls) == 2 and http.calls[1][1] == {}
    one = API({first: Resp(200, recorded("instagram_media_1"))})
    assert list(instagram_media(one, TOKENS, {"DAAAAAAAAA1"})) == ["DAAAAAAAAA1"] and len(one.calls) == 1
    elsewhere = recorded("instagram_media_1") | {"paging": {"next": "https://evil.example/me/media"}}
    away = API({first: Resp(200, elsewhere)})
    assert instagram_media(away, TOKENS, {"nothere0000"}) == {} and len(away.calls) == 1


@pytest.fixture
def app(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    client = TestClient(create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False),
                        headers={"X-Clippy-Request": "1"})
    return client, db


def a_clip(db, number=1, hook_type="question", length=30.0, chat=None):
    chat = chat or db.create_chat("c")
    clip = Clip(0.0, length, 80, title=f"T{number}", hook_type=hook_type).to_dict()
    return db.add_clip(chat["id"], number, 1, clip, {}, {"duration_out": length}, "x.mp4", "x.jpg", "x.txt")


def test_a_post_made_by_hand_is_linked_to_its_clip_and_can_be_unlinked(app):
    client, db = app
    row = a_clip(db)
    chat = row["chat_id"]
    other = a_clip(db, 2, chat=db.get_chat(chat))
    answer = client.post(f"/api/chats/{chat}/clips/1/posts", json={"url": "https://youtu.be/AbCdEfGhIj0"})
    assert answer.status_code == 200
    post = answer.json()["posts"][0]
    assert (post["platform"], post["state"], post["url"]) == ("youtube", "linked", f"https://www.youtube.com/shorts/{VIDEO}")
    assert db._one("SELECT remote_id FROM posts WHERE id = ?", (post["id"],))["remote_id"] == VIDEO
    again = client.post(f"/api/chats/{chat}/clips/{other['number']}/posts",
                        json={"url": f"https://www.youtube.com/watch?v={VIDEO}"})
    assert again.status_code == 400 and again.json()["detail"]["key"] == "error.post_linked"
    assert again.json()["detail"]["message"] == "That post is already on clip #1."
    bad = client.post(f"/api/chats/{chat}/clips/1/posts", json={"url": "https://www.tiktok.com/@a/video/1"})
    assert bad.status_code == 400 and bad.json()["detail"]["key"] == "error.post_link"
    assert client.post(f"/api/chats/{chat}/clips/9/posts", json={"url": "https://youtu.be/AbCdEfGhIj0"}).status_code == 404
    reel = client.post(f"/api/chats/{chat}/clips/1/posts", json={"url": "https://www.instagram.com/reel/DAAAAAAAAA1/"})
    assert {p["platform"] for p in reel.json()["posts"]} == {"youtube", "instagram"}
    uploaded = db.add_post(row, "youtube", "public", "https://youtube.com/shorts/v1", "v1")
    assert client.delete(f"/api/chats/{chat}/clips/1/posts/{uploaded['id']}").json() == {"ok": False}
    assert client.delete(f"/api/chats/{chat}/clips/1/posts/{post['id']}").json() == {"ok": True}
    assert client.post(f"/api/chats/{chat}/clips/2/posts",
                       json={"url": "https://youtu.be/AbCdEfGhIj0"}).status_code == 200


class Platform:
    oauth = True

    def configured(self):
        return True

    def needs_refresh(self, tokens, now):
        return False


def test_linked_posts_are_read_back_even_when_old(tmp_path):
    db = DB(tmp_path / "t.db")
    storage = Storage(tmp_path / "data", tmp_path / "library")
    row = a_clip(db)
    youtube = db.add_post(row, "youtube", "linked", f"https://www.youtube.com/shorts/{VIDEO}", VIDEO)
    reel = db.add_post(row, "instagram", "linked", "https://www.instagram.com/reel/C_reel_old1/")
    lost = db.add_post(row, "instagram", "linked", "https://www.instagram.com/reel/Cnot_found0/")
    db._exec("UPDATE posts SET created = ?", (NOW - 60 * DAY,))
    http = youtube_api()
    http.routes |= {f"{INSTAGRAM_GRAPH}/me/media": Resp(200, recorded("instagram_media_1")),
                    f"{INSTAGRAM_GRAPH}/me/media?after=QVFI2": Resp(200, recorded("instagram_media_2")),
                    f"{INSTAGRAM_GRAPH}/17900000000000003/insights": Resp(200, {"data": [
                        {"name": "views", "values": [{"value": 800}]},
                        {"name": "ig_reels_avg_watch_time", "values": [{"value": 9000}]}]})}
    store = AccountStore(storage, {"youtube": Platform(), "instagram": Platform()}, http=http, clock=lambda: NOW)
    store.save("youtube", Tokens(access_token="a", expires_at=NOW + 3600))
    store.save("instagram", Tokens(access_token="b", expires_at=NOW + 3600))
    assert results.refresh(db, store, NOW) == 2
    rows = {p["id"]: p for p in db._all("SELECT * FROM posts")}
    assert rows[youtube["id"]]["stats"]["hold"] == 0.485 and rows[youtube["id"]]["created"] == PUBLISHED
    assert rows[reel["id"]]["remote_id"] == "17900000000000003" and rows[reel["id"]]["created"] == REEL_OLD
    assert rows[reel["id"]]["stats"] == {"views": 800, "likes": 0, "comments": 0, "shares": 0, "watched": 0.3}
    assert rows[lost["id"]]["remote_id"] == "" and rows[lost["id"]]["stats"] == {}
    assert results.refresh(db, store, NOW + 7 * 3600) == 0


def held(db, number, hook_type, hold, views=1000, age=5 * DAY):
    row = a_clip(db, number, hook_type)
    post = db.add_post(row, "youtube", "linked", f"https://www.youtube.com/shorts/x{number:010d}", f"x{number:010d}")
    db._exec("UPDATE posts SET created = ? WHERE id = ?", (NOW - age, post["id"]))
    db.set_post_stats(post["id"], {"views": views, **({"hold": hold} if hold is not None else {})}, NOW)


def test_what_works_compares_openings_by_how_many_stay_past_three_seconds(tmp_path):
    db = DB(tmp_path / "t.db")
    for i, (hook, hold) in enumerate([("question", 0.82), ("question", 0.78), ("question", 0.75),
                                      ("statement", 0.52), ("statement", 0.48),
                                      ("cold_open", 0.65), ("cold_open", 0.65), ("cold_open", 0.65)]):
        held(db, i + 1, hook, hold)
    found = results.insights(db, NOW)
    assert found["held"] == 8
    by_hook = {f["value"]: f for f in found["holds"] if f["dimension"] == "hook"}
    assert set(by_hook) == {"question", "statement"}
    assert by_hook["question"] == {"dimension": "hook", "value": "question", "hold": 0.78, "usual": 0.65, "count": 3}
    assert by_hook["statement"]["hold"] == 0.5
    text = results.lessons(found)
    assert "- hook type 'question': 78 of 100 viewers still watching after the first 3 s, against the usual 65 (3 clips)" in text
    assert "hook type 'cold_open'" not in text


def test_a_single_post_or_a_mixed_group_teaches_nothing(tmp_path):
    db = DB(tmp_path / "t.db")
    for i, (hook, hold) in enumerate([("question", 0.95), ("contrarian", 0.98), ("contrarian", 0.58),
                                      ("contrarian", 0.60), ("statement", 0.62), ("statement", 0.61),
                                      ("statement", 0.63)]):
        held(db, i + 1, hook, hold)
    found = results.insights(db, NOW)
    assert not [f for f in found["holds"] if f["value"] in ("question", "contrarian")]
    few = DB(tmp_path / "few.db")
    for i in range(4):
        held(few, i + 1, "question" if i < 2 else "statement", 0.9 if i < 2 else 0.4)
    held(few, 9, "question", None)
    assert results.insights(few, NOW)["held"] == 4 and results.insights(few, NOW)["holds"] == []
