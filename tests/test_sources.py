from pathlib import Path

import pytest

from clippy import i18n
from clippy.download import Remote, _single, explain
from clippy.tools import ClippyError

COLOURED = "\x1b[0;31mERROR:\x1b[0m [vimeo] 56015672: The web client only works when logged-in. Use --cookies"


@pytest.mark.parametrize("message, key", [
    (COLOURED, "error.download_login"),
    ("ERROR: [Reddit] 6rrwyj: Account authentication is required. Use --cookies-from-browser", "error.download_login"),
    ("ERROR: [youtube] abc: Sign in to confirm your age", "error.download_login"),
    ("ERROR: [youtube] BaW_jenozKc: This video is unavailable", "error.download_gone"),
    ("ERROR: [dailymotion] x2iuewm: Not found.", "error.download_gone"),
    ("ERROR: [TikTok] 1: HTTP Error 429: Too Many Requests", "error.download_busy"),
    ("ERROR: Unsupported URL: https://example.com/page", "error.download_unsupported"),
    ("ERROR: [facebook] 367: Cannot parse data; please report this issue on https://github.com/yt-dlp", "error.download_failed"),
])
def test_site_errors_become_a_readable_reason(message, key):
    err = explain(message, "vimeo.com")
    assert err.key == key and "\x1b" not in str(err) and "please report" not in str(err)
    assert i18n.t("hu", err.key, **err.params) != err.key


def test_live_streams_profiles_and_posts_without_video_are_refused():
    with pytest.raises(ClippyError) as live:
        _single({"id": "x", "live_status": "is_live"}, "twitch.tv")
    assert live.value.key == "error.download_live"
    with pytest.raises(ClippyError) as channel:
        _single({"_type": "playlist", "extractor_key": "YoutubeTab", "entries": [{"id": "a"}]}, "youtube.com")
    assert channel.value.key == "error.download_playlist"
    with pytest.raises(ClippyError) as photos:
        _single({"_type": "playlist", "extractor_key": "Instagram",
                 "entries": [{"id": "p", "formats": [{"vcodec": "none"}]}]}, "instagram.com")
    assert photos.value.key == "error.download_unsupported"


def test_a_carousel_post_gives_its_first_video():
    post = {"_type": "playlist", "extractor_key": "Instagram", "entries": [
        {"id": "photo", "formats": [{"vcodec": "none", "acodec": "none"}]},
        {"id": "clip", "formats": [{"vcodec": "h264", "acodec": "aac"}]}]}
    info, single, item = _single(post, "instagram.com")
    assert info["id"] == "clip" and single is False and item == 2
    assert Remote("u", info, "clip", "t", single, item).sound_track is None


def test_only_the_videos_own_split_sound_is_fetched_first():
    youtube = {"requested_formats": [{"format_id": "313", "vcodec": "vp9", "acodec": "none"},
                                     {"format_id": "251", "vcodec": "none", "acodec": "opus"}]}
    tiktok = {"format_id": "bytevc1_1080p", "formats": [{"format_id": "audio", "vcodec": "none", "acodec": "mp3"},
                                                        {"format_id": "bytevc1_1080p", "vcodec": "h265", "acodec": "aac"}]}
    assert Remote("u", youtube, "a", "t").sound_track == "251"
    assert Remote("u", tiktok, "b", "t").sound_track is None


def project_with(monkeypatch, tmp_path, duration, words):
    from clippy import pipeline
    from clippy.storage import Storage

    seen = {}

    def fake_find(transcript, llm, max_clips, min_len, max_len, **kw):
        seen.update(n=max_clips, min_len=min_len, max_len=max_len)
        return [pipeline.Clip(0.0, min_len, 80.0, hook="h")]

    monkeypatch.setattr(pipeline, "find_highlights", fake_find)
    monkeypatch.setattr(pipeline, "create_llm", lambda *a: type("L", (), {"describe": lambda self: "fake"})())
    proj = pipeline.Project("https://x", pipeline.Options(), Storage(tmp_path / "d", tmp_path / "l"))
    proj.transcript, proj.words, proj.title = {"duration": duration}, words, "A TikTok"
    return proj, seen


def test_a_short_video_becomes_one_clip_of_nearly_all_of_it(monkeypatch, tmp_path):
    proj, seen = project_with(monkeypatch, tmp_path, 15.0, [{"word": "hi", "start": 0.5, "end": 1.0}])
    [clip] = proj.select(n=5)
    assert seen == {"n": 1, "min_len": 9.0, "max_len": 15.0}
    assert (clip.start, clip.end, clip.hook, clip.alt_hook) == (0.0, 15.0, "", "")
    with pytest.raises(ClippyError):
        proj.select(exclude=[clip])
    proj, seen = project_with(monkeypatch, tmp_path, 3300.0, [{"word": "hi", "start": 0.5, "end": 1.0}])
    proj.select(n=5)
    assert seen == {"n": 5, "min_len": 20, "max_len": 60}


def test_a_whole_moment_stays_one_clip_even_when_the_picker_finds_nothing(monkeypatch, tmp_path):
    from clippy import pipeline

    proj, seen = project_with(monkeypatch, tmp_path, 35.0, [{"word": "yo", "start": 0.5, "end": 1.0}])
    proj.opts = pipeline.Options(min_len=20, max_len=90, whole=True)
    [clip] = proj.select(n=1)
    assert seen["n"] == 1 and seen["max_len"] == 35.0 and (clip.start, clip.end) == (0.0, 35.0)
    monkeypatch.setattr(pipeline, "find_highlights", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "create_llm", lambda *a: type("L", (), {"describe": lambda self: "fake",
                                                                          "backend": "ollama"})())
    written = []

    def fake_texts(llm, clips, words, title, text_language, signals=None):
        written.append(text_language)
        clips[0].description, clips[0].question = "Kai cannot believe it.", "Would you?"
        return 1

    monkeypatch.setattr(pipeline, "write_texts", fake_texts)
    proj.transcript["language"] = "en"
    [kept] = proj.select(n=1, instructions="Credit @kaicenat and use the hashtags #GTA6 #GTAVI.")
    assert (kept.start, kept.end, kept.title, kept.hook) == (0.0, 35.0, "A TikTok", "")
    assert written == ["English"] and kept.description == "Kai cannot believe it."
    assert kept.hashtags == ["#GTA6", "#GTAVI"]
    proj.opts = pipeline.Options(min_len=20, max_len=45)
    with pytest.raises(ClippyError) as caught:
        proj.select(n=1)
    assert caught.value.key == "error.no_suitable_clip"


def test_a_short_video_without_speech_is_kept_whole(monkeypatch, tmp_path):
    proj, _ = project_with(monkeypatch, tmp_path, 14.0, [])
    [clip] = proj.select()
    assert (clip.start, clip.end, clip.title) == (0.0, 14.0, "A TikTok")
    with pytest.raises(ClippyError) as more:
        proj.select(exclude=[clip])
    assert more.value.key == "error.empty_transcript"
    proj, seen = project_with(monkeypatch, tmp_path, 1800.0, [])
    proj.select()
    assert seen["n"] == 5, "a long video without speech is picked by its reactions"


def test_the_same_link_again_retries_when_the_chat_has_no_clips_yet():
    from clippy.web.agent import fast_path

    chat = {"source": "https://vm.tiktok.com/ZN85PJR9T/"}
    retry = fast_path("https://vm.tiktok.com/ZN85PJR9T/ csinálj belőle klipet", chat, has_clips=False)
    assert retry and retry["type"] == "generate" and retry["url"] == chat["source"]
    assert fast_path("https://vm.tiktok.com/ZN85PJR9T/", chat, has_clips=True) is None


def test_a_video_without_sound_stops_with_a_clear_reason(monkeypatch, tmp_path):
    from clippy import pipeline

    monkeypatch.setattr(pipeline, "probe_media", lambda path: {"video": (720, 1280), "audio": False})
    with pytest.raises(ClippyError) as silent:
        pipeline.require_sound(tmp_path / "reel.mp4", "https://www.instagram.com/reel/x/")
    assert silent.value.key == "error.no_sound" and silent.value.params["host"] == "www.instagram.com"
    monkeypatch.setattr(pipeline, "probe_media", lambda path: {"video": (720, 1280), "audio": True})
    pipeline.require_sound(tmp_path / "reel.mp4", "https://www.instagram.com/reel/x/")


@pytest.mark.parametrize("host, path, key", [
    ("www.tiktok.com", "/@tiktok", "error.download_playlist"),
    ("www.youtube.com", "/channel/UC123", "error.download_playlist"),
    ("www.instagram.com", "/natgeo/", "error.download_playlist"),
    ("vm.tiktok.com", "/ZN85PJR9T/", "error.download_failed"),
    ("www.tiktok.com", "/@tiktok/video/123", "error.download_failed"),
    ("www.facebook.com", "/radiokicksfm/videos/3676516585958356/", "error.download_failed"),
    ("clips.twitch.tv", "/FaintLightGullWholeWheat", "error.download_failed"),
    ("www.twitch.tv", "/xqc", "error.download_playlist"),
    ("youtu.be", "/abc123", "error.download_failed"),
])
def test_a_failing_profile_link_is_named_as_one(host, path, key):
    assert explain("ERROR: tiktok: Failed to parse JSON", host, path).key == key


def test_the_sites_rounded_length_does_not_cut_off_the_end(monkeypatch, tmp_path):
    proj, _ = project_with(monkeypatch, tmp_path, 15.74, [{"word": "hi", "start": 0.5, "end": 1.0}])
    proj.source_duration = 15.0
    assert proj.select()[0].end == 15.74
    proj, _ = project_with(monkeypatch, tmp_path, 25.3, [{"word": "hi", "start": 0.5, "end": 1.0}])
    proj.source_duration = 15.0
    assert proj.select()[0].end == 15.0


def test_a_cookies_file_is_used_through_a_private_copy(tmp_path, monkeypatch):
    from clippy import download
    original = "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tsecret\n"
    jar = tmp_path / "cookies.txt"
    jar.write_text(original, encoding="utf-8")
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen["file"] = Path(opts["cookiefile"])
            seen["text"] = seen["file"].read_text(encoding="utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            seen["file"].write_text("rewritten by yt-dlp", encoding="utf-8")

    monkeypatch.setattr(download, "_yt_dlp", lambda: type("M", (), {"YoutubeDL": FakeYDL}))
    monkeypatch.setenv("CLIPPY_COOKIES_FILE", str(jar))
    with download._session({"quiet": True}):
        pass
    assert seen["text"] == original and seen["file"] != jar
    assert jar.read_text(encoding="utf-8") == original and not seen["file"].exists()


def test_a_missing_cookies_file_is_a_clear_error(tmp_path, monkeypatch):
    from clippy import download
    monkeypatch.setenv("CLIPPY_COOKIES_FILE", str(tmp_path / "nope.txt"))
    with pytest.raises(ClippyError) as missing:
        download.cookies_file()
    assert missing.value.key == "error.cookies_file_missing" and str(tmp_path) not in str(missing.value.params)
    monkeypatch.delenv("CLIPPY_COOKIES_FILE")
    assert download.cookies_file() is None


def test_a_refused_link_is_asked_for_again(tmp_path, monkeypatch):
    from clippy import download
    import yt_dlp

    calls = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            calls.append(url)
            if len(calls) < 3:
                raise yt_dlp.utils.DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
            Path(self.opts["outtmpl"].replace("%(ext)s", "mkv")).write_bytes(b"video")

    monkeypatch.setattr(download, "_yt_dlp", lambda: type("M", (), {"YoutubeDL": FakeYDL, "utils": yt_dlp.utils}))
    monkeypatch.setattr(download, "RETRY_PAUSE", 0.0)
    remote = download.Remote("https://www.youtube.com/watch?v=x", {}, "vid1", "Video")
    done = download._download(remote, tmp_path, "vid1", {}, 1)
    assert done == tmp_path / "vid1.mkv" and done.read_bytes() == b"video" and len(calls) == 3
    assert not list(tmp_path.glob(".part-*"))

    calls.clear()
    monkeypatch.setattr(download, "FORBIDDEN_RETRIES", 1)
    again = tmp_path / "again"
    again.mkdir()
    with pytest.raises(ClippyError):
        download._download(remote, again, "vid1", {}, 1)
    assert len(calls) == 2
