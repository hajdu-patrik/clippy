"""SQLite storage for chats, messages and clips: one connection, one lock, parameterised queries only."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .texts import NEW_TITLE

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
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    client TEXT NOT NULL DEFAULT '',
    brief TEXT NOT NULL DEFAULT '',
    preset TEXT NOT NULL DEFAULT 'gameplay',
    cpm REAL NOT NULL DEFAULT 0,
    cap REAL NOT NULL DEFAULT 0,
    sources TEXT NOT NULL DEFAULT '[]',
    chat_ids TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    clipper TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    checked REAL NOT NULL DEFAULT 0,
    problem TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled(state, due);
CREATE INDEX IF NOT EXISTS idx_campaign_posts ON campaign_posts(campaign_id, id);
CREATE INDEX IF NOT EXISTS idx_post_clip ON posts(chat_id, number, platform, id);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_clip_chat ON clips(chat_id, number, version);
"""

_JSON_COLS = {"options", "data", "clip", "overrides", "info", "problem", "settings", "seen", "platforms", "stats",
              "picks", "sources", "chat_ids"}


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
            if "cover_due" not in {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}:
                self.conn.execute("ALTER TABLE posts ADD COLUMN cover_due REAL NOT NULL DEFAULT 0")
                self.conn.execute("ALTER TABLE posts ADD COLUMN cover_tries INTEGER NOT NULL DEFAULT 0")
            if "publish_at" not in {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}:
                self.conn.execute("ALTER TABLE posts ADD COLUMN publish_at REAL NOT NULL DEFAULT 0")
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

    def create_chat(self, title: str = NEW_TITLE) -> dict:
        now = time.time()
        cur = self._exec("INSERT INTO chats (title, created, updated) VALUES (?, ?, ?)", (title, now, now))
        return self.get_chat(cur.lastrowid)

    def get_chat(self, chat_id: int) -> dict | None:
        return self._one("SELECT * FROM chats WHERE id = ?", (chat_id,))

    def list_chats(self) -> list[dict]:
        return self._all("SELECT * FROM chats ORDER BY updated DESC")

    def _update(self, table: str, row_id: int, allowed: tuple[str, ...], fields: dict) -> None:
        """Sets the allowed columns of one row; JSON columns are stored as JSON."""
        known = {k: v for k, v in fields.items() if k in allowed}
        if known:
            sets = ", ".join(f"{k} = ?" for k in known)
            args = [json.dumps(v, ensure_ascii=False) if k in _JSON_COLS else v for k, v in known.items()]
            self._exec(f"UPDATE {table} SET {sets} WHERE id = ?", (*args, row_id))

    def update_chat(self, chat_id: int, **fields) -> dict | None:
        self._update("chats", chat_id, ("title", "source", "video_title", "video_id", "options", "updated"),
                     {**fields, "updated": time.time()})
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

    def current_clip(self, chat_id: int, number: int) -> dict | None:
        return next((c for c in self.current_clips(chat_id) if c["number"] == number), None)

    def next_number(self, chat_id: int) -> int:
        r = self._one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM clips WHERE chat_id = ?", (chat_id,))
        return int(r["n"])

    def mark_deleted(self, chat_id: int, number: int) -> int:
        cur = self._exec("UPDATE clips SET deleted = 1 WHERE chat_id = ? AND number = ?", (chat_id, number))
        return cur.rowcount

    def add_post(self, clip: dict, platform: str, state: str, url: str = "", remote_id: str = "",
                 problem: dict | None = None, cover_due: float = 0.0, publish_at: float = 0.0) -> dict:
        """`cover_due`: when to try the cover again that the platform turned down at posting (0: nothing to do).
        `publish_at`: when the platform itself makes an uploaded video public (0: not timed)."""
        cur = self._exec(
            "INSERT INTO posts (chat_id, clip_id, number, platform, state, url, remote_id, problem, created, cover_due, "
            "publish_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (clip["chat_id"], clip["id"], clip["number"], platform, state, url, remote_id,
             json.dumps(problem or {}, ensure_ascii=False), time.time(), cover_due, publish_at))
        return self._one("SELECT * FROM posts WHERE id = ?", (cur.lastrowid,))

    def covers_due(self, now: float) -> list[dict]:
        return self._all(
            "SELECT p.*, c.jpg AS jpg FROM posts p JOIN clips c ON c.id = p.clip_id WHERE p.cover_due > 0 "
            "AND p.cover_due <= ? AND p.state != 'failed' AND p.remote_id != '' ORDER BY p.cover_due", (now,))

    def set_cover_retry(self, post_id: int, due: float, tries: int) -> None:
        self._exec("UPDATE posts SET cover_due = ?, cover_tries = ? WHERE id = ?", (due, tries, post_id))

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
        self._update("watches", watch_id, ("title", "settings", "seen", "enabled", "checked", "problem"), fields)
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
        self._update("daily", daily_id, ("settings", "seen", "picks", "enabled", "ran", "problem"), fields)
        return self.get_daily(daily_id)

    def delete_daily(self, daily_id: int) -> bool:
        return self._exec("DELETE FROM daily WHERE id = ?", (daily_id,)).rowcount > 0

    def add_campaign(self, name: str, **fields) -> dict:
        cur = self._exec("INSERT INTO campaigns (name, created) VALUES (?, ?)", (name, time.time()))
        return self.update_campaign(cur.lastrowid, **fields)

    def get_campaign(self, campaign_id: int) -> dict | None:
        return self._one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))

    def list_campaigns(self) -> list[dict]:
        return self._all("SELECT * FROM campaigns ORDER BY active DESC, id DESC")

    def update_campaign(self, campaign_id: int, **fields) -> dict | None:
        self._update("campaigns", campaign_id, ("name", "client", "brief", "preset", "cpm", "cap", "sources",
                                                "chat_ids", "active"), fields)
        return self.get_campaign(campaign_id)

    def delete_campaign(self, campaign_id: int) -> bool:
        return self._exec("DELETE FROM campaigns WHERE id = ?", (campaign_id,)).rowcount > 0

    def add_campaign_post(self, campaign_id: int, clipper: str, url: str) -> dict:
        cur = self._exec("INSERT INTO campaign_posts (campaign_id, clipper, url, created) VALUES (?,?,?,?)",
                         (campaign_id, clipper, url, time.time()))
        return self._one("SELECT * FROM campaign_posts WHERE id = ?", (cur.lastrowid,))

    def campaign_posts(self, campaign_id: int) -> list[dict]:
        return self._all("SELECT * FROM campaign_posts WHERE campaign_id = ? ORDER BY id", (campaign_id,))

    def update_campaign_post(self, post_id: int, **fields) -> None:
        self._update("campaign_posts", post_id, ("views", "checked", "problem"), fields)

    def delete_campaign_post(self, campaign_id: int, post_id: int) -> bool:
        return self._exec("DELETE FROM campaign_posts WHERE id = ? AND campaign_id = ?",
                          (post_id, campaign_id)).rowcount > 0

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

    def reschedule(self, item_id: int, due: float) -> bool:
        """A new time for a post that is still planned; False when it was not planned any more."""
        return self._exec("UPDATE scheduled SET due = ? WHERE id = ? AND state = 'planned'", (due, item_id)).rowcount > 0

    def timed_posts(self, since: float) -> list[dict]:
        """Videos a platform keeps and publishes at their time, with their clip: those timed from `since` on, and
        the ones from before times were recorded that went up since then (time 0)."""
        return self._all(
            "SELECT p.*, c.clip AS clip, c.jpg AS jpg FROM posts p JOIN clips c ON c.id = p.clip_id "
            "WHERE p.state = 'scheduled' AND p.remote_id != '' AND (p.publish_at >= ? OR (p.publish_at = 0 AND "
            "p.created >= ?)) ORDER BY p.publish_at, p.id", (since, since))

    def due_posts(self, now: float) -> list[dict]:
        return self._all("SELECT * FROM scheduled WHERE state = 'planned' AND due <= ? ORDER BY due", (now,))

    def set_scheduled_state(self, item_id: int, state: str, only_from: str = "planned") -> bool:
        """Moves a planned post on (started, cancelled); False when it was not planned any more."""
        return self._exec("UPDATE scheduled SET state = ? WHERE id = ? AND state = ?",
                          (state, item_id, only_from)).rowcount > 0

    def posts_to_measure(self, now: float, every: float, max_age: float) -> list[dict]:
        """Posts that went up (not failed) on a platform that tells how they did, not read for `every` seconds; a
        post linked by hand is read at least once however old it is."""
        return self._all(
            "SELECT p.*, c.info AS info FROM posts p JOIN clips c ON c.id = p.clip_id WHERE p.state != 'failed' "
            "AND p.remote_id != '' AND p.platform IN ('youtube', 'instagram') AND (p.created > ? OR "
            "(p.state = 'linked' AND p.stats_at = 0)) AND p.stats_at < ? ORDER BY p.stats_at, p.id",
            (now - max_age, now - every))

    def set_post_stats(self, post_id: int, stats: dict, at: float) -> None:
        self._exec("UPDATE posts SET stats = ?, stats_at = ? WHERE id = ?", (json.dumps(stats), at, post_id))

    def linked_post(self, platform: str, url: str, remote_id: str = "") -> dict | None:
        """The post (not failed) already known under this link or platform id."""
        return self._one(
            "SELECT * FROM posts WHERE platform = ? AND state != 'failed' AND (url = ? OR (remote_id != '' AND "
            "remote_id = ?)) ORDER BY id LIMIT 1", (platform, url, remote_id))

    def posts_to_find(self, platform: str) -> list[dict]:
        """Posts linked by hand whose id on the platform is not known yet."""
        return self._all("SELECT * FROM posts WHERE platform = ? AND state = 'linked' AND remote_id = '' ORDER BY id",
                         (platform,))

    def set_post_found(self, post_id: int, remote_id: str, created: float | None) -> None:
        """A post linked by hand gets its id on the platform and the time it really went up."""
        self._exec("UPDATE posts SET remote_id = ?, created = COALESCE(?, created) WHERE id = ? AND state = 'linked'",
                   (remote_id, created, post_id))

    def unlink_post(self, chat_id: int, number: int, post_id: int) -> bool:
        return self._exec("DELETE FROM posts WHERE id = ? AND chat_id = ? AND number = ? AND state = 'linked'",
                          (post_id, chat_id, number)).rowcount > 0

    def measured_posts(self) -> list[dict]:
        """Every post with stats, with its clip: what was posted and how it did."""
        return self._all(
            "SELECT p.*, c.clip AS clip, c.info AS info FROM posts p JOIN clips c ON c.id = p.clip_id "
            "WHERE p.stats_at > 0 ORDER BY p.id")
