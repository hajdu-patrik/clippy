import threading

import pytest

from clippy.storage import Storage
from clippy.tools import ClippyError, ThreadContext, log, log_sink, report, thread_context
from clippy.web.clips import ClipLibrary, ClipPresenter
from clippy.web.db import DB
from clippy.web.hub import Hub
from clippy.web.jobs import Runner
from clippy.web.media import MediaStore


def test_a_stopped_job_ends_at_its_next_line_also_in_its_worker_threads():
    stop = threading.Event()
    seen = []
    with log_sink(seen.append, lambda f: None, stop):
        log("step one")
        carried = thread_context()
        stop.set()
        with pytest.raises(ClippyError) as caught:
            log("step two")
        assert caught.value.key == "error.chat_deleted"
        with pytest.raises(ClippyError):
            report(0.5)
        failure = []

        def worker():
            try:
                log("in a worker")
            except ClippyError as exc:
                failure.append(exc.key)

        thread = threading.Thread(target=carried.wrap(worker))
        thread.start()
        thread.join()
        assert failure == ["error.chat_deleted"]
    log("after the job: no stop here")
    assert seen == ["step one"]
    assert ThreadContext().stop is None


def test_deleting_a_chat_stops_its_jobs(tmp_path):
    storage = Storage(tmp_path / "data", tmp_path / "library")
    db = DB(tmp_path / "t.db")
    runner = Runner(db, Hub(), storage, ClipPresenter(MediaStore(storage), db), ClipLibrary(storage), start=False)
    first, other = db.create_chat("a"), db.create_chat("b")
    job = runner.submit(first["id"], "generate", {"url": "https://youtu.be/x"})
    kept = runner.submit(other["id"], "generate", {"url": "https://youtu.be/y"})
    runner.forget(first["id"])
    assert runner.stop_of(job).is_set() and not runner.stop_of(kept).is_set()


def test_unfinished_download_folders_are_swept_after_hours(tmp_path):
    import os
    import time

    from clippy.download import PARTIAL_MAX_AGE, remove_partial, sweep_partials

    old, fresh, cache = tmp_path / ".part-a-1", tmp_path / ".part-b-2", tmp_path / "video.mkv"
    for folder in (old, fresh):
        folder.mkdir()
        (folder / "x.part").write_bytes(b"x")
    cache.write_bytes(b"v")
    long_ago = time.time() - PARTIAL_MAX_AGE - 60
    os.utime(old, (long_ago, long_ago))
    assert sweep_partials(tmp_path) == 1
    assert not old.exists() and fresh.exists() and cache.exists()
    remove_partial(fresh)
    assert not fresh.exists()
