import json

import pytest
from fastapi.testclient import TestClient

from clippy.highlights import Clip, find_highlights
from clippy.publish import Tokens
from clippy.publish.accounts import AccountStore
from clippy.publish.stats import INSTAGRAM_GRAPH, YOUTUBE_ANALYTICS, YOUTUBE_API, instagram_stats, youtube_stats
from clippy.storage import Storage
from clippy.web import results
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import Runner
from clippy.web.media import MediaStore
from clippy.web.server import create_app

DAY = 86400.0
NOW = 1_800_000_000.0


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.headers = status, data, {}
        self.content = json.dumps(data).encode() if data is not None else b""

    def json(self):
        return self._data


class HTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.routes[url]


TOKENS = Tokens(access_token="tok", expires_at=9e12)


def test_youtube_counts_and_the_share_watched():
    http = HTTP({
        f"{YOUTUBE_API}/videos": Resp(200, {"items": [
            {"id": "a", "statistics": {"viewCount": "1200", "likeCount": "40", "commentCount": "3"}},
            {"id": "b", "statistics": {"viewCount": "90"}}]}),
        YOUTUBE_ANALYTICS: Resp(200, {"columnHeaders": [{"name": "video"}, {"name": "averageViewPercentage"},
                                                        {"name": "shares"}],
                                      "rows": [["a", 71.5, 4]]}),
    })
    found = youtube_stats(http, TOKENS, ["a", "b"])
    assert found["a"].to_json() == {"views": 1200, "likes": 40, "comments": 3, "shares": 4, "watched": 0.715}
    assert found["b"].to_json() == {"views": 90, "likes": 0, "comments": 0, "shares": 0}
    assert http.calls[1][1]["params"]["filters"] == "video==a,b"
    http.routes[YOUTUBE_ANALYTICS] = Resp(403, {"error": {"code": 403, "message": "insufficient scope"}})
    assert youtube_stats(http, TOKENS, ["a"])["a"].watched is None


def test_instagram_reel_insights():
    http = HTTP({f"{INSTAGRAM_GRAPH}/m1/insights": Resp(200, {"data": [
        {"name": "views", "values": [{"value": 5400}]}, {"name": "likes", "values": [{"value": 210}]},
        {"name": "shares", "values": [{"value": 12}]}, {"name": "ig_reels_avg_watch_time", "values": [{"value": 13500}]}]})})
    stats = instagram_stats(http, TOKENS, "m1", 30.0)
    assert stats.to_json() == {"views": 5400, "likes": 210, "comments": 0, "shares": 12, "watched": 0.45}


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def posted(db, tmp_path, number, hook_type, views, platform="youtube", age=5 * DAY, length=30.0, watched=None):
    chat = db.create_chat("c")
    clip = Clip(0.0, length, 80, title=f"T{number}", hook_type=hook_type).to_dict()
    row = db.add_clip(chat["id"], number, 1, clip, {}, {"duration_out": length}, "x.mp4", "x.jpg", "x.txt")
    post = db.add_post(row, platform, "public", "https://example/v", f"id{number}")
    db._exec("UPDATE posts SET created = ? WHERE id = ?", (NOW - age, post["id"]))
    if views is not None:
        db.set_post_stats(post["id"], {"views": views, **({"watched": watched} if watched is not None else {})}, NOW)
    return post


def test_patterns_need_enough_ripe_clips(db, tmp_path):
    for i, (hook, views) in enumerate([("question", 4000), ("question", 3000), ("statement", 400), ("statement", 300)]):
        posted(db, tmp_path, i + 1, hook, views)
    posted(db, tmp_path, 9, "question", 99999, age=DAY)
    found = results.insights(db, NOW)
    assert (found["measured"], found["compared"], found["findings"]) == (5, 4, [])
    assert results.lessons(found) == ""


def test_what_works_is_found_and_told_to_the_picker(db, tmp_path):
    rows = [("question", 4000, 0.8), ("question", 5000, 0.75), ("statement", 300, 0.3), ("statement", 250, 0.35),
            ("cold_open", 1000, None), ("cold_open", 1100, None)]
    for i, (hook, views, watched) in enumerate(rows):
        posted(db, tmp_path, i + 1, hook, views, watched=watched)
    found = results.insights(db, NOW)
    hooks = {f["value"]: f for f in found["findings"] if f["dimension"] == "hook"}
    assert hooks["question"]["factor"] > 2 and hooks["question"]["watched"] == 0.78
    assert hooks["statement"]["factor"] < 0.5 and "cold_open" not in hooks
    assert not [f for f in found["findings"] if f["dimension"] == "length"]
    text = results.lessons(found)
    assert "- hook type 'question':" in text and "78% watched (2 clips)" in text
    assert "posts in the" not in results.lessons(found, ("hook",))


def test_stats_are_read_back_for_recent_posts(tmp_path, db):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    old = posted(db, tmp_path, 1, "question", None, age=40 * DAY)
    fresh = posted(db, tmp_path, 2, "question", None, age=DAY)
    insta = posted(db, tmp_path, 3, "question", None, platform="instagram", age=DAY)
    http = HTTP({f"{YOUTUBE_API}/videos": Resp(200, {"items": [{"id": "id2", "statistics": {"viewCount": "77"}}]}),
                 YOUTUBE_ANALYTICS: Resp(403, {"error": {"code": 403, "message": "no"}})})

    class Platform:
        oauth = True

        def configured(self):
            return True

        def needs_refresh(self, tokens, now):
            return False

    store = AccountStore(storage, {"youtube": Platform(), "instagram": Platform()}, http=http, clock=lambda: NOW)
    store.save("youtube", Tokens(access_token="a", expires_at=NOW + 3600))
    assert results.refresh(db, store, NOW) == 1
    stats = {p["id"]: p["stats"] for p in db._all("SELECT id, stats FROM posts")}
    assert stats[fresh["id"]] == {"views": 77, "likes": 0, "comments": 0, "shares": 0}
    assert stats[old["id"]] == {} and stats[insta["id"]] == {}
    assert results.refresh(db, store, NOW + 60) == 0
    presenter = ClipPresenter(MediaStore(storage), db)
    row = db.get_clip(fresh["clip_id"])
    assert presenter.present(row)["posts"][0]["stats"]["views"] == 77


def test_the_picker_hears_the_lessons():
    seen = []

    class LLM:
        backend = "ollama"

        def describe(self):
            return "fake"

        def chat_json(self, system, prompt, **kwargs):
            seen.append(system)
            if prompt.startswith("TRANSCRIPT:"):
                return {"clips": [{"start": s, "end": s + 6.0, "score": 60} for s in (0.0, 20.0)]}
            return {}

    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(["Hello", "there."] * 40)]
    find_highlights({"segments": [{"words": words}], "language": "en"}, LLM(), max_clips=1, min_len=5, max_len=15,
                    lessons="- hook type 'question': 2.1x the usual views (4 clips)")
    window, rerank = seen[0], next(s for s in seen if s.startswith("You are the head editor"))
    assert "How this creator's posted clips did" in rerank and "2.1x" in rerank and "2.1x" not in window


def test_results_route(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    client = TestClient(create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False),
                        headers={"X-Clippy-Request": "1"})
    answer = client.get("/api/results").json()
    assert answer == {"measured": 0, "compared": 0, "findings": [], "analytics": True}
    runner.accounts.save("youtube", Tokens(access_token="a", expires_at=9e12, scope="youtube.upload"))
    assert client.get("/api/results").json()["analytics"] is False
    assert client.post("/api/results/refresh").json()["read"] == 0


def test_a_turned_down_cover_is_tried_again_less_often_until_it_goes_through(db, tmp_path):
    from clippy.web import cover_retry

    class Platform:
        def __init__(self):
            self.answers, self.calls = [False, False, True], []

        def set_cover(self, http, tokens, remote_id, image):
            self.calls.append((remote_id, image.name))
            return self.answers.pop(0)

    class Accounts:
        http = None

        def __init__(self):
            self.youtube = Platform()

        def tokens(self, name):
            return TOKENS

        def platform(self, name):
            return self.youtube

    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"jpg")
    chat = db.create_chat("c")
    row = db.add_clip(chat["id"], 1, 1, Clip(0.0, 20.0, 80).to_dict(), {}, {}, "c.mp4", str(cover), "c.txt")
    post = db.add_post(row, "youtube", "scheduled", "https://youtube.com/shorts/v1", "v1",
                       cover_due=cover_retry.first_try(NOW))
    db.add_post(row, "youtube", "public", "https://youtube.com/shorts/v2", "v2")
    accounts = Accounts()
    assert cover_retry.retry(db, accounts, NOW) == 0 and accounts.youtube.calls == []
    now = NOW + cover_retry.FIRST
    assert cover_retry.retry(db, accounts, now) == 0
    first = db._one("SELECT * FROM posts WHERE id = ?", (post["id"],))
    assert first["cover_tries"] == 1 and first["cover_due"] == now + 2 * cover_retry.FIRST
    assert cover_retry.retry(db, accounts, first["cover_due"]) == 0
    second = db._one("SELECT * FROM posts WHERE id = ?", (post["id"],))
    assert second["cover_due"] == first["cover_due"] + 4 * cover_retry.FIRST
    assert cover_retry.retry(db, accounts, second["cover_due"]) == 1
    assert accounts.youtube.calls == [("v1", "c.jpg")] * 3 and db.covers_due(NOW * 2) == []
    assert cover_retry.next_try(NOW, cover_retry.TRIES) == 0.0
    assert cover_retry.next_try(NOW, cover_retry.TRIES - 1) == NOW + cover_retry.LONGEST
