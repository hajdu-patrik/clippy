from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from clippy import download, streamers
from clippy.download import Upload
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


def utc(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("text, name", [
    ("klippeld N3on legújabb streamjét", "N3on"),
    ("N3on-nak a legutóbbi adása", "N3on"),
    ("a Kai Cenat-tól az utolsó stream", "Kai Cenat"),
    ("kai cenattól", "Kai Cenat"),
    ("kaicenat latest stream", "Kai Cenat"),
    ("IShowSpeednek a legfrissebb adása", "IShowSpeed"),
    ("clip the latest stream of i show speed", "IShowSpeed"),
    ("xQc-vel készült utolsó stream", "xQc"),
    ("hasan piker newest vod", "HasanAbi"),
    ("ÁDIN ROSS legújabb streamje", "Adin Ross"),
])
def test_a_streamer_is_found_in_a_message(text, name):
    assert streamers.resolve(text).name == name


def test_only_whole_words_and_known_endings_count():
    assert streamers.resolve("clip the latest stream of someone else") is None
    assert streamers.resolve("the newest stream was ninjas and cats") is None
    assert streamers.resolve("legújabb stream, absinthe és más") is None
    assert streamers.resolve("") is None


def test_an_unknown_name_is_guessed_only_from_an_explicit_handle():
    twitch = streamers.resolve("clip the latest stream of twitch.tv/Some_Guy")
    assert twitch.name == "Some_Guy" and twitch.handles() == {"twitch": "Some_Guy"}
    assert streamers.resolve("kick.com/some-guy legújabb streamje").handles() == {"kick": "some-guy"}
    assert streamers.resolve("youtube.com/@SomeGuy latest stream").handles() == {"youtube": "SomeGuy"}
    anywhere = streamers.resolve("latest stream of @someguy")
    assert anywhere.handles() == {"youtube": "someguy", "twitch": "someguy", "kick": "someguy"}
    assert streamers.resolve("mail me at someone@example.com about the latest stream") is None
    assert streamers.resolve("twitch.tv/videos/123") is None
    assert streamers.resolve("Kai Cenat twitch.tv/other").name == "Kai Cenat"


def test_the_directory_is_clean():
    assert len(streamers.DIRECTORY) >= 40
    assert len({s.name for s in streamers.DIRECTORY}) == len(streamers.DIRECTORY)
    forms = [f for s in streamers.DIRECTORY for f in s.aliases]
    assert len(set(forms)) == len(forms)
    for s in streamers.DIRECTORY:
        assert s.handles() and len(s.channels()) == len(s.handles())
        assert s.name.lower().replace(" ", "") in {f.replace(" ", "") for f in s.aliases}
    assert streamers.listing()[0] == {"name": "Kai Cenat", "urls": {"twitch": "https://www.twitch.tv/kaicenat"}}
    kai = streamers.resolve("kai cenat")
    assert kai.urls() == {"twitch": "https://www.twitch.tv/kaicenat"}
    assert streamers.Channel("youtube", "IbaiLlanos").page == "https://www.youtube.com/@IbaiLlanos/streams"
    assert streamers.Channel("twitch", "xqc").page == "https://www.twitch.tv/xqc/videos?filter=archives"
    assert streamers.Channel("kick", "xqc").page == "https://kick.com/xqc/videos"


class Listing:
    """Fake yt-dlp: listing pages by URL, probes by video URL; counts the network calls."""

    def __init__(self, pages=None, moments=None):
        self.pages, self.moments, self.listed, self.probed = pages or {}, moments or {}, [], []

    def lister(self, page, limit):
        self.listed.append(page)
        found = self.pages.get(page, [])
        if isinstance(found, Exception):
            raise found
        return found

    def moment(self, url):
        self.probed.append(url)
        found = self.moments[url]
        if isinstance(found, Exception):
            raise found
        return found

    def newest(self, streamer, kick_api=lambda slug, limit: []):
        return streamers.latest_stream(streamer, self.lister, self.moment, kick_api)


def test_the_newest_finished_stream_wins_across_platforms():
    streamer = streamers.Streamer("Someone", (), twitch="someone", kick="someone", youtube="Someone")
    fake = Listing({
        "https://www.twitch.tv/someone/videos?filter=archives": [Upload("v1", "https://www.twitch.tv/videos/1", "Twitch one"),
                                                                  Upload("v0", "https://www.twitch.tv/videos/0", "Older")],
        "https://www.youtube.com/@Someone/streams": [Upload("y1", "https://www.youtube.com/watch?v=y1", "YouTube one")],
        "https://kick.com/someone/videos": [Upload("k1", "https://kick.com/someone/videos/k1", "Kick one", utc(10))],
    }, {"https://www.twitch.tv/videos/1": (utc(13), True), "https://www.youtube.com/watch?v=y1": (utc(12), True)})
    found = fake.newest(streamer)
    assert (found.platform, found.url, found.title, found.date) == ("twitch", "https://www.twitch.tv/videos/1",
                                                                     "Twitch one", "2026-11-13")
    assert found.label == "Twitch" and found.streamer == "Someone"
    assert fake.probed.count("https://www.twitch.tv/videos/0") == 0
    assert len(fake.probed) == 2


def test_a_stream_that_is_on_now_gives_way_to_the_one_before_it():
    streamer = streamers.Streamer("Someone", (), twitch="someone")
    page = "https://www.twitch.tv/someone/videos?filter=archives"
    fake = Listing({page: [Upload("live", "https://www.twitch.tv/videos/3", "On now"),
                           Upload("v2", "https://www.twitch.tv/videos/2", "Finished"),
                           Upload("v1", "https://www.twitch.tv/videos/1", "Older")]},
                   {"https://www.twitch.tv/videos/3": (utc(15), False), "https://www.twitch.tv/videos/2": (utc(14), True)})
    assert fake.newest(streamer).title == "Finished"
    assert fake.probed == ["https://www.twitch.tv/videos/3", "https://www.twitch.tv/videos/2"]


def test_a_video_that_cannot_be_read_is_skipped_and_few_are_tried():
    streamer = streamers.Streamer("Someone", (), twitch="someone")
    page = "https://www.twitch.tv/someone/videos?filter=archives"
    uploads = [Upload(f"v{i}", f"https://www.twitch.tv/videos/{i}", f"T{i}") for i in range(5)]
    fake = Listing({page: uploads}, {u.url: ClippyError(key="error.download_gone", host="twitch.tv", detail="")
                                     for u in uploads})
    with pytest.raises(ClippyError) as caught:
        fake.newest(streamer)
    assert caught.value.key == "error.stream_none" and caught.value.params == {"streamer": "Someone"}
    assert len(fake.probed) == streamers.PROBES


def test_a_failing_platform_does_not_hide_the_others():
    streamer = streamers.Streamer("Someone", (), twitch="someone", youtube="Someone")
    fake = Listing({"https://www.twitch.tv/someone/videos?filter=archives": ClippyError(key="error.download_gone"),
                    "https://www.youtube.com/@Someone/streams": [Upload("y", "https://www.youtube.com/watch?v=y", "Y")]},
                   {"https://www.youtube.com/watch?v=y": (utc(9), True)})
    assert fake.newest(streamer).platform == "youtube"
    boom = Listing({"https://www.twitch.tv/someone/videos?filter=archives": RuntimeError("boom")})
    with pytest.raises(ClippyError):
        boom.newest(streamers.Streamer("Someone", (), twitch="someone"))


KICK_SAMPLE = {"data": [
    {"id": "01a1dc18-live", "is_live": True, "title": "On now", "start_time": "2026-11-15T20:00:00Z",
     "status": "public"},
    {"id": "01a1dc18-night", "is_live": False, "title": "Night stream", "start_time": "2026-11-14T22:41:47Z",
     "status": "public"},
    {"id": "01a1dc18-day", "is_live": False, "title": "Day stream", "start_time": "2026-11-13T10:00:00Z"},
    {"id": "01a1dc18-subs", "is_live": False, "title": "Subscribers only", "status": "subscriber_only"},
    {"id": "", "is_live": False, "title": "No id"},
]}


def test_kick_json_becomes_finished_uploads():
    found = streamers.kick_uploads(KICK_SAMPLE, "someone")
    assert [(u.vid, u.title) for u in found] == [("01a1dc18-night", "Night stream"), ("01a1dc18-day", "Day stream")]
    assert found[0].url == "https://kick.com/someone/videos/01a1dc18-night"
    assert datetime.fromtimestamp(found[0].when, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") == "2026-11-14 22:41:47"
    assert datetime.fromtimestamp(found[1].when, timezone.utc).day == 13
    assert streamers.kick_uploads({"error": "x"}, "someone") == [] and streamers.kick_uploads("nope", "x") == []
    assert streamers.kick_uploads({"data": [None, 4]}, "x") == []


class Answer:
    def __init__(self, status, data):
        self.status_code, self.data = status, data

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def test_kick_json_is_read_with_a_browser_agent_and_degrades_quietly(monkeypatch):
    seen = []
    answers = {"https://kick.com/api/v2/channels/someone": Answer(200, {"id": 77}),
               "https://web.kick.com/api/v1/channels/77/videos": Answer(200, KICK_SAMPLE)}

    def fake_get(url, headers, timeout):
        seen.append((url, headers))
        return answers[url]

    monkeypatch.setattr(streamers.requests, "get", fake_get)
    assert [u.vid for u in streamers.kick_api_streams("someone", 5)] == ["01a1dc18-night", "01a1dc18-day"]
    assert [u for u, _ in seen] == list(answers) and all("Mozilla/5.0" in h["User-Agent"] for _, h in seen)
    assert len(streamers.kick_api_streams("someone", 1)) == 1
    answers["https://web.kick.com/api/v1/channels/77/videos"] = Answer(403, "blocked")
    assert streamers.kick_api_streams("someone") == []
    answers["https://web.kick.com/api/v1/channels/77/videos"] = Answer(200, ValueError("html"))
    assert streamers.kick_api_streams("someone") == []
    answers["https://kick.com/api/v2/channels/someone"] = Answer(200, {"id": "x"})
    assert streamers.kick_api_streams("someone") == []
    assert streamers.kick_api_streams("not a slug/../x") == []

    def broken(url, headers, timeout):
        raise streamers.requests.ConnectionError("offline")

    monkeypatch.setattr(streamers.requests, "get", broken)
    assert streamers.kick_api_streams("someone") == []


def test_kick_falls_back_to_the_json_when_yt_dlp_cannot_list_it():
    streamer = streamers.Streamer("Someone", (), kick="someone")
    asked = []

    def api(slug, limit):
        asked.append(slug)
        return streamers.kick_uploads(KICK_SAMPLE, slug)

    unreadable = Listing({"https://kick.com/someone/videos": ClippyError(key="error.download_failed")})
    found = unreadable.newest(streamer, api)
    assert (found.platform, found.title, found.date) == ("kick", "Night stream", "2026-11-14") and asked == ["someone"]
    assert not unreadable.probed
    listed = Listing({"https://kick.com/someone/videos": [Upload("a", "https://kick.com/someone/videos/a", "Listed",
                                                                  utc(12))]})
    assert listed.newest(streamer, api).title == "Listed" and asked == ["someone"]
    with pytest.raises(ClippyError) as caught:
        unreadable.newest(streamer)
    assert caught.value.key == "error.stream_none"


class FakeYdl:
    def __init__(self, info):
        self.info = info

    def extract_info(self, url, download=False):
        return self.info


def fake_session(info):
    @contextmanager
    def session(opts):
        yield FakeYdl(info)

    return session


def test_listing_pages_drop_live_entries_and_keep_the_dates(monkeypatch):
    info = {"entries": [
        {"id": "a", "url": "https://kick.com/x/videos/a#__youtubedl_smuggle=%7B%7D", "title": "Live", "is_live": True},
        {"id": "b", "url": "https://www.youtube.com/watch?v=b", "title": "Upcoming", "live_status": "is_upcoming"},
        {"id": "c", "url": "https://kick.com/x/videos/c#__youtubedl_smuggle=%7B%7D", "title": "Done", "is_live": False,
         "timestamp": 1794696107},
        {"id": "d", "url": "https://www.youtube.com/watch?v=d", "title": "Past", "live_status": "was_live",
         "upload_date": "20261110"},
        {"id": "e", "url": "relative/only", "title": "Broken"},
        None,
    ]}
    monkeypatch.setattr(download, "_session", fake_session(info))
    found = download.past_streams("https://kick.com/x/videos")
    assert [(u.vid, u.url, u.when) for u in found] == [
        ("c", "https://kick.com/x/videos/c", 1794696107.0), ("d", "https://www.youtube.com/watch?v=d", utc(10, 0))]


def test_a_stream_page_tells_its_start_and_whether_it_is_over(monkeypatch):
    monkeypatch.setattr(download, "_session", fake_session({"timestamp": 1794696107, "live_status": "was_live"}))
    assert download.stream_moment("https://www.twitch.tv/videos/1") == (1794696107.0, True)
    monkeypatch.setattr(download, "_session", fake_session({"release_timestamp": 5, "is_live": True}))
    assert download.stream_moment("https://www.twitch.tv/videos/2") == (5.0, False)
    monkeypatch.setattr(download, "_session", fake_session({"live_status": "post_live"}))
    assert download.stream_moment("https://www.twitch.tv/videos/3") == (None, False)


CHAT = {"id": 1}


def test_the_chat_finds_the_latest_stream_even_when_the_model_misses_it():
    action = agent.fast_path("klippeld N3on legújabb streamjét", CHAT)
    assert action == {"type": "latest_stream", "streamer": "N3on", "handles": {"kick": "n3on", "youtube": "N3ON"},
                      "n": 0}
    assert agent.fast_path("clip the newest vod of Kai Cenat, 5 klipet", CHAT)["n"] == 5
    assert agent.fast_path("3 clips of the last stream of xQc", CHAT)["streamer"] == "xQc"
    assert agent.fast_path("klippeld Kai Cenat adását", CHAT) is None
    assert agent.fast_path("a legutolsó klip legyen rövidebb", CHAT) is None
    assert agent.fast_path("klippeld a legújabb streamet", CHAT) is None
    link = agent.fast_path("N3on legújabb streamje https://www.twitch.tv/videos/12", CHAT)
    assert link["type"] == "generate" and link["url"] == "https://www.twitch.tv/videos/12"


def test_the_model_cannot_start_a_lookup_the_user_did_not_ask_for():
    raw = [{"type": "latest_stream", "streamer": "xQc", "n": 7}]
    asked = agent.ActionValidator(set(), CHAT, "klippeld xQc legújabb streamjét").validate(raw)
    assert asked == [{"type": "latest_stream", "streamer": "xQc", "handles": {"twitch": "xqc", "kick": "xqc"}, "n": 7}]
    assert agent.ActionValidator(set(), CHAT, "klippeld xQc legújabb streamjét, 2 klip").validate(raw)[0]["n"] == 2
    other = agent.ActionValidator(set(), CHAT, "klippeld Kai Cenat legújabb streamjét").validate(raw)
    assert other[0]["streamer"] == "Kai Cenat" and other[0]["handles"] == {"twitch": "kaicenat"}
    assert agent.ActionValidator(set(), CHAT, "xQc streamjei jók").validate(raw) == []
    assert agent.ActionValidator(set(), CHAT, "klippeld a legújabb streamet").validate(raw) == []
    with_link = agent.ActionValidator(set(), CHAT, "xQc legújabb streamje https://youtu.be/abc")
    assert with_link.validate(raw) == []
    unknown = agent.ActionValidator(set(), CHAT, "klippeld Valaki legújabb streamjét")
    assert unknown.validate(raw) == [] and "twitch.tv/name" in unknown.notes[0]
    odd = agent.ActionValidator(set(), CHAT, "latest stream of xQc").validate([{"type": "latest_stream", "n": "many"}])
    assert odd[0]["n"] == 0


class Model:
    def __init__(self, out):
        self.out = out

    def chat_json(self, system, prompt, think=False, schema=None):
        return self.out


def test_the_agent_replies_about_the_lookup_and_keeps_the_models_own_action():
    ask = "klippeld N3on legújabb streamjét"
    missed = agent.run_agent(Model({"reply": "Küldj egy linket.", "actions": [{"type": "generate", "url": ""}]}),
                             CHAT, [], [], ask)
    assert [a["type"] for a in missed["actions"]] == ["latest_stream"] and missed["notes"] == []
    assert "N3on" in missed["reply"] and "link" not in missed["reply"]
    hit = agent.run_agent(Model({"reply": "Megkeresem.", "actions": [{"type": "latest_stream", "streamer": "N3on"}]}),
                          CHAT, [], [], ask)
    assert hit["reply"] == "Megkeresem." and [a["type"] for a in hit["actions"]] == ["latest_stream"]
    assert "latest_stream" in agent.SYSTEM_PROMPT and "latest_stream" in agent.AGENT_SCHEMA["properties"]["actions"][
        "items"]["properties"]["type"]["enum"]


class FakeLLM:
    reply = {"reply": "Megkeresem.", "actions": []}

    def chat_json(self, system, user, think=False, schema=None):
        return FakeLLM.reply


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr("clippy.web.chats.require_public_url", lambda url: url)
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    app = create_app(storage, db_path=tmp_path / "t.db", runner=runner, llm_factory=FakeLLM, start_worker=False)
    return TestClient(app, headers=CSRF), runner, db


def test_the_chat_clips_the_latest_stream_with_the_gameplay_preset(web, monkeypatch):
    client, runner, db = web
    asked = []

    def latest(streamer):
        asked.append(streamer)
        return streamers.Stream(streamer.name, "kick", "https://kick.com/n3on/videos/abc", "N3on x Twitchcon", utc(14))

    monkeypatch.setattr(streamers, "latest_stream", latest)
    cid = client.post("/api/chats").json()["id"]
    answer = client.post(f"/api/chats/{cid}/messages", json={"text": "klippeld N3on legújabb streamjét, 4 klipet"}).json()
    assert asked == [streamers.Streamer("N3on", (), kick="n3on", youtube="N3ON")]
    assert answer["actions"][0]["picked"]["platform"] == "Kick"
    text = answer["assistant"]["content"]
    assert "N3on" in text and "Kick" in text and "N3on x Twitchcon" in text and "2026-11-14" in text
    [job] = runner.pending
    assert job.kind == "generate" and job.chat_id == cid
    payload = job.payload
    assert payload["url"] == "https://kick.com/n3on/videos/abc" and payload["n"] == 4
    assert payload["options"]["layout"] == "stream" and payload["options"]["audio"] == "natural"
    assert payload["instructions"].startswith("Gameplay or stream.")
    assert db.get_chat(cid)["title"] == "N3on x Twitchcon"


def test_the_default_count_is_the_gameplay_presets_and_english_works_too(web, monkeypatch):
    client, runner, db = web
    monkeypatch.setattr(streamers, "latest_stream", lambda s: streamers.Stream(
        s.name, "twitch", "https://www.twitch.tv/videos/9", "GTA night", utc(12)))
    cid = client.post("/api/chats").json()["id"]
    FakeLLM.reply = {"reply": "", "actions": []}
    answer = client.post(f"/api/chats/{cid}/messages", json={"text": "clip the latest stream of Kai Cenat"}).json()
    assert answer["assistant"]["content"].startswith("Looking for the latest finished stream of Kai Cenat.")
    assert "Found it: Kai Cenat on Twitch" in answer["assistant"]["content"]
    assert runner.pending[0].payload["n"] == 5


def test_no_finished_stream_is_told_instead_of_started(web, monkeypatch):
    client, runner, db = web

    def nothing(streamer):
        raise ClippyError(key="error.stream_none", streamer=streamer.name)

    monkeypatch.setattr(streamers, "latest_stream", nothing)
    cid = client.post("/api/chats").json()["id"]
    answer = client.post(f"/api/chats/{cid}/messages", json={"text": "clip the latest stream of twitch.tv/some_guy"}).json()
    assert answer["actions"] == [] and not runner.pending
    assert "I found no finished stream of some_guy on YouTube, Twitch or Kick" in answer["assistant"]["content"]


def test_a_message_with_a_link_still_takes_the_normal_path(web, monkeypatch):
    client, runner, db = web
    monkeypatch.setattr(streamers, "latest_stream", lambda s: pytest.fail("no lookup for a link"))
    cid = client.post("/api/chats").json()["id"]
    FakeLLM.reply = {"reply": "", "actions": []}
    answer = client.post(f"/api/chats/{cid}/messages",
                         json={"text": "N3on legújabb streamje https://www.twitch.tv/videos/12"}).json()
    assert answer["actions"][0]["type"] == "generate" and runner.pending[0].payload["url"].endswith("/videos/12")


def test_the_streamer_names_are_listed(web):
    client, _, _ = web
    listed = client.get("/api/streamers").json()["streamers"]
    assert listed[0]["name"] == "Kai Cenat" and len(listed) == len(streamers.DIRECTORY)
    assert {"name": "xQc", "urls": {"twitch": "https://www.twitch.tv/xqc", "kick": "https://kick.com/xqc"}} in listed


def test_the_newest_dated_stream_wins_whatever_the_listing_order():
    from clippy.download import Upload
    from clippy.streamers import Streamer, latest_stream

    def lister(page, limit):
        return [Upload("a", "https://kick.com/x/videos/a", "older", 1_000.0),
                Upload("b", "https://kick.com/x/videos/b", "newer", 2_000.0)]

    streamer = Streamer("Example", ("example",), kick="examplecreator")
    found = latest_stream(streamer, lister=lister, moment=lambda url: (None, True), kick_api=lambda *a: [])
    assert found.title == "newer"
