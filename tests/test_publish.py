import json

import pytest
from fastapi.testclient import TestClient

from clippy import recovery
from clippy.highlights import Clip
from clippy.publish import PostResult, PostText, Tokens, Upload, checked, instagram, tiktok, youtube
from clippy.publish.accounts import AccountStore
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
    assert waits == [instagram.POLL_SECONDS]


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


class FakePlatform:
    oauth = True

    def __init__(self, name, outcome):
        self.name, self.outcome, self.posted = name, outcome, 0

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
