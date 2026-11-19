from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from clippy.highlights import Clip
from clippy.storage import Storage
from clippy.web import agenda, schedule
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import Runner
from clippy.web.media import MediaStore
from clippy.web.server import create_app

CSRF = {"X-Clippy-Request": "1"}
HOUR = 3600.0


def local(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute).timestamp()


@pytest.fixture
def web(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    client = TestClient(create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False),
                        headers=CSRF)
    return client, runner, db, storage


def add_clip(db, storage, chat_id, number, title="T"):
    folder = storage.chat_clips(chat_id)
    mp4 = folder / f"clip_{number:02d}_v1.mp4"
    mp4.write_bytes(b"x")
    mp4.with_suffix(".jpg").write_bytes(b"jpg")
    return db.add_clip(chat_id, number, 1, Clip(0.0, 10.0, 80, title=f"{title}{number}").to_dict(), {},
                       {"duration_out": 10.0}, str(mp4), str(mp4.with_suffix(".jpg")), str(mp4.with_suffix(".txt")))


def test_the_agenda_lists_planned_posts_timed_videos_and_uploads_on_their_way(web):
    client, runner, db, storage = web
    chat = db.create_chat("Podcast")
    first, second = add_clip(db, storage, chat["id"], 1), add_clip(db, storage, chat["id"], 2)
    now = local(19, 12)
    planned = db.add_scheduled(chat["id"], 1, ["tiktok"], {}, now + 5 * HOUR)
    soon = db.add_post(first, "youtube", "scheduled", "https://youtube.com/shorts/abc", "abc", publish_at=now + HOUR)
    gone = db.add_post(second, "youtube", "scheduled", "https://youtube.com/shorts/old", "old", publish_at=now - HOUR)
    old = db.add_post(second, "youtube", "scheduled", "https://youtube.com/shorts/ancient", "ancient",
                      publish_at=now - 30 * 86400)
    db.add_post(first, "youtube", "public", "https://youtube.com/shorts/now", "now")
    runner.submit(chat["id"], "publish", {"clip": 2, "platforms": ["youtube"],
                                          "options": {"youtube": {"publish_at": now + 2 * HOUR}}})
    runner.submit(chat["id"], "publish", {"clip": 2, "platforms": ["youtube"], "options": {}})
    items = agenda.entries(db, MediaStore(storage), runner.jobs_of("publish"), now)
    assert [(e["state"], e["source"], e["platforms"]) for e in items] == [
        ("live", "platform", ["youtube"]), ("scheduled", "platform", ["youtube"]),
        ("uploading", "platform", ["youtube"]), ("planned", "plan", ["tiktok"])]
    live, upcoming, uploading, waiting = items
    assert live["id"] == gone["id"] and old["id"] not in {e.get("id") for e in items} and soon["id"] == upcoming["id"]
    assert upcoming["due"] == now + HOUR and upcoming["studio"] == "https://studio.youtube.com/video/abc/edit"
    assert upcoming["url"] == "https://youtube.com/shorts/abc" and "id" not in uploading
    assert waiting["id"] == planned["id"] and waiting["title"] == "T1" and waiting["chat"] == "Podcast"
    assert waiting["cover"] == f"/media/{chat['id']}/clip_01_v1.jpg" and waiting["number"] == 1


def test_a_video_timed_before_times_were_kept_has_no_time(web):
    client, runner, db, storage = web
    chat = db.create_chat("c")
    row = add_clip(db, storage, chat["id"], 1)
    db.add_post(row, "youtube", "scheduled", "https://youtube.com/shorts/v", "v")
    [item] = agenda.entries(db, MediaStore(storage), [], row["created"] + 60)
    assert item["state"] == "kept" and item["due"] is None


def test_free_slots_skip_what_is_taken(web):
    client, runner, db, storage = web
    chat = db.create_chat("c")
    row = add_clip(db, storage, chat["id"], 1)
    now = local(19, 10)
    db.add_scheduled(chat["id"], 1, ["tiktok"], {}, local(19, 11))
    db.add_post(row, "youtube", "scheduled", "https://youtube.com/shorts/v", "v", publish_at=local(19, 16))
    view = agenda.view(db, MediaStore(storage), [], ["11:00", "16:00", "20:00"], now)
    assert view["slots"] == ["11:00", "16:00", "20:00"] and view["saved"] is True
    assert view["free"][:3] == [local(19, 20), local(20, 11), local(20, 16)]
    assert schedule.taken_times(db, now) == {local(19, 11), local(19, 16)}


def test_slots_come_from_the_saved_times_or_else_the_watched_channels(web):
    client, runner, db, storage = web
    db.add_watch("https://www.youtube.com/@a", "A", {"slots": ["21:00", "18:00"]}, [], 0)
    db.add_watch("https://www.youtube.com/@b", "B", {"slots": ["18:00", "23:30"]}, [], 0)
    assert client.get("/api/schedule").json()["slots"] == ["18:00", "21:00", "23:30"]
    assert client.get("/api/schedule").json()["saved"] is False
    saved = client.put("/api/settings", json={"profile_links": "", "post_slots": "20.00, 11:00"}).json()
    assert saved["post_slots"] == ["11:00", "20:00"]
    assert client.get("/api/settings").json()["post_slots"] == ["11:00", "20:00"]
    assert client.get("/api/schedule").json()["slots"] == ["11:00", "20:00"]
    client.put("/api/settings", json={"profile_links": ""})
    assert client.get("/api/settings").json()["post_slots"] == ["11:00", "20:00"]
    bad = client.put("/api/settings", json={"profile_links": "", "post_slots": "noon"})
    assert bad.status_code == 400 and bad.json()["detail"]["key"] == "error.bad_slot"
    assert client.put("/api/settings", json={"profile_links": "", "post_slots": ""}).json()["post_slots"] == []


def test_the_schedule_offers_the_chats_with_finished_clips(web):
    client, runner, db, storage = web
    empty, full = db.create_chat("Empty"), db.create_chat("Full")
    add_clip(db, storage, full["id"], 1)
    add_clip(db, storage, full["id"], 2)
    db.mark_deleted(full["id"], 2)
    [only] = client.get("/api/schedule").json()["choices"]
    assert only["id"] == full["id"] and only["title"] == "Full" and empty["id"] != only["id"]
    assert only["clips"] == [{"number": 1, "title": "T1", "cover": f"/media/{full['id']}/clip_01_v1.jpg"}]


def test_a_planned_post_can_be_moved_and_a_timed_video_is_left_to_its_platform(web):
    client, runner, db, storage = web
    chat = db.create_chat("c")
    add_clip(db, storage, chat["id"], 1)
    item = db.add_scheduled(chat["id"], 1, ["tiktok"], {}, datetime.now().timestamp() + 2 * HOUR)
    later = datetime.now().timestamp() + 5 * HOUR
    assert client.patch(f"/api/scheduled/{item['id']}", json={"at": later}).json() == {
        "id": item["id"], "due": later, "platforms": ["tiktok"]}
    assert db.get_scheduled(item["id"])["due"] == later
    soon = client.patch(f"/api/scheduled/{item['id']}", json={"at": datetime.now().timestamp() + 5})
    assert soon.status_code == 400 and soon.json()["detail"]["key"] == "error.schedule_past"
    assert db.get_scheduled(item["id"])["due"] == later
    assert client.patch("/api/scheduled/999", json={"at": later}).status_code == 404
    assert client.delete(f"/api/scheduled/{item['id']}").json() == {"ok": True}
    assert client.patch(f"/api/scheduled/{item['id']}", json={"at": later}).status_code == 404
    assert client.get("/api/schedule").json()["items"] == []
    assert TestClient(client.app).patch(f"/api/scheduled/{item['id']}", json={"at": later}).status_code == 403
