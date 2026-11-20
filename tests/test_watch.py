from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from clippy.download import Upload, channel_lists
from clippy.highlights import Clip
from clippy.storage import Storage
from clippy.tools import ClippyError
from clippy.web import agent, schedule
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import Runner
from clippy.web.media import MediaStore
from clippy.web.server import create_app
from clippy.web.watch import NEW_PER_CHECK, Watcher, generate_payload, watch_settings

CSRF = {"X-Clippy-Request": "1"}


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute).timestamp()


def test_daily_slots_are_read_and_sorted():
    assert schedule.clean_slots("21.30, 9:05 18:00;18:00") == ["09:05", "18:00", "21:30"]
    assert schedule.clean_slots("") == []
    assert schedule.parse_slots("21.30, 9:05 18:00;18:00") == ["21:30", "09:05", "18:00"]
    for bad in ("25:00", "noon", "18:61"):
        with pytest.raises(ClippyError) as caught:
            schedule.clean_slots(bad)
        assert caught.value.key == "error.bad_slot"


def test_next_free_slots_skip_the_past_and_the_taken_ones():
    slots = ["18:00", "21:00"]
    assert schedule.next_slots(slots, 3, set(), at(15, 17)) == [at(15, 18), at(15, 21), at(16, 18)]
    assert schedule.next_slots(slots, 2, {at(15, 21)}, at(15, 17, 57)) == [at(16, 18), at(16, 21)]
    assert schedule.next_slots([], 2, set(), at(15, 17)) == []


def test_watch_settings_and_the_job_they_start():
    with pytest.raises(ClippyError) as caught:
        watch_settings(platforms=["youtube"])
    assert caught.value.key == "error.autopost_incomplete"
    settings = watch_settings(preset="gameplay", instructions="only the clutch plays", every=7, platforms=["tiktok"],
                              slots="20:00", n=99)
    assert settings["every"] == 60 and settings["n"] == 12 and settings["slots"] == ["20:00"]
    payload = generate_payload("https://www.twitch.tv/videos/1", settings)
    assert payload["n"] == 12 and payload["options"]["layout"] == "stream" and "n" not in payload["options"]
    assert payload["instructions"].startswith("Gameplay or stream.") and "clutch" in payload["instructions"]
    assert payload["autopost"] == {"platforms": ["tiktok"], "options": settings["options"], "slots": ["20:00"]}
    plain = generate_payload("https://youtu.be/x", watch_settings())
    assert plain["n"] == 5 and plain["options"] == {} and "autopost" not in plain


def test_channel_pages_list_uploads_and_past_streams():
    assert channel_lists("https://www.youtube.com/@SixthStarRider/") == [
        "https://www.youtube.com/@SixthStarRider/videos", "https://www.youtube.com/@SixthStarRider/streams"]
    assert channel_lists("https://www.twitch.tv/somebody") == ["https://www.twitch.tv/somebody/videos"]
    playlist = "https://www.youtube.com/playlist?list=PL1"
    assert channel_lists(playlist) == [playlist]


class Lister:
    def __init__(self, *videos):
        self.videos, self.fail = list(videos), None

    def __call__(self, url, limit):
        if self.fail:
            raise self.fail
        return "Sixth Star", [Upload(v, f"https://www.youtube.com/watch?v={v}", f"Video {v}") for v in self.videos]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("clippy.web.watch.require_public_url", lambda url: url)
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    hub = Hub()
    runner = Runner(db, hub, storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    now = [at(15, 12)]
    lister = Lister("c", "b", "a")
    return Watcher(db, runner, hub, lister=lister, clock=lambda: now[0]), lister, now


def test_a_new_watch_clips_only_what_comes_later(env):
    watcher, lister, now = env
    w = watcher.add("https://www.youtube.com/@x", watch_settings(n=3), latest=False)
    assert w["title"] == "Sixth Star" and w["seen"] == ["c", "b", "a"] and not watcher.runner.pending
    lister.videos = ["e", "d", "c", "b", "a"]
    assert watcher.check(watcher.db.get_watch(w["id"])) == 2
    jobs = watcher.runner.pending
    assert [j.payload["url"][-1] for j in jobs] == ["d", "e"] and all(j.kind == "generate" for j in jobs)
    chat = watcher.db.get_chat(jobs[0].chat_id)
    assert chat["title"] == "Video d"
    note = watcher.db.list_messages(chat["id"])[0]
    assert note["data"]["t"] == {"key": "watch.new_video", "params": {"channel": "Sixth Star", "title": "Video d"}}
    assert watcher.db.get_watch(w["id"])["seen"][:2] == ["e", "d"]
    assert watcher.check(watcher.db.get_watch(w["id"])) == 0


def test_a_flood_of_new_videos_is_capped_and_the_newest_comes_now(env):
    watcher, lister, now = env
    w = watcher.add("https://www.youtube.com/@x", watch_settings(), latest=True)
    assert [j.payload["url"][-1] for j in watcher.runner.pending] == ["c"]
    lister.videos = [str(i) for i in range(9)] + ["c", "b", "a"]
    assert watcher.check(watcher.db.get_watch(w["id"])) == NEW_PER_CHECK
    assert len(watcher.db.get_watch(w["id"])["seen"]) == 12


def test_a_failing_channel_is_noted_and_checked_again_later(env):
    watcher, lister, now = env
    w = watcher.add("https://www.youtube.com/@x", watch_settings(every=30))
    lister.fail = ClippyError(key="error.watch_empty")
    now[0] += 31 * 60
    watcher.tick()
    stored = watcher.db.get_watch(w["id"])
    assert stored["problem"]["key"] == "error.watch_empty" and stored["checked"] == now[0]
    lister.fail = None
    now[0] += 10 * 60
    watcher.tick()
    assert watcher.db.get_watch(w["id"])["problem"]["key"] == "error.watch_empty"
    now[0] += 30 * 60
    watcher.tick()
    assert watcher.db.get_watch(w["id"])["problem"] == {}
    watcher.db.update_watch(w["id"], enabled=0, checked=0)
    lister.videos = ["new"]
    watcher.tick()
    assert not watcher.runner.pending


def add_clip(db, chat_id, number, tmp_path):
    mp4 = tmp_path / f"clip{chat_id}_{number}.mp4"
    mp4.write_bytes(b"x")
    return db.add_clip(chat_id, number, 1, Clip(0.0, 10.0, 80, title=f"T{number}").to_dict(), {},
                       {"duration_out": 10.0}, str(mp4), str(mp4.with_suffix(".jpg")), str(mp4.with_suffix(".txt")))


def test_planned_posts_start_on_time_and_skip_deleted_clips(env, tmp_path):
    watcher, lister, now = env
    db = watcher.db
    chat = db.create_chat("c")
    for number in (1, 2):
        add_clip(db, chat["id"], number, tmp_path)
    keep = schedule.plan(db, chat["id"], 1, ["youtube"], {"youtube": {"privacy": "public"}}, now[0] + 3600, now[0])
    gone = schedule.plan(db, chat["id"], 2, ["tiktok"], {}, now[0] + 3600, now[0])
    later = schedule.plan(db, chat["id"], 1, ["tiktok"], {}, now[0] + 7200, now[0])
    with pytest.raises(ClippyError) as caught:
        schedule.plan(db, chat["id"], 1, ["tiktok"], {}, now[0] + 10, now[0])
    assert caught.value.key == "error.schedule_past"
    db.mark_deleted(chat["id"], 2)
    assert watcher.post_due() == 0
    now[0] += 3600
    assert watcher.post_due() == 1
    [job] = watcher.runner.pending
    assert job.kind == "publish" and job.payload == {"clip": 1, "platforms": ["youtube"],
                                                     "options": {"youtube": {"privacy": "public"}}}
    assert db.get_scheduled(keep["id"])["state"] == "started"
    assert db.get_scheduled(gone["id"])["state"] == "dropped"
    assert [s["id"] for s in db.planned()] == [later["id"]]
    assert watcher.post_due() == 0


def test_new_clips_of_a_watch_fill_the_free_slots(env, tmp_path):
    watcher, lister, now = env
    db = watcher.db
    first, second = db.create_chat("a"), db.create_chat("b")
    autopost = {"platforms": ["youtube"], "options": {}, "slots": ["18:00", "21:00"]}
    assert [s["due"] for s in schedule.plan_in_slots(db, first["id"], [2, 1], autopost, now[0])] == [at(15, 18),
                                                                                                     at(15, 21)]
    planned = schedule.plan_in_slots(db, second["id"], [1], autopost, now[0])
    assert [(s["number"], s["due"]) for s in planned] == [(1, at(16, 18))]
    assert [s["number"] for s in db.planned(first["id"])] == [1, 2]


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr("clippy.web.watch.require_public_url", lambda url: url)
    monkeypatch.setattr("clippy.web.watch.latest_uploads", Lister("b", "a"))
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    client = TestClient(create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False),
                        headers=CSRF)
    return client, runner, db


def test_watch_routes(web):
    client, runner, db = web
    bad = client.post("/api/watches", json={"url": "https://www.youtube.com/@x", "platforms": ["youtube"]})
    assert bad.status_code == 400 and bad.json()["detail"]["key"] == "error.autopost_incomplete"
    made = client.post("/api/watches", json={"url": "https://www.youtube.com/@x", "preset": "podcast", "latest": True,
                                             "platforms": ["youtube"], "slots": "18:00"}).json()
    assert made["title"] == "Sixth Star" and made["settings"]["slots"] == ["18:00"]
    assert [j.kind for j in runner.pending] == ["generate"] and runner.pending[0].payload["autopost"]
    listed = client.get("/api/watches").json()
    assert [w["id"] for w in listed["watches"]] == [made["id"]] and 60 in listed["every"]
    assert client.patch(f"/api/watches/{made['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.post(f"/api/watches/{made['id']}/check").json() == {"new": 0}
    assert client.delete(f"/api/watches/{made['id']}").json() == {"ok": True}
    assert client.delete(f"/api/watches/{made['id']}").status_code == 404
    assert TestClient(client.app).post("/api/watches", json={"url": "https://www.youtube.com/@x"}).status_code == 403


def test_a_post_can_be_planned_from_the_card_and_cancelled(web, tmp_path):
    client, runner, db = web
    chat = db.create_chat("c")
    add_clip(db, chat["id"], 1, tmp_path)
    from clippy.publish import Tokens

    runner.accounts.save("youtube", Tokens(access_token="a", expires_at=9e12, name="Me"))
    runner.accounts.save("tiktok", Tokens(access_token="t", expires_at=9e12, name="Me"))
    when = datetime.now().timestamp() + 7200
    answer = client.post(f"/api/chats/{chat['id']}/clips/1/publish",
                         json={"platforms": ["youtube", "tiktok"], "at": when}).json()
    item = answer["scheduled"]
    [job] = runner.pending
    assert job.payload["platforms"] == ["youtube"] and job.payload["options"]["youtube"]["publish_at"] == when
    clip = next(iter(client.get(f"/api/chats/{chat['id']}").json()["clips"].values()), None)
    assert clip is None or clip["scheduled"][0]["id"] == item
    row = db.current_clips(chat["id"])[0]
    assert runner.presenter.present(row)["scheduled"] == [{"id": item, "due": when, "platforms": ["tiktok"]}]
    past = client.post(f"/api/chats/{chat['id']}/clips/1/publish", json={"platforms": ["youtube"], "at": 1000})
    assert past.status_code == 400 and past.json()["detail"]["key"] == "error.schedule_past"
    assert client.delete(f"/api/scheduled/{item}").json() == {"ok": True}
    assert client.delete(f"/api/scheduled/{item}").status_code == 404
    assert runner.presenter.present(row)["scheduled"] == []


def test_the_chat_watches_a_channel_only_when_asked():
    chat = {"id": 1}
    action = agent.fast_path("figyeld ezt a gaming csatornát, 4 klip legyen: https://www.twitch.tv/somebody", chat)
    assert action == {"type": "watch", "url": "https://www.twitch.tv/somebody", "n": 4, "preset": "gameplay"}
    assert agent.fast_path("https://youtu.be/abc follow the speaker, the streamer laughs", chat)["type"] == "generate"
    raw = [{"type": "watch", "url": "https://evil.example/x"}]
    asked = agent.ActionValidator(set(), chat, "watch this podcast channel https://www.youtube.com/@pod")
    assert asked.validate(raw) == [{"type": "watch", "url": "https://www.youtube.com/@pod", "n": 0,
                                    "preset": "podcast"}]
    assert agent.ActionValidator(set(), chat, "make clips https://www.youtube.com/@pod").validate(raw) == []


def test_an_editor_base_unused_for_a_week_is_removed(env):
    import os

    from clippy.web import watch

    watcher, _, now = env
    old, fresh = watcher.db.create_chat("old"), watcher.db.create_chat("fresh")
    storage = watcher.runner.storage
    for chat, age in ((old, watch.BASE_MAX_AGE + 60), (fresh, 60)):
        f = storage.chat_base(chat["id"]) / "k.mkv"
        f.write_bytes(b"x")
        os.utime(f, (now[0] - age, now[0] - age))
    assert watcher.sweep_bases(now[0]) == 1
    assert not (storage.data / "chats" / str(old["id"]) / "base").exists()
    assert (storage.chat_base(fresh["id"]) / "k.mkv").exists()
