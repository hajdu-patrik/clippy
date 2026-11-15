import pytest

from clippy.download import require_public_url
from clippy.storage import Storage
from clippy.tools import ClippyError, safe_name


def test_purge_chat_and_source(tmp_path):
    st = Storage(tmp_path / "data", tmp_path / "lib")
    (st.chat_clips(3) / "clip_01_v1.mp4").write_bytes(b"x")
    (st.chat_work(3) / "tmp.ass").write_text("x")
    for kind, name in (("downloads", "abc.mkv"), ("audio", "abc.vocals.flac"), ("transcripts", "abc.large-v3.json"),
                       ("downloads", "other.mkv")):
        (st.cache(kind) / name).write_bytes(b"x")
    st.purge_chat(3)
    assert not (tmp_path / "data" / "chats" / "3").exists()
    removed = st.purge_source("abc")
    assert sorted(p.name for p in removed) == ["abc.large-v3.json", "abc.mkv", "abc.vocals.flac"]
    assert (st.cache("downloads") / "other.mkv").exists()


def test_scratch_folders_are_unique(tmp_path):
    st = Storage(tmp_path / "data", tmp_path / "lib")
    assert st.scratch() != st.scratch()


def test_library_folder_names_are_windows_safe(tmp_path):
    st = Storage(tmp_path / "data", tmp_path / "lib")
    assert st.library_dir('DESH: "a/b" <c>?').name == "DESH a b c"
    assert safe_name("...") == "Untitled"


@pytest.mark.parametrize("url", ["http://127.0.0.1:7860/api", "http://localhost/x", "http://10.0.0.5/v",
                                 "http://[::1]/v", "file:///C:/Windows/win.ini", "ftp://example.com/x"])
def test_private_and_non_http_links_are_refused(url):
    with pytest.raises(ClippyError):
        require_public_url(url)


def test_public_ip_is_accepted():
    assert require_public_url("https://93.184.216.34/video") == "https://93.184.216.34/video"
