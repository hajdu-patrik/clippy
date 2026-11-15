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
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_clip_chat ON clips(chat_id, number, version);
"""

_JSON_COLS = {"options", "data", "clip", "overrides", "info"}


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
