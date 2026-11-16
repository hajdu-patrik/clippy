"""SQLite storage for chats, messages and clips: one connection, one lock, parameterised queries only."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'New chat',
    source TEXT,
    video_title TEXT,
    video_id TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    content TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    clip TEXT NOT NULL,
    overrides TEXT NOT NULL DEFAULT '{}',
    info TEXT NOT NULL DEFAULT '{}',
    mp4 TEXT, jpg TEXT, txt TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    platform TEXT NOT NULL,
    state TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    remote_id TEXT NOT NULL DEFAULT '',
    problem TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}',
    seen TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    checked REAL NOT NULL DEFAULT 0,
    problem TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    seen TEXT NOT NULL DEFAULT '[]',
    picks TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    ran REAL NOT NULL DEFAULT 0,
    problem TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    platforms TEXT NOT NULL DEFAULT '[]',
    settings TEXT NOT NULL DEFAULT '{}',
    due REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled(state, due);
CREATE INDEX IF NOT EXISTS idx_post_clip ON posts(chat_id, number, platform, id);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_clip_chat ON clips(chat_id, number, version);
"""

_JSON_COLS = {"options", "data", "clip", "overrides", "info", "problem", "settings", "seen", "platforms", "stats",
              "picks"}


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON_COLS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else {}
        except (TypeError, ValueError):
            d[k] = {}
    return d


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(chats)")}
            if "video_id" not in columns:
                self.conn.execute("ALTER TABLE chats ADD COLUMN video_id TEXT")
            if "timeline" not in {r[1] for r in self.conn.execute("PRAGMA table_info(clips)")}:
                self.conn.execute("ALTER TABLE clips ADD COLUMN timeline TEXT")
            if "stats" not in {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}:
                self.conn.execute("ALTER TABLE posts ADD COLUMN stats TEXT NOT NULL DEFAULT '{}'")
                self.conn.execute("ALTER TABLE posts ADD COLUMN stats_at REAL NOT NULL DEFAULT 0")
            try:
                self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clip_version ON clips(chat_id, number, version)")
            except sqlite3.IntegrityError:
                pass
            self.conn.commit()

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(sql, args).fetchone())

    def create_chat(self, title: str = "New chat") -> dict:
        now = time.time()
        cur = self._exec("INSERT INTO chats (title, created, updated) VALUES (?, ?, ?)", (title, now, now))
        return self.get_chat(cur.lastrowid)

    def get_chat(self, chat_id: int) -> dict | None:
        return self._one("SELECT * FROM chats WHERE id = ?", (chat_id,))

    def list_chats(self) -> list[dict]:
        return self._all("SELECT * FROM chats ORDER BY updated DESC")

    def update_chat(self, chat_id: int, **fields) -> dict | None:
        allowed = {"title", "source", "video_title", "video_id", "options"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                args.append(json.dumps(v, ensure_ascii=False) if k == "options" else v)
        sets.append("updated = ?")
        args.append(time.time())
        self._exec(f"UPDATE chats SET {', '.join(sets)} WHERE id = ?", (*args, chat_id))
        return self.get_chat(chat_id)

    def touch(self, chat_id: int) -> None:
        self._exec("UPDATE chats SET updated = ? WHERE id = ?", (time.time(), chat_id))

    def delete_chat(self, chat_id: int) -> None:
        self._exec("DELETE FROM chats WHERE id = ?", (chat_id,))

    def video_in_use(self, video_id: str) -> bool:
        return self._one("SELECT 1 AS used FROM chats WHERE video_id = ? LIMIT 1", (video_id,)) is not None

    def add_message(self, chat_id: int, role: str, content: str = "", kind: str = "text",
                    data: dict | None = None) -> dict:
        now = time.time()
        cur = self._exec(
            "INSERT INTO messages (chat_id, role, kind, content, data, created, updated) VALUES (?,?,?,?,?,?,?)",
            (chat_id, role, kind, content, json.dumps(data or {}, ensure_ascii=False), now, now))
        self.touch(chat_id)
        return self.get_message(cur.lastrowid)

    def update_message(self, msg_id: int, content: str | None = None, kind: str | None = None,
                       data: dict | None = None) -> dict | None:
        sets, args = ["updated = ?"], [time.time()]
        if content is not None:
            sets.append("content = ?")
            args.append(content)
        if kind is not None:
            sets.append("kind = ?")
            args.append(kind)
        if data is not None:
            sets.append("data = ?")
            args.append(json.dumps(data, ensure_ascii=False))
        self._exec(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", (*args, msg_id))
        return self.get_message(msg_id)

    def get_message(self, msg_id: int) -> dict | None:
        return self._one("SELECT * FROM messages WHERE id = ?", (msg_id,))

    def unfinished_jobs(self) -> list[dict]:
        """Progress cards of jobs that never finished (the server stopped while they waited or ran), oldest first."""
        rows = self._all("SELECT * FROM messages WHERE kind = 'progress' ORDER BY id")
        return [r for r in rows if not (r.get("data") or {}).get("done")]

    def list_messages(self, chat_id: int) -> list[dict]:
        return self._all("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))

    def add_clip(self, chat_id: int, number: int, version: int, clip: dict, overrides: dict, info: dict,
                 mp4: str, jpg: str, txt: str, timeline: str | None = None) -> dict:
        """`timeline` is the editor's Timeline JSON of this version (None for clips made before the editor)."""
        cur = self._exec(
            "INSERT INTO clips (chat_id, number, version, clip, overrides, info, mp4, jpg, txt, timeline, created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, number, version, json.dumps(clip, ensure_ascii=False), json.dumps(overrides, ensure_ascii=False),
             json.dumps(info, ensure_ascii=False, default=str), mp4, jpg, txt, timeline, time.time()))
        return self.get_clip(cur.lastrowid)

    def update_clip_texts(self, clip_id: int, clip: dict) -> dict | None:
        """The post texts of a stored version (title, description…); the video itself does not change."""
        self._exec("UPDATE clips SET clip = ? WHERE id = ?", (json.dumps(clip, ensure_ascii=False), clip_id))
        return self.get_clip(clip_id)

    def get_clip(self, clip_id: int) -> dict | None:
        return self._one("SELECT * FROM clips WHERE id = ?", (clip_id,))

    def clip_versions(self, chat_id: int, number: int) -> list[dict]:
        """Every version of one clip, oldest first."""
        return self._all("SELECT * FROM clips WHERE chat_id = ? AND number = ? ORDER BY version", (chat_id, number))

    def current_clips(self, chat_id: int) -> list[dict]:
        """Latest version of every non-deleted clip number."""
        return self._all(
            "SELECT c.* FROM clips c JOIN (SELECT number, MAX(version) v FROM clips WHERE chat_id = ? GROUP BY number) m "
            "ON c.number = m.number AND c.version = m.v WHERE c.chat_id = ? AND c.deleted = 0 ORDER BY c.number",
            (chat_id, chat_id))

    def next_number(self, chat_id: int) -> int:
        r = self._one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM clips WHERE chat_id = ?", (chat_id,))
        return int(r["n"])

    def mark_deleted(self, chat_id: int, number: int) -> int:
        cur = self._exec("UPDATE clips SET deleted = 1 WHERE chat_id = ? AND number = ?", (chat_id, number))
        return cur.rowcount

    def add_post(self, clip: dict, platform: str, state: str, url: str = "", remote_id: str = "",
                 problem: dict | None = None) -> dict:
        cur = self._exec(
            "INSERT INTO posts (chat_id, clip_id, number, platform, state, url, remote_id, problem, created) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (clip["chat_id"], clip["id"], clip["number"], platform, state, url, remote_id,
             json.dumps(problem or {}, ensure_ascii=False), time.time()))
        return self._one("SELECT * FROM posts WHERE id = ?", (cur.lastrowid,))

    def latest_posts(self, chat_id: int, number: int) -> list[dict]:
        """The newest post of a clip number on each platform (a failed try is replaced by the next one)."""
        return self._all(
            "SELECT p.* FROM posts p JOIN (SELECT platform, MAX(id) m FROM posts WHERE chat_id = ? AND number = ? "
            "GROUP BY platform) l ON p.id = l.m ORDER BY p.platform", (chat_id, number))

    def add_watch(self, url: str, title: str, settings: dict, seen: list[str], checked: float) -> dict:
        cur = self._exec("INSERT INTO watches (url, title, settings, seen, checked, created) VALUES (?,?,?,?,?,?)",
                         (url, title, json.dumps(settings, ensure_ascii=False), json.dumps(seen), checked,
                          time.time()))
        return self.get_watch(cur.lastrowid)

    def get_watch(self, watch_id: int) -> dict | None:
        return self._one("SELECT * FROM watches WHERE id = ?", (watch_id,))

    def list_watches(self) -> list[dict]:
        return self._all("SELECT * FROM watches ORDER BY id")

    def update_watch(self, watch_id: int, **fields) -> dict | None:
        allowed = {"title", "settings", "seen", "enabled", "checked", "problem"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                args.append(json.dumps(v, ensure_ascii=False) if k in _JSON_COLS else v)
        if sets:
            self._exec(f"UPDATE watches SET {', '.join(sets)} WHERE id = ?", (*args, watch_id))
        return self.get_watch(watch_id)

    def delete_watch(self, watch_id: int) -> bool:
        return self._exec("DELETE FROM watches WHERE id = ?", (watch_id,)).rowcount > 0

    def add_daily(self, game: str, settings: dict) -> dict:
        cur = self._exec("INSERT INTO daily (game, settings, created) VALUES (?,?,?)",
                         (game, json.dumps(settings, ensure_ascii=False), time.time()))
        return self.get_daily(cur.lastrowid)

    def get_daily(self, daily_id: int) -> dict | None:
        return self._one("SELECT * FROM daily WHERE id = ?", (daily_id,))

    def list_daily(self) -> list[dict]:
        return self._all("SELECT * FROM daily ORDER BY id")

    def update_daily(self, daily_id: int, **fields) -> dict | None:
        allowed = {"settings", "seen", "picks", "enabled", "ran", "problem"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                args.append(json.dumps(v, ensure_ascii=False) if k in _JSON_COLS else v)
        if sets:
            self._exec(f"UPDATE daily SET {', '.join(sets)} WHERE id = ?", (*args, daily_id))
        return self.get_daily(daily_id)

    def delete_daily(self, daily_id: int) -> bool:
        return self._exec("DELETE FROM daily WHERE id = ?", (daily_id,)).rowcount > 0

    def add_scheduled(self, chat_id: int, number: int, platforms: list[str], settings: dict, due: float) -> dict:
        cur = self._exec("INSERT INTO scheduled (chat_id, number, platforms, settings, due, created) VALUES (?,?,?,?,?,?)",
                         (chat_id, number, json.dumps(platforms), json.dumps(settings, ensure_ascii=False), due,
                          time.time()))
        return self._one("SELECT * FROM scheduled WHERE id = ?", (cur.lastrowid,))

    def get_scheduled(self, item_id: int) -> dict | None:
        return self._one("SELECT * FROM scheduled WHERE id = ?", (item_id,))

    def planned(self, chat_id: int | None = None, number: int | None = None) -> list[dict]:
        """Posts waiting for their time, soonest first (of one clip, one chat, or all)."""
        sql, args = "SELECT * FROM scheduled WHERE state = 'planned'", []
        if chat_id is not None:
            sql, args = sql + " AND chat_id = ?", [chat_id]
        if number is not None:
            sql, args = sql + " AND number = ?", [*args, number]
        return self._all(sql + " ORDER BY due", tuple(args))

    def due_posts(self, now: float) -> list[dict]:
        return self._all("SELECT * FROM scheduled WHERE state = 'planned' AND due <= ? ORDER BY due", (now,))

    def set_scheduled_state(self, item_id: int, state: str, only_from: str = "planned") -> bool:
        """Moves a planned post on (started, cancelled); False when it was not planned any more."""
        return self._exec("UPDATE scheduled SET state = ? WHERE id = ? AND state = ?",
                          (state, item_id, only_from)).rowcount > 0

    def posts_to_measure(self, now: float, every: float, max_age: float) -> list[dict]:
        """Posts that went up (not failed) on a platform that tells how they did, not read for `every` seconds."""
        return self._all(
            "SELECT p.*, c.info AS info FROM posts p JOIN clips c ON c.id = p.clip_id WHERE p.state != 'failed' "
            "AND p.remote_id != '' AND p.platform IN ('youtube', 'instagram') AND p.created > ? AND p.stats_at < ? "
            "ORDER BY p.stats_at, p.id", (now - max_age, now - every))

    def set_post_stats(self, post_id: int, stats: dict, at: float) -> None:
        self._exec("UPDATE posts SET stats = ?, stats_at = ? WHERE id = ?", (json.dumps(stats), at, post_id))

    def measured_posts(self) -> list[dict]:
        """Every post with stats, with its clip: what was posted and how it did."""
        return self._all(
            "SELECT p.*, c.clip AS clip, c.info AS info FROM posts p JOIN clips c ON c.id = p.clip_id "
            "WHERE p.stats_at > 0 ORDER BY p.id")
