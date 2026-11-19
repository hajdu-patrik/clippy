import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from clippy import recovery
from clippy.highlights import Clip
from clippy.publish import PostResult, PostText, Tokens, Upload, checked, facebook, instagram, kick, tiktok, twitch, x, youtube
from clippy.publish.base import POLL_SECONDS, Platform
from clippy.publish.accounts import AccountStore
from clippy.publish.registry import ADAPTERS, LABELS, PLATFORMS, POSTING, default_platforms
from clippy.publish.oauth import OAuthFlows, hex_challenge, s256_challenge
from clippy.storage import Storage
from clippy.tools import ClippyError
from clippy.web import agent
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import Runner
from clippy.web.media import MediaStore
from clippy.web.server import create_app

CSRF = {"X-Clippy-Request": "1"}
HERE = ("127.0.0.1", 50000)


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.content = b"" if data is None else json.dumps(data).encode()

    def json(self):
        return self._data


class FakeHTTP:
    """Answers by exact (method, url); a list of answers is used in order, the last one repeats."""

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls = []

    def _call(self, method, url, **kw):
        body = kw.get("data")
        if hasattr(body, "read"):
            kw["sent"] = body.read()
        self.calls.append((method, url, kw))
        answers = self.routes[(method, url)]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")
    return Upload(path, 20.0)


POST = PostText("Big <reveal>", "The teaser\n\nWhat do you think?", ("#gta6", "#gaming"))
TOKENS = Tokens(access_token="tok", refresh_token="ref", expires_at=10_000.0, user_id="17841")


def test_oauth_state_is_single_use_and_expires():
    now = [1000.0]
    flows = OAuthFlows(clock=lambda: now[0])
    state, verifier = flows.start("youtube")
    assert flows.finish("tiktok", state) is None
    state, verifier = flows.start("youtube")
    assert flows.finish("youtube", state) == verifier
    assert flows.finish("youtube", state) is None
    state, _ = flows.start("youtube")
    now[0] += 601
    assert flows.finish("youtube", state) is None
    assert len(s256_challenge("abc")) == 43 and "=" not in s256_challenge("abc")
    assert hex_challenge("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.parametrize("response,key", [
    (Resp(401, {"error": {"message": "bad"}}), "error.account_expired"),
    (Resp(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}),
     "error.account_expired"),
    (Resp(400, {"error": {"code": 190, "message": "Error validating access token"}}), "error.account_expired"),
    (Resp(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts"}}), "error.platform_audit"),
    (Resp(403, {"error": {"code": 403, "message": "quotaExceeded"}}), "error.platform_rejected"),
    (Resp(200, {"error": {"code": "spam_risk_too_many_posts", "message": "too many"}}), "error.platform_rejected"),
])
def test_platform_errors_become_readable_keys(response, key):
    with pytest.raises(ClippyError) as caught:
        checked(response, "TikTok")
    assert caught.value.key == key
    assert key in recovery.FINAL


def test_a_successful_answer_passes_through():
    assert checked(Resp(200, {"data": {"x": 1}, "error": {"code": "ok"}}), "TikTok")["data"] == {"x": 1}


def test_youtube_uploads_in_chunks_with_the_clip_title(monkeypatch, video):
    monkeypatch.setattr(youtube, "CHUNK", 4)
    session = "https://upload.youtube/session"
    http = FakeHTTP({
        ("POST", youtube.UPLOAD): Resp(200, {}, {"Location": session}),
        ("PUT", session): [Resp(308, None, {"Range": "bytes=0-3"}), Resp(308, None, {"Range": "bytes=0-7"}),
                           Resp(200, {"id": "abc", "status": {"privacyStatus": "private"}})],
    })
    result = youtube.YouTube().publish(http, TOKENS, video, POST, {"privacy": "public"})
    assert result == PostResult("https://youtube.com/shorts/abc", "abc", "private_until_audit")
    start = http.calls[0][2]
    assert start["json"]["snippet"]["title"] == "Big reveal"
    assert start["json"]["snippet"]["tags"] == ["gta6", "gaming"]
    assert start["json"]["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    ranges = [kw["headers"]["Content-Range"] for m, url, kw in http.calls if m == "PUT"]
    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
    assert b"".join(kw["sent"] for m, url, kw in http.calls if m == "PUT") == b"0123456789"


def test_youtube_keeps_a_planned_clip_private_until_its_time(video):
    session = "https://upload.youtube/session"
    http = FakeHTTP({("POST", youtube.UPLOAD): Resp(200, {}, {"Location": session}),
                     ("PUT", session): Resp(200, {"id": "abc", "status": {"privacyStatus": "private"}})})
    at = time.time() + 3 * 3600
    result = youtube.YouTube().publish(http, TOKENS, video, POST, {"privacy": "public", "publish_at": at})
    status = http.calls[0][2]["json"]["status"]
    assert status["privacyStatus"] == "private" and status["publishAt"].endswith("Z")
    assert abs(datetime.fromisoformat(status["publishAt"].replace("Z", "+00:00")).timestamp() - at) < 1
    assert result.state == "scheduled"


def test_youtube_sets_the_clips_cover_as_thumbnail(video):
    video.path.with_suffix(".jpg").write_bytes(b"jpegbytes")
    session = "https://upload.youtube/session"
    http = FakeHTTP({("POST", youtube.UPLOAD): Resp(200, {}, {"Location": session}),
                     ("PUT", session): Resp(200, {"id": "abc", "status": {"privacyStatus": "public"}}),
                     ("POST", youtube.THUMBNAILS): Resp(200, {})})
    assert not youtube.YouTube().publish(http, TOKENS, video, POST, {"privacy": "public", "category": "20"}).cover_pending
    assert http.calls[0][2]["json"]["snippet"]["categoryId"] == "20"
    thumb = http.calls[-1]
    assert thumb[1] == youtube.THUMBNAILS and thumb[2]["params"]["videoId"] == "abc" and thumb[2]["data"] == b"jpegbytes"
    failing = FakeHTTP({("POST", youtube.UPLOAD): Resp(200, {}, {"Location": session}),
                        ("PUT", session): Resp(200, {"id": "abc", "status": {"privacyStatus": "public"}}),
                        ("POST", youtube.THUMBNAILS): Resp(403, {"error": {"message": "verify your channel"}})})
    refused = youtube.YouTube().publish(failing, TOKENS, video, POST, {})
    assert refused.state == "public" and refused.cover_pending
    video.path.with_suffix(".jpg").unlink()
    assert not youtube.YouTube().publish(failing, TOKENS, video, POST, {}).cover_pending


def test_youtube_resumes_where_google_says_after_a_broken_chunk(monkeypatch, video):
    monkeypatch.setattr(youtube, "CHUNK", 4)
    session = "https://upload.youtube/session"
    http = FakeHTTP({
        ("POST", youtube.UPLOAD): Resp(200, {}, {"Location": session}),
        ("PUT", session): [OSError("reset"), Resp(308, None, {"Range": "bytes=0-3"}),
                           Resp(308, None, {"Range": "bytes=0-7"}), Resp(201, {"id": "z"})],
    })
    assert youtube.YouTube().publish(http, TOKENS, video, POST, {}).url.endswith("/z")
    ranges = [kw["headers"]["Content-Range"] for m, url, kw in http.calls if m == "PUT"]
    assert ranges == ["bytes 0-3/10", "bytes */10", "bytes 4-7/10", "bytes 8-9/10"]


def test_youtube_refuses_a_clip_longer_than_a_short(tmp_path):
    path = tmp_path / "long.mp4"
    path.write_bytes(b"x")
    with pytest.raises(ClippyError) as caught:
        youtube.YouTube().publish(FakeHTTP({}), TOKENS, Upload(path, 200.0), POST, {})
    assert caught.value.key == "error.clip_too_long_for"


def test_instagram_container_upload_wait_and_publish(video):
    g = instagram.GRAPH
    http = FakeHTTP({
        ("POST", f"{g}/17841/media"): Resp(200, {"id": "C1"}),
        ("POST", f"{instagram.RUPLOAD}/C1"): Resp(200, {"success": True}),
        ("GET", f"{g}/C1"): [Resp(200, {"status_code": "IN_PROGRESS"}), Resp(200, {"status_code": "FINISHED"})],
        ("POST", f"{g}/17841/media_publish"): Resp(200, {"id": "M1"}),
        ("GET", f"{g}/M1"): Resp(200, {"permalink": "https://www.instagram.com/reel/abc/"}),
    })
    waits = []
    result = instagram.Instagram(sleep=waits.append).publish(http, TOKENS, video, POST, {})
    assert result == PostResult("https://www.instagram.com/reel/abc/", "M1", "public")
    container = http.calls[0][2]["data"]
    assert container["media_type"] == "REELS" and container["upload_type"] == "resumable"
    assert container["caption"] == POST.caption
    upload = http.calls[1][2]
    assert upload["headers"]["Authorization"] == "OAuth tok" and upload["headers"]["file_size"] == "10"
    assert upload["sent"] == b"0123456789"
    assert waits == [POLL_SECONDS]


def test_instagram_token_is_checked_and_refreshed_well_before_it_runs_out():
    http = FakeHTTP({("GET", f"{instagram.GRAPH}/me"): Resp(200, {"user_id": "17841", "username": "sixthstarrider"}),
                     ("GET", instagram.REFRESH): Resp(200, {"access_token": "new", "expires_in": 5_184_000})})
    platform = instagram.Instagram()
    tokens = platform.connect(http, "  IGAA" + "x" * 40 + " ", 0.0)
    assert (tokens.name, tokens.user_id, tokens.access_token) == ("sixthstarrider", "17841", "IGAA" + "x" * 40)
    assert not platform.needs_refresh(tokens, 0.0)
    assert platform.needs_refresh(tokens, 31 * 86400)
    assert platform.refresh(http, tokens, 100.0).access_token == "new"
    with pytest.raises(ClippyError) as caught:
        platform.connect(http, "short", 0.0)
    assert caught.value.key == "error.bad_token"


def test_tiktok_chunk_rule():
    assert tiktok.chunks(3_000_000) == (3_000_000, 1)
    assert tiktok.chunks(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    size = 95 * 1024 * 1024
    chunk, count = tiktok.chunks(size)
    assert chunk == tiktok.CHUNK and count == 9 and size - chunk * (count - 1) <= 128 * 1024 * 1024


def tiktok_http(status="SEND_TO_USER_INBOX", init_path="/post/publish/inbox/video/init/"):
    api = tiktok.API
    return FakeHTTP({
        ("POST", f"{api}{init_path}"): Resp(200, {"data": {"publish_id": "P1", "upload_url": "https://up.tiktok/x"},
                                                  "error": {"code": "ok"}}),
        ("POST", f"{api}/post/publish/creator_info/query/"): Resp(200, {"data": {
            "privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}}),
        ("PUT", "https://up.tiktok/x"): Resp(201),
        ("POST", f"{api}/post/publish/status/fetch/"): [
            Resp(200, {"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}}),
            Resp(200, {"data": {"status": status}, "error": {"code": "ok"}})],
    })


def test_tiktok_sends_to_drafts_by_default(video):
    http = tiktok_http()
    result = tiktok.TikTok(sleep=lambda s: None).publish(http, TOKENS, video, POST, {})
    assert result == PostResult("", "P1", "draft")
    init = http.calls[0][2]["json"]
    assert init == {"source_info": {"source": "FILE_UPLOAD", "video_size": 10, "chunk_size": 10,
                                    "total_chunk_count": 1}}
    put = next(kw for m, url, kw in http.calls if m == "PUT")
    assert put["headers"]["Content-Range"] == "bytes 0-9/10" and put["sent"] == b"0123456789"


def test_tiktok_private_post_is_self_only(video):
    http = tiktok_http("PUBLISH_COMPLETE", "/post/publish/video/init/")
    result = tiktok.TikTok(sleep=lambda s: None).publish(http, TOKENS, video, POST, {"mode": "private"})
    assert result.state == "private"
    init = next(kw for m, url, kw in http.calls if url.endswith("/post/publish/video/init/"))["json"]
    assert init["post_info"]["privacy_level"] == "SELF_ONLY" and init["post_info"]["title"] == POST.caption


def test_tiktok_sign_in_uses_hex_pkce_and_reads_the_name(monkeypatch):
    monkeypatch.setenv("CLIPPY_TIKTOK_CLIENT_KEY", "key")
    monkeypatch.setenv("CLIPPY_TIKTOK_CLIENT_SECRET", "secret")
    platform = tiktok.TikTok()
    url = platform.auth_url("http://127.0.0.1:7860/oauth/tiktok/callback", "st", "verifier")
    assert f"code_challenge={hex_challenge('verifier')}" in url and "client_key=key" in url
    http = FakeHTTP({
        ("POST", f"{tiktok.API}/oauth/token/"): Resp(200, {"access_token": "a", "refresh_token": "r",
                                                           "expires_in": 86400, "open_id": "o1"}),
        ("GET", f"{tiktok.API}/user/info/"): Resp(200, {"data": {"user": {"display_name": "Sixth Star"}},
                                                        "error": {"code": "ok"}}),
    })
    tokens = platform.exchange(http, "code", "verifier", "http://127.0.0.1:7860/oauth/tiktok/callback", 0.0)
    assert (tokens.name, tokens.user_id, tokens.expires_at) == ("Sixth Star", "o1", 86400.0)
    assert http.calls[0][2]["data"]["code_verifier"] == "verifier"


class FakePlatform(Platform):
    def __init__(self, name, outcome):
        self.name, self.label, self.outcome, self.posted = name, name.title(), outcome, 0

    def configured(self):
        return True

    def needs_refresh(self, tokens, now):
        return False

    def publish(self, http, tokens, upload, post, options):
        self.posted += 1
        outcome = self.outcome.pop(0) if isinstance(self.outcome, list) else self.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return PostResult(f"https://{self.name}.example/v", "id1", options.get("privacy", "public"))


def make_store(storage, outcomes, now=1000.0):
    platforms = {name: FakePlatform(name, outcome) for name, outcome in outcomes.items()}
    store = AccountStore(storage, platforms, http=object(), clock=lambda: now)
    for name in outcomes:
        store.save(name, Tokens(access_token=f"secret-{name}", name=f"{name} user", expires_at=now + 3600))
    return store, platforms


@pytest.fixture
def chat_env(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    chat = db.update_chat(db.create_chat()["id"], source="https://youtu.be/a", video_title="Video")
    folder = storage.chat_clips(chat["id"])
    mp4 = folder / "clip_01_v1.mp4"
    mp4.write_bytes(b"0123456789")
    (folder / "clip_01_v1.txt").write_text("Teaser 🔥\n\n#gta6\n\n---\nTitle: x\n", encoding="utf-8")
    clip = Clip(10.0, 30.0, 80, title="Title", description="Teaser 🔥", hashtags=["#gta6"]).to_dict()
    db.add_clip(chat["id"], 1, 1, clip, {}, {"duration_out": 20.0}, str(mp4), str(mp4.with_suffix(".jpg")),
                str(folder / "clip_01_v1.txt"))
    return storage, db, chat


def make_runner(storage, db, store):
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False,
                    accounts=store)
    runner.retry_pause = lambda attempt: 0
    return runner


def run_all(runner):
    while runner.pending:
        runner.run_one(runner.pending[0])


def test_a_timed_upload_keeps_the_time_the_platform_publishes_it(chat_env):
    storage, db, chat = chat_env
    store, _ = make_store(storage, {"youtube": None, "tiktok": None})
    runner = make_runner(storage, db, store)
    options = {"youtube": {"privacy": "scheduled", "publish_at": 5000.0}, "tiktok": {"publish_at": 7000.0}}
    runner.submit(chat["id"], "publish", {"clip": 1, "platforms": ["youtube", "tiktok"], "options": options})
    assert [j.kind for j in runner.jobs_of("publish")] == ["publish"] and runner.jobs_of("generate") == []
    run_all(runner)
    posts = {p["platform"]: p for p in db.latest_posts(chat["id"], 1)}
    assert posts["youtube"]["state"] == "scheduled" and posts["youtube"]["publish_at"] == 5000.0
    assert posts["tiktok"]["state"] == "public" and posts["tiktok"]["publish_at"] == 0.0


def test_one_platform_failing_does_not_stop_the_others(chat_env):
    storage, db, chat = chat_env
    store, platforms = make_store(storage, {
        "youtube": None, "instagram": ClippyError(key="error.platform_rejected", platform="Instagram",
                                                  detail="too many posts"), "tiktok": None})
    runner = make_runner(storage, db, store)
    runner.submit(chat["id"], "publish", {"clip": 1, "platforms": ["youtube", "instagram", "tiktok"],
                                          "options": {"youtube": {"privacy": "unlisted"}}})
    run_all(runner)
    posts = {p["platform"]: p for p in db.latest_posts(chat["id"], 1)}
    assert posts["youtube"]["state"] == "unlisted" and posts["youtube"]["url"] == "https://youtube.example/v"
    assert posts["instagram"]["state"] == "failed"
    assert posts["instagram"]["problem"]["key"] == "error.platform_rejected"
    assert posts["tiktok"]["state"] == "public"
    card = [m for m in db.list_messages(chat["id"]) if m["data"].get("job")][-1]
    assert card["data"]["summary_t"]["key"] == "done.publish_some"
    assert platforms["instagram"].posted == 1
    presented = {p["platform"]: p for p in runner.presenter.present(db.current_clips(chat["id"])[0])["posts"]}
    assert set(presented) == {"youtube", "instagram", "tiktok"}
    assert presented["instagram"]["error_t"]["key"] == "error.platform_rejected"
    assert "error_t" not in presented["youtube"] and presented["youtube"]["url"] == "https://youtube.example/v"


def test_a_network_failure_retries_without_posting_twice(chat_env):
    import requests

    storage, db, chat = chat_env
    store, platforms = make_store(storage, {"youtube": None, "tiktok": [requests.ConnectionError("down"), None]})
    runner = make_runner(storage, db, store)
    runner.submit(chat["id"], "publish", {"clip": 1, "platforms": ["youtube", "tiktok"]})
    run_all(runner)
    assert platforms["youtube"].posted == 1 and platforms["tiktok"].posted == 2
    states = [p["state"] for p in db._all("SELECT state FROM posts WHERE platform = 'tiktok' ORDER BY id")]
    assert states == ["failed", "public"]
    card = [m for m in db.list_messages(chat["id"]) if m["data"].get("job")][-1]
    assert card["data"]["job"]["payload"]["done"] == {"youtube": "public", "tiktok": "public"}
    assert card["data"]["summary_t"]["key"] == "done.publish"


def test_a_restarted_job_skips_what_is_already_posted(chat_env):
    storage, db, chat = chat_env
    store, platforms = make_store(storage, {"youtube": None, "tiktok": None})
    runner = make_runner(storage, db, store)
    runner.submit(chat["id"], "publish", {"clip": 1, "platforms": ["youtube", "tiktok"], "done": {"youtube": "public"}})
    run_all(runner)
    assert platforms["youtube"].posted == 0 and platforms["tiktok"].posted == 1


def test_no_connected_account_is_a_final_error(chat_env):
    storage, db, chat = chat_env
    store = AccountStore(storage, {"youtube": FakePlatform("youtube", None)}, http=object())
    runner = make_runner(storage, db, store)
    runner.submit(chat["id"], "publish", {"clip": 1, "platforms": ["youtube"]})
    run_all(runner)
    card = [m for m in db.list_messages(chat["id"]) if m["data"].get("job")][-1]
    assert card["kind"] == "error" and card["data"]["summary_t"]["key"] == "error.account_missing"


def test_tokens_refresh_before_they_run_out_and_never_show_in_the_status(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")

    class Refreshing(FakePlatform):
        def needs_refresh(self, tokens, now):
            return tokens.expires_at - now < 300

        def refresh(self, http, tokens, now):
            return Tokens(access_token="fresh", refresh_token=tokens.refresh_token, expires_at=now + 3600,
                          name=tokens.name)

    store = AccountStore(storage, {"youtube": Refreshing("youtube", None)}, http=object(), clock=lambda: 1000.0)
    store.save("youtube", Tokens(access_token="old", refresh_token="r", expires_at=1100.0, name="Channel"))
    assert store.tokens("youtube").access_token == "fresh"
    assert json.loads(storage.accounts_file.read_text(encoding="utf-8"))["youtube"]["access_token"] == "fresh"
    status = store.status()
    assert status["youtube"]["connected"] and status["youtube"]["name"] == "Channel"
    assert "fresh" not in json.dumps(status) and "token" not in json.dumps(status)
    assert store.remove("youtube") and not store.status()["youtube"]["connected"]


@pytest.fixture
def web(tmp_path, monkeypatch, chat_env):
    storage, db, chat = chat_env
    monkeypatch.setenv("CLIPPY_YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("CLIPPY_YOUTUBE_CLIENT_SECRET", "csecret")
    http = FakeHTTP({
        ("POST", youtube.TOKEN): Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3599}),
        ("GET", f"{youtube.API}/channels"): Resp(200, {"items": [{"id": "UC1", "snippet": {"title": "Sixth Star"}}]}),
    })
    store = AccountStore(storage, {"youtube": youtube.YouTube(), "instagram": instagram.Instagram(),
                                   "tiktok": tiktok.TikTok()}, http=http)
    runner = make_runner(storage, db, store)
    app = create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False)
    here = TestClient(app, headers=CSRF, client=HERE)
    phone = TestClient(app, headers=CSRF, client=("192.168.1.20", 50000))
    return here, phone, runner, chat


def test_accounts_connect_only_from_this_computer(web):
    here, phone, runner, chat = web
    assert phone.post("/api/accounts/youtube/connect").status_code == 403
    assert phone.delete("/api/accounts/youtube").status_code == 403
    assert phone.put("/api/accounts/instagram", json={"token": "x" * 30}).status_code == 403
    assert phone.get("/oauth/youtube/callback?code=c&state=s", follow_redirects=False).status_code == 403
    answer = here.get("/api/accounts").json()
    assert answer["here"] and not answer["accounts"]["youtube"]["connected"]
    assert answer["accounts"]["youtube"]["configured"] and not answer["accounts"]["tiktok"]["configured"]
    assert here.post("/api/accounts/tiktok/connect").json()["detail"]["key"] == "error.platform_not_configured"


def test_youtube_sign_in_round_trip(web):
    here, _, runner, _ = web
    url = here.post("/api/accounts/youtube/connect").json()["url"]
    assert url.startswith(youtube.AUTH) and "code_challenge_method=S256" in url and "access_type=offline" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A" in url
    state = url.split("state=")[1].split("&")[0]
    bad = here.get("/oauth/youtube/callback?code=c&state=wrong", follow_redirects=False)
    assert bad.status_code == 303 and bad.headers["location"] == "/#settings"
    assert here.get("/api/accounts").json()["notices"]["youtube"]["key"] == "error.oauth_state"
    ok = here.get(f"/oauth/youtube/callback?code=c&state={state}", follow_redirects=False)
    assert ok.status_code == 303
    answer = here.get("/api/accounts").json()
    assert answer["accounts"]["youtube"]["connected"] and answer["accounts"]["youtube"]["name"] == "Sixth Star"
    assert "youtube" not in answer["notices"] and "token" not in json.dumps(answer)
    assert runner.accounts.tokens("youtube").refresh_token == "rt"
    again = here.get(f"/oauth/youtube/callback?code=c&state={state}", follow_redirects=False)
    assert again.status_code == 303
    assert here.get("/api/accounts").json()["notices"]["youtube"]["key"] == "error.oauth_state"


def test_publish_route_needs_a_connected_account_and_the_header(web):
    here, phone, runner, chat = web
    body = {"platforms": ["youtube"], "youtube_privacy": "private"}
    answer = here.post(f"/api/chats/{chat['id']}/clips/1/publish", json=body)
    assert answer.status_code == 400 and answer.json()["detail"]["key"] == "error.account_missing"
    runner.accounts.save("youtube", Tokens(access_token="a", refresh_token="r", expires_at=9e12, name="Me"))
    plain = TestClient(here.app, client=HERE)
    assert plain.post(f"/api/chats/{chat['id']}/clips/1/publish", json=body).status_code == 403
    assert here.post(f"/api/chats/{chat['id']}/clips/9/publish", json=body).status_code == 404
    assert here.post(f"/api/chats/{chat['id']}/clips/1/publish", json={"platforms": ["myspace"]}).status_code == 422
    assert phone.post(f"/api/chats/{chat['id']}/clips/1/publish", json=body).status_code == 200
    job = runner.pending[-1]
    assert job.kind == "publish" and job.payload == {"clip": 1, "platforms": ["youtube"], "options": {
        "youtube": {"privacy": "private"}, "tiktok": {"mode": "draft"}}}


def test_the_chat_posts_only_when_the_user_asks():
    raw = [{"type": "publish", "clips": [2], "platforms": ["youtube", "instagram", "tiktok"]}]
    asked = agent.ActionValidator({1, 2}, {"source": "x"}, "posztold ki a 2. klipet youtube-ra privátként")
    assert asked.validate(raw) == [{"type": "publish", "clips": [2], "platforms": ["youtube"], "options": {
        "youtube": {"privacy": "private"}, "tiktok": {"mode": "private"}}}]
    every = agent.ActionValidator({1, 2}, {"source": "x"}, "upload clip 2")
    assert every.validate(raw)[0]["platforms"] == []
    injected = agent.ActionValidator({1, 2}, {"source": "x"}, "make clip 2 shorter")
    assert injected.validate(raw) == [] and injected.notes


def test_the_registry_lists_seven_platforms_and_only_five_post():
    assert PLATFORMS == ("youtube", "instagram", "facebook", "tiktok", "x", "twitch", "kick")
    assert POSTING == ("youtube", "instagram", "facebook", "tiktok", "x")
    assert LABELS["x"] == "X" and len({a.name for a in ADAPTERS}) == 7 and set(default_platforms()) == set(PLATFORMS)
    for adapter in ADAPTERS:
        assert (adapter.publish is not Platform.publish) == adapter.posting


def test_the_ui_knows_the_same_platforms():
    import re
    from pathlib import Path

    source = (Path(__file__).parent.parent / "clippy/web/static/js/platforms.js").read_text(encoding="utf-8")
    labels = dict(re.findall(r'(\w+): "([^"]+)"', source.split("export const POSTING")[0]))
    posting = re.findall(r'"(\w+)"', source.split("export const POSTING")[1].split("\n")[0])
    assert labels == LABELS and tuple(posting) == POSTING


@pytest.mark.parametrize("name", ["twitch", "kick"])
def test_platforms_without_an_upload_api_refuse_to_post(name, video):
    platform = default_platforms()[name]
    assert not platform.posting
    with pytest.raises(ClippyError) as caught:
        platform.publish(FakeHTTP({}), TOKENS, video, POST, {})
    assert caught.value.key == "error.posting_unsupported" and caught.value.key in recovery.FINAL


@pytest.mark.parametrize("response,key", [
    (Resp(429, {"title": "Too Many Requests", "detail": "slow down"}), "error.platform_rate_limited"),
    (Resp(401, {"title": "Unauthorized"}), "error.account_expired"),
])
def test_rate_limits_and_expired_tokens_have_their_own_messages(response, key):
    with pytest.raises(ClippyError) as caught:
        checked(response, "X")
    assert caught.value.key == key and key in recovery.FINAL


def test_a_plain_message_or_detail_is_the_reason():
    for body, reason in [({"message": "Invalid refresh token"}, "Invalid refresh token"),
                         ({"title": "Forbidden", "detail": "Video too long"}, "Video too long")]:
        with pytest.raises(ClippyError) as caught:
            checked(Resp(403, body), "X")
        assert caught.value.params["detail"] == reason


REDIRECT = "http://127.0.0.1:7860/oauth/{}/callback"


def query(url):
    from urllib.parse import parse_qs, urlsplit

    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_x_sign_in_is_pkce_with_the_posting_scopes_and_a_public_client(monkeypatch):
    monkeypatch.setenv("CLIPPY_X_CLIENT_ID", "xid")
    monkeypatch.delenv("CLIPPY_X_CLIENT_SECRET", raising=False)
    platform = x.X()
    url = platform.auth_url(REDIRECT.format("x"), "st", "verifier")
    assert url.startswith(x.AUTH) and query(url)["code_challenge"] == s256_challenge("verifier")
    assert query(url)["scope"] == "tweet.read tweet.write users.read media.write offline.access"
    http = FakeHTTP({
        ("POST", platform.token_url): Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 7200,
                                                 "scope": "tweet.read tweet.write"}),
        ("GET", f"{x.API}/users/me"): Resp(200, {"data": {"id": "9", "name": "Six", "username": "sixth"}}),
    })
    tokens = platform.exchange(http, "code", "verifier", REDIRECT.format("x"), 0.0)
    assert (tokens.name, tokens.user_id, tokens.expires_at) == ("@sixth", "9", 7200.0)
    call = http.calls[0][2]
    assert call["data"]["client_id"] == "xid" and call["data"]["code_verifier"] == "verifier" and not call["headers"]


def test_x_with_a_secret_signs_in_with_basic_auth_and_rotates_the_refresh_token(monkeypatch):
    monkeypatch.setenv("CLIPPY_X_CLIENT_ID", "xid")
    monkeypatch.setenv("CLIPPY_X_CLIENT_SECRET", "xsecret")
    platform = x.X()
    http = FakeHTTP({("POST", platform.token_url): Resp(200, {"access_token": "new", "refresh_token": "r2",
                                                              "expires_in": 7200})})
    fresh = platform.refresh(http, Tokens(access_token="old", refresh_token="r1", expires_at=100.0), 1000.0)
    assert (fresh.access_token, fresh.refresh_token, fresh.expires_at) == ("new", "r2", 8200.0)
    call = http.calls[0][2]
    assert call["headers"]["Authorization"].startswith("Basic ") and "client_id" not in call["data"]
    assert call["data"]["grant_type"] == "refresh_token"
    assert platform.needs_refresh(Tokens(access_token="a", expires_at=1100.0), 1000.0)


def test_a_refused_refresh_means_the_sign_in_has_run_out(monkeypatch):
    monkeypatch.setenv("CLIPPY_TWITCH_CLIENT_ID", "id")
    monkeypatch.setenv("CLIPPY_TWITCH_CLIENT_SECRET", "secret")
    platform = twitch.Twitch()
    http = FakeHTTP({("POST", twitch.TOKEN): Resp(400, {"status": 400, "message": "Invalid refresh token"})})
    with pytest.raises(ClippyError) as caught:
        platform.refresh(http, Tokens(access_token="a", refresh_token="r"), 0.0)
    assert caught.value.key == "error.account_expired"
    with pytest.raises(ClippyError) as none:
        platform.refresh(http, Tokens(access_token="a"), 0.0)
    assert none.value.key == "error.account_expired"


def test_x_posts_a_video_in_chunks_waits_for_processing_and_fits_the_text(monkeypatch, video):
    monkeypatch.setattr(x, "CHUNK", 4)
    api = x.API
    http = FakeHTTP({
        ("POST", f"{api}/media/upload/initialize"): Resp(200, {"data": {"id": "m1"}}),
        ("POST", f"{api}/media/upload/m1/append"): Resp(204),
        ("POST", f"{api}/media/upload/m1/finalize"): Resp(200, {"data": {"id": "m1", "processing_info": {
            "state": "pending", "check_after_secs": 2}}}),
        ("GET", f"{api}/media/upload"): [Resp(200, {"data": {"processing_info": {"state": "in_progress",
                                                                                  "check_after_secs": 1}}}),
                                          Resp(200, {"data": {"processing_info": {"state": "succeeded"}}})],
        ("POST", f"{api}/tweets"): Resp(201, {"data": {"id": "T9", "text": "..."}}),
    })
    waits = []
    platform = x.X(sleep=waits.append)
    long_post = PostText("t", "A" * 200 + "\n\n" + "B" * 200)
    result = platform.publish(http, TOKENS, video, long_post, {})
    assert result == PostResult("https://x.com/i/status/T9", "T9", "public")
    init = http.calls[0][2]["json"]
    assert init == {"media_type": "video/mp4", "total_bytes": 10, "media_category": "tweet_video"}
    appends = [kw for m, url, kw in http.calls if url.endswith("/append")]
    assert [a["data"]["segment_index"] for a in appends] == ["0", "1", "2"]
    assert b"".join(a["files"]["media"][1] for a in appends) == b"0123456789"
    assert waits == [2.0, 1.0]
    tweet = http.calls[-1][2]["json"]
    assert tweet["media"] == {"media_ids": ["m1"]} and tweet["text"] == "A" * 200


def test_x_stops_when_the_video_fails_processing(video):
    api = x.API
    http = FakeHTTP({
        ("POST", f"{api}/media/upload/initialize"): Resp(200, {"data": {"id": "m1"}}),
        ("POST", f"{api}/media/upload/m1/append"): Resp(204),
        ("POST", f"{api}/media/upload/m1/finalize"): Resp(200, {"data": {"processing_info": {
            "state": "failed", "error": {"message": "InvalidMedia"}}}}),
    })
    with pytest.raises(ClippyError) as caught:
        x.X(sleep=lambda s: None).publish(http, TOKENS, video, POST, {})
    assert caught.value.key == "error.platform_rejected" and caught.value.params["detail"] == "InvalidMedia"


def test_x_text_is_cut_at_a_word_when_one_paragraph_is_too_long():
    assert x.fit("short\n\nsecond\n\n#tag") == "short\n\nsecond\n\n#tag"
    cut = x.fit("word " * 100)
    assert len(cut) <= 280 and cut.endswith("…") and not cut.endswith(" …")


def test_facebook_sign_in_uses_the_scopes_or_a_login_for_business_configuration(monkeypatch):
    monkeypatch.setenv("CLIPPY_FACEBOOK_APP_ID", "appid")
    monkeypatch.setenv("CLIPPY_FACEBOOK_APP_SECRET", "appsecret")
    monkeypatch.delenv("CLIPPY_FACEBOOK_CONFIG_ID", raising=False)
    platform = facebook.Facebook()
    plain = query(platform.auth_url(REDIRECT.format("facebook"), "st", "v"))
    assert plain["scope"] == facebook.SCOPES and plain["state"] == "st" and "code_challenge" not in plain
    monkeypatch.setenv("CLIPPY_FACEBOOK_CONFIG_ID", "cfg1")
    business = query(platform.auth_url(REDIRECT.format("facebook"), "st", "v"))
    assert business["config_id"] == "cfg1" and "scope" not in business


def facebook_http(pages):
    return FakeHTTP({
        ("GET", facebook.Facebook.token_url): [Resp(200, {"access_token": "short"}), Resp(200, {"access_token": "long"})],
        ("GET", f"{facebook.GRAPH}/me/accounts"): Resp(200, {"data": pages}),
    })


def test_facebook_keeps_the_page_token_not_the_user_token(monkeypatch):
    monkeypatch.setenv("CLIPPY_FACEBOOK_APP_ID", "appid")
    monkeypatch.setenv("CLIPPY_FACEBOOK_APP_SECRET", "appsecret")
    monkeypatch.delenv("CLIPPY_FACEBOOK_PAGE_ID", raising=False)
    pages = [{"id": "p1", "name": "First", "access_token": "page-tok-1"},
             {"id": "p2", "name": "Second", "access_token": "page-tok-2"}]
    http = facebook_http(pages)
    tokens = facebook.Facebook().exchange(http, "code", "v", REDIRECT.format("facebook"), 0.0)
    assert (tokens.access_token, tokens.user_id, tokens.name, tokens.expires_at) == ("page-tok-1", "p1", "First", 0.0)
    first, second = http.calls[0][2]["params"], http.calls[1][2]["params"]
    assert first["code"] == "code" and first["client_secret"] == "appsecret"
    assert second["grant_type"] == "fb_exchange_token" and second["fb_exchange_token"] == "short"
    assert http.calls[2][2]["params"]["access_token"] == "long"
    monkeypatch.setenv("CLIPPY_FACEBOOK_PAGE_ID", "p2")
    chosen = facebook.Facebook().exchange(facebook_http(pages), "c", "v", "r", 0.0)
    assert chosen.user_id == "p2" and chosen.access_token == "page-tok-2"


def test_facebook_without_a_page_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("CLIPPY_FACEBOOK_APP_ID", "appid")
    monkeypatch.setenv("CLIPPY_FACEBOOK_APP_SECRET", "appsecret")
    monkeypatch.delenv("CLIPPY_FACEBOOK_PAGE_ID", raising=False)
    with pytest.raises(ClippyError) as caught:
        facebook.Facebook().exchange(facebook_http([]), "c", "v", "r", 0.0)
    assert caught.value.key == "error.no_page" and caught.value.key in recovery.FINAL


def test_facebook_page_token_never_asks_for_a_refresh():
    platform = facebook.Facebook()
    assert not platform.needs_refresh(Tokens(access_token="p", expires_at=0.0), 9e9) and platform.renews


def test_facebook_posts_a_reel_in_three_steps(video):
    g = facebook.GRAPH
    up = "https://rupload.facebook.com/video-upload/v27.0/V1"
    http = FakeHTTP({
        ("POST", f"{g}/P1/video_reels"): [Resp(200, {"video_id": "V1", "upload_url": up}), Resp(200, {"success": True})],
        ("POST", up): Resp(200, {"success": True}),
        ("GET", f"{g}/V1"): [Resp(200, {"status": {"video_status": "processing"}}),
                             Resp(200, {"status": {"video_status": "ready"}})],
    })
    waits = []
    tokens = Tokens(access_token="page-tok", user_id="P1")
    result = facebook.Facebook(sleep=waits.append).publish(http, tokens, video, POST, {})
    assert result == PostResult("https://www.facebook.com/reel/V1", "V1", "public")
    start, upload, finish = http.calls[0][2], http.calls[1][2], http.calls[2][2]
    assert start["data"] == {"upload_phase": "start"} and start["params"] == {"access_token": "page-tok"}
    assert upload["headers"] == {"Authorization": "OAuth page-tok", "offset": "0", "file_size": "10"}
    assert upload["sent"] == b"0123456789"
    assert finish["data"] == {"upload_phase": "finish", "video_id": "V1", "video_state": "PUBLISHED",
                              "description": POST.caption}
    assert waits == [POLL_SECONDS]


def test_facebook_reports_a_failed_reel_and_refuses_a_long_clip(tmp_path, video):
    g = facebook.GRAPH
    http = FakeHTTP({
        ("POST", f"{g}/P1/video_reels"): [Resp(200, {"video_id": "V1"}), Resp(200, {"success": True})],
        ("POST", f"{facebook.RUPLOAD}/V1"): Resp(200, {"success": True}),
        ("GET", f"{g}/V1"): Resp(200, {"status": {"video_status": "error", "processing_phase": {
            "error": {"message": "Bad codec"}}}}),
    })
    tokens = Tokens(access_token="page-tok", user_id="P1")
    with pytest.raises(ClippyError) as caught:
        facebook.Facebook(sleep=lambda s: None).publish(http, tokens, video, POST, {})
    assert caught.value.key == "error.platform_rejected" and caught.value.params["detail"] == "Bad codec"
    path = tmp_path / "long.mp4"
    path.write_bytes(b"x")
    with pytest.raises(ClippyError) as long:
        facebook.Facebook().publish(FakeHTTP({}), tokens, Upload(path, 91.0), POST, {})
    assert long.value.key == "error.clip_too_long_for" and long.value.params["max"] == 90


def test_twitch_sign_in_has_no_pkce_and_reads_the_display_name(monkeypatch):
    monkeypatch.setenv("CLIPPY_TWITCH_CLIENT_ID", "tid")
    monkeypatch.setenv("CLIPPY_TWITCH_CLIENT_SECRET", "tsecret")
    monkeypatch.delenv("CLIPPY_TWITCH_SCOPES", raising=False)
    platform = twitch.Twitch()
    asked = query(platform.auth_url(REDIRECT.format("twitch"), "st", "v"))
    assert asked["client_id"] == "tid" and asked["state"] == "st" and "code_challenge" not in asked
    http = FakeHTTP({
        ("POST", twitch.TOKEN): Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 14000,
                                           "scope": ["user:read:email"]}),
        ("GET", f"{twitch.API}/users"): Resp(200, {"data": [{"id": "55", "login": "sixth", "display_name": "SixthStar"}]}),
    })
    tokens = platform.exchange(http, "code", "v", REDIRECT.format("twitch"), 0.0)
    assert (tokens.name, tokens.user_id, tokens.scope) == ("SixthStar", "55", "user:read:email")
    assert "code_verifier" not in http.calls[0][2]["data"] and http.calls[0][2]["data"]["client_secret"] == "tsecret"
    assert http.calls[1][2]["headers"] == {"Authorization": "Bearer a", "Client-Id": "tid"}


def test_kick_sign_in_is_pkce_and_reads_the_user(monkeypatch):
    monkeypatch.setenv("CLIPPY_KICK_CLIENT_ID", "kid")
    monkeypatch.setenv("CLIPPY_KICK_CLIENT_SECRET", "ksecret")
    platform = kick.Kick()
    asked = query(platform.auth_url(REDIRECT.format("kick"), "st", "verifier"))
    assert asked["code_challenge"] == s256_challenge("verifier") and asked["code_challenge_method"] == "S256"
    assert asked["scope"] == "user:read channel:read" and kick.AUTH.startswith("https://id.kick.com/")
    http = FakeHTTP({
        ("POST", kick.TOKEN): Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600}),
        ("GET", f"{kick.API}/users"): Resp(200, {"data": [{"user_id": 7, "name": "SixthStar", "email": "x@y.z"}]}),
    })
    tokens = platform.exchange(http, "code", "verifier", REDIRECT.format("kick"), 0.0)
    assert (tokens.name, tokens.user_id) == ("SixthStar", "7")
    assert http.calls[0][2]["data"]["code_verifier"] == "verifier"
    assert "x@y.z" not in json.dumps(tokens.__dict__)


@pytest.fixture
def seven(tmp_path, monkeypatch, chat_env):
    storage, db, chat = chat_env
    for name, value in {"YOUTUBE_CLIENT_ID": "i", "YOUTUBE_CLIENT_SECRET": "s", "TIKTOK_CLIENT_KEY": "i",
                        "TIKTOK_CLIENT_SECRET": "s", "FACEBOOK_APP_ID": "i", "FACEBOOK_APP_SECRET": "s",
                        "X_CLIENT_ID": "i", "TWITCH_CLIENT_ID": "i", "TWITCH_CLIENT_SECRET": "s"}.items():
        monkeypatch.setenv(f"CLIPPY_{name}", value)
    for name in ("KICK_CLIENT_ID", "KICK_CLIENT_SECRET", "X_CLIENT_SECRET", "FACEBOOK_CONFIG_ID"):
        monkeypatch.delenv(f"CLIPPY_{name}", raising=False)
    http = FakeHTTP({
        ("POST", twitch.TOKEN): Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 14000}),
        ("GET", f"{twitch.API}/users"): Resp(200, {"data": [{"id": "55", "display_name": "SixthStar"}]}),
    })
    store = AccountStore(storage, http=http)
    runner = make_runner(storage, db, store)
    app = create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False)
    return TestClient(app, headers=CSRF, client=HERE), runner, chat


def test_the_accounts_status_lists_all_seven_and_says_which_can_post(seven):
    here, _, _ = seven
    accounts = here.get("/api/accounts").json()["accounts"]
    assert list(accounts) == list(PLATFORMS)
    assert {n: a["posting"] for n, a in accounts.items()} == {n: n in POSTING for n in PLATFORMS}
    assert accounts["instagram"]["configured"] and not accounts["instagram"]["oauth"]
    assert accounts["x"]["configured"] and not accounts["kick"]["configured"]
    assert here.post("/api/accounts/kick/connect").json()["detail"]["key"] == "error.platform_not_configured"


@pytest.mark.parametrize("name,host", [("youtube", "accounts.google.com"), ("facebook", "www.facebook.com"),
                                       ("tiktok", "www.tiktok.com"), ("x", "x.com"), ("twitch", "id.twitch.tv")])
def test_every_configured_platform_gets_its_own_sign_in_page(seven, name, host):
    from urllib.parse import urlsplit

    here, _, _ = seven
    url = here.post(f"/api/accounts/{name}/connect").json()["url"]
    assert urlsplit(url).netloc == host
    asked = query(url)
    assert asked["redirect_uri"].endswith(f"/oauth/{name}/callback") and asked["state"]


def test_twitch_connects_and_disconnects_but_cannot_be_posted_to(seven):
    here, runner, chat = seven
    url = here.post("/api/accounts/twitch/connect").json()["url"]
    state = query(url)["state"]
    assert here.get(f"/oauth/twitch/callback?code=c&state={state}", follow_redirects=False).status_code == 303
    status = here.get("/api/accounts").json()
    assert status["accounts"]["twitch"]["connected"] and status["accounts"]["twitch"]["name"] == "SixthStar"
    assert status["accounts"]["twitch"]["lasting"] and "token" not in json.dumps(status)
    assert here.post(f"/api/chats/{chat['id']}/clips/1/publish", json={"platforms": ["twitch"]}).status_code == 422
    assert here.post(f"/api/chats/{chat['id']}/clips/1/publish", json={"platforms": ["kick"]}).status_code == 422
    assert here.delete("/api/accounts/twitch").status_code == 200
    assert not here.get("/api/accounts").json()["accounts"]["twitch"]["connected"]


def test_the_chat_names_facebook_and_x_but_never_twitch():
    raw = [{"type": "publish", "clips": [2], "platforms": ["twitch"]}]
    for message, named in [("posztold ki a 2. klipet facebookra", ["facebook"]),
                           ("post clip 2 to twitter and tiktok", ["tiktok", "x"]),
                           ("upload clip 2 on X", ["x"])]:
        asked = agent.ActionValidator({1, 2}, {"source": "x"}, message)
        assert asked.validate(raw)[0]["platforms"] == named
