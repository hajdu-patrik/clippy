from datetime import date, datetime, timezone

import pytest
import requests
from fastapi.testclient import TestClient

from clippy import categories
from clippy.categories import Moment
from clippy.storage import Storage
from clippy.tools import ClippyError
from clippy.web import agent, daily
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.daily import Daily, daily_settings, present_daily
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import Runner
from clippy.web.media import MediaStore
from clippy.web.server import create_app

CSRF = {"X-Clippy-Request": "1"}
GTA6 = categories.game("gta-vi")


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def moment(vid, streamer, views, platform="Twitch", duration=30.0):
    return Moment(vid, f"https://www.twitch.tv/{streamer}/clip/{vid}", f"clip {vid}", streamer, platform, views,
                  duration)


def test_new_york_time_follows_the_us_daylight_rule():
    assert daily.eastern_offset(utc(2026, 11, 16, 12)) == -5
    assert daily.eastern_offset(utc(2026, 7, 1, 12)) == -4
    assert daily.eastern_offset(utc(2026, 3, 8, 6, 59)) == -5 and daily.eastern_offset(utc(2026, 3, 8, 7)) == -4
    assert daily.eastern_offset(utc(2026, 11, 1, 5, 59)) == -4 and daily.eastern_offset(utc(2026, 11, 1, 6)) == -5
    assert daily.eastern_time(date(2026, 11, 16), "19:00") == utc(2026, 11, 17, 0)
    assert daily.eastern_time(date(2026, 7, 1), "19:00") == utc(2026, 7, 1, 23)
    assert daily.eastern_clock(utc(2026, 11, 17, 0)) == "19:00"


def test_posting_times_come_at_least_an_hour_ahead_best_first():
    now = utc(2026, 11, 16, 16, 30)
    assert daily.post_times(["19:00", "15:00", "12:00"], 3, now) == [
        utc(2026, 11, 17, 0), utc(2026, 11, 16, 20), utc(2026, 11, 17, 17)]
    assert daily.post_times(["19:00", "15:00"], 1, now) == [utc(2026, 11, 17, 0)]


def test_daily_settings_keep_the_order_of_the_times():
    assert daily_settings() == {"n": 3, "at": "09:00", "times": ["19:00", "15:00", "12:00"]}
    assert daily_settings(5, "7.30", "21:00; 9:15 21:00") == {"n": 2, "at": "07:30", "times": ["21:00", "09:15"]}
    for bad in ({"times": "25:00"}, {"at": "noon"}):
        with pytest.raises(ClippyError) as caught:
            daily_settings(**bad)
        assert caught.value.key == "error.bad_slot"


def test_one_pick_per_streamer_and_nothing_twice():
    found = [moment("a", "kaicenat", 900), moment("b", "KaiCenat", 800), moment("c", "xqc", 700),
             moment("d", "jynxzi", 600), moment("e", "n3on", 500)]
    assert [m.vid for m in daily.choose(found, {"c"}, 3)] == ["a", "d", "e"]


def test_games_are_found_by_name_the_newer_one_first():
    assert categories.find_game("Csinálj minden nap 3 GTA VI klipet").key == "gta-vi"
    assert categories.find_game("gta 6 reactions").key == "gta-vi"
    assert categories.find_game("Grand Theft Auto V roleplay").key == "gta-v"
    assert categories.find_game("cs2 clips").key == "counter-strike"
    assert categories.find_game("gta vice city") is None and categories.find_game("lol") is None


def test_twitch_moments_read_the_clip_list():
    sent = []

    def post(query):
        sent.append(query)
        return {"data": {"game": {"clips": {"edges": [
            {"node": {"slug": "Funny-1", "url": "https://www.twitch.tv/kaicenat/clip/Funny-1", "title": "kai reacts",
                      "viewCount": 4200, "durationSeconds": 35, "createdAt": "2026-11-16T05:11:00Z",
                      "broadcaster": {"login": "kaicenat"}}},
            {"node": {"slug": "x", "url": "https://evil.example/x", "broadcaster": {"login": "x"}}}]}}}}

    found = categories.twitch_moments(GTA6, "en", post=post)
    assert 'game(slug: "grand-theft-auto-vi")' in sent[0] and "languages: [EN]" in sent[0]
    assert found == [Moment("twitch:Funny-1", "https://www.twitch.tv/kaicenat/clip/Funny-1", "kai reacts", "kaicenat",
                            "Twitch", 4200, 35.0, utc(2026, 11, 16, 5, 11))]


def test_kick_moments_keep_only_the_game_public_and_readable():
    def clip(cid, title="LMAOOO", slug="grand-theft-auto-vi", channel="wwx40", **extra):
        return {"id": cid, "title": title, "category": {"slug": slug}, "privacy": "public", "view_count": 50,
                "duration": 20, "created_at": "2026-11-16T01:39:41.01Z", "channel": {"slug": channel}, **extra}

    data = {"clips": [clip("clip_01M7GXNTQGA8R6RCAAK75C33DN"), clip("clip_01M7GXNTQGA8R6RCAAK75C33DA", slug="cs2"),
                      clip("clip_01M7GXNTQGA8R6RCAAK75C33DB", is_mature=True),
                      clip("clip_01M7GXNTQGA8R6RCAAK75C33DC", title="نذاله ابو عنابه"),
                      clip("clip_01M7GXNTQGA8R6RCAAK75C33DD", title="programação gta", channel="davyjones"),
                      clip("bad id")]}
    channels = {"wwx40": (1, "en"), "davyjones": (2, "pt")}

    def get(url):
        if "/categories/" in url:
            return data
        if "/api/v2/channels/" in url:
            return {"id": channels[url.rsplit("/", 1)[-1]][0]}
        creator = int(url.split("/channels/")[1].split("/")[0])
        return {"data": [{"language": next(lang for cid, lang in channels.values() if cid == creator)}]}

    found = categories.kick_moments(GTA6, "en", get=get)
    assert [m.url for m in found] == ["https://kick.com/wwx40/clips/clip_01M7GXNTQGA8R6RCAAK75C33DN"]
    assert found[0].platform == "Kick" and found[0].views == 50
    assert categories.kick_language("nobody", get=lambda url: None) == ""


def test_top_moments_skip_a_silent_platform_long_clips_and_adult_titles():
    def broken(game, language):
        raise requests.ConnectionError("down")

    def twitch(game, language):
        return [moment("a", "a", 10), moment("b", "b", 99), moment("c", "c", 50, duration=600),
                Moment("d", "https://www.twitch.tv/d/clip/d", "+18 stuff", "d", "Twitch", 70, 30.0)]

    assert [m.vid for m in categories.top_moments(GTA6, "en", sources=(broken, twitch))] == ["b", "a"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("clippy.web.daily.require_public_url", lambda url: url)
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    hub = Hub()
    runner = Runner(db, hub, storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    found = [moment("a", "kaicenat", 900), moment("b", "kaicenat", 800), moment("c", "xqc", 700),
             moment("d", "jynxzi", 600)]
    now = [utc(2026, 11, 20, 14)]
    return Daily(db, runner, hub, finder=lambda game, language: list(found), clock=lambda: now[0]), found, now


def test_a_daily_run_makes_one_chat_per_pick_with_its_time(env):
    picks, found, now = env
    d = picks.add("gta-vi", daily_settings())
    jobs = picks.runner.pending
    assert [j.payload["url"].rsplit("/", 1)[-1] for j in jobs] == ["a", "c", "d"]
    payload = jobs[0].payload
    assert payload["n"] == 1 and payload["options"]["layout"] == "stream"
    assert payload["options"]["whole"] is True and payload["options"]["max_len"] == 90.0
    assert "@kaicenat" in payload["instructions"] and "#GTA6" in payload["instructions"]
    chat = picks.db.get_chat(jobs[0].chat_id)
    assert chat["title"] == "GTA VI · kaicenat"
    note = picks.db.list_messages(chat["id"])[0]["data"]["t"]
    same_day = datetime.fromtimestamp(utc(2026, 11, 21, 0)).date() == datetime.fromtimestamp(now[0]).date()
    assert note["key"] == ("daily.pick_today" if same_day else "daily.pick_tomorrow")
    assert note["params"]["ny"] == "19:00" and note["params"]["i"] == 1
    assert [p["due"] for p in d["picks"]] == [utc(2026, 11, 21, 0), utc(2026, 11, 20, 20), utc(2026, 11, 20, 17)]
    assert d["seen"] == ["a", "c", "d"] and d["problem"] == {}
    shown = present_daily(picks.db, picks.runner.presenter, d)
    assert [p["streamer"] for p in shown["picks"]] == ["jynxzi", "xqc", "kaicenat"]
    assert shown["picks"][0]["text"] == "" and shown["picks"][0]["ny"] == "12:00" and not shown["picks"][0]["failed"]
    picks.db.add_message(shown["picks"][0]["chat_id"], "assistant", "No suitable clip found.", kind="error")
    assert present_daily(picks.db, picks.runner.presenter, d)["picks"][0]["failed"] is True


def test_the_daily_run_comes_once_a_day_at_its_time(env):
    picks, found, now = env
    d = picks.add("gta-vi", daily_settings(n=1, at="09:00"), now=False)
    now[0] = datetime(2026, 11, 21, 8, 59).timestamp()
    picks.tick()
    assert not picks.runner.pending
    now[0] = datetime(2026, 11, 21, 9, 1).timestamp()
    picks.tick()
    picks.tick()
    assert len(picks.runner.pending) == 1
    found.clear()
    now[0] = datetime(2026, 11, 22, 9, 1).timestamp()
    picks.tick()
    d = picks.db.get_daily(d["id"])
    assert len(picks.runner.pending) == 1 and d["problem"]["key"] == "error.daily_nothing"
    picks.db.delete_chat(picks.runner.pending[0].chat_id)
    assert present_daily(picks.db, picks.runner.presenter, d)["picks"] == []


def test_asking_again_changes_the_same_daily_pick(env):
    picks, found, now = env
    first = picks.add("gta-vi", daily_settings(n=1), now=False)
    again = picks.add("gta-vi", daily_settings(n=2, at="10:00"), now=False)
    assert first["id"] == again["id"] and again["settings"]["at"] == "10:00" and len(picks.db.list_daily()) == 1
    with pytest.raises(ClippyError) as caught:
        picks.add("tetris", daily_settings())
    assert caught.value.key == "error.daily_unknown_game"


def test_the_chat_starts_daily_picks_only_when_asked():
    chat = {"id": 1}
    assert agent.fast_path("Csinálj nekem minden nap 3 GTA VI klipet streamer reakciókkal", chat) == {
        "type": "daily", "game": "gta-vi", "n": 3}
    assert agent.fast_path("napi 2 gta 6 klip", chat)["n"] == 2
    assert agent.fast_path("every day gta 5 clips", chat)["game"] == "gta-v"
    assert agent.fast_path("csinálj 3 klipet a GTA VI trailerről", chat) is None
    assert agent.fast_path("minden nap posztolj", chat) is None


def test_daily_routes(tmp_path, monkeypatch):
    monkeypatch.setattr("clippy.web.daily.require_public_url", lambda url: url)
    monkeypatch.setattr("clippy.categories.top_moments", lambda game, language: [moment("a", "kaicenat", 9)])
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    client = TestClient(create_app(storage, db_path=tmp_path / "t.db", runner=runner, start_worker=False),
                        headers=CSRF)
    listed = client.get("/api/daily").json()
    assert listed["daily"] == [] and {"key": "gta-vi", "name": "GTA VI"} in listed["games"]
    bad = client.post("/api/daily", json={"game": "gta-vi", "times": "noon"})
    assert bad.status_code == 400 and bad.json()["detail"]["key"] == "error.bad_slot"
    made = client.post("/api/daily", json={"game": "gta-vi", "n": 3}).json()
    assert made["name"] == "GTA VI" and len(made["picks"]) == 1 and [j.kind for j in runner.pending] == ["generate"]
    assert client.post(f"/api/daily/{made['id']}/run").json() == {"new": 0}
    assert client.patch(f"/api/daily/{made['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/daily/{made['id']}").json() == {"ok": True}
    assert client.delete(f"/api/daily/{made['id']}").status_code == 404
    assert TestClient(client.app).post("/api/daily", json={"game": "gta-vi"}).status_code == 403
