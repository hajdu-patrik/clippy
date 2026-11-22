"""Clipping campaigns, the way a clipping agency runs them: a client (a streamer, a podcast, a brand) gives a brief, a
rate per 1,000 views with a cap per post, and the moments to clip. Clippy cuts every source with the brief as its
picking rules, the clippers' post links are collected, their views are read from the public pages, and the payout is
worked out per post and for the whole campaign."""

from __future__ import annotations

import time
from typing import Callable

from ..download import probe, require_public_url
from ..tools import ClippyError
from . import presets
from .texts import english, tx
from .watch import generate_payload

MAX_SOURCES = 20
CLIPS_PER_SOURCE = 3
CHECK_EVERY = 3600.0


def payout(views: int, cpm: float, cap: float) -> float:
    """What a post earns: its views per thousand at the rate, never above the cap when there is one."""
    earned = max(0, int(views)) / 1000 * max(0.0, float(cpm))
    return round(min(earned, cap) if cap > 0 else earned, 2)


def present(campaign: dict, posts: list[dict]) -> dict:
    rows = [{**p, "payout": payout(p["views"], campaign["cpm"], campaign["cap"]),
             **({"error_t": p["problem"]} if (p.get("problem") or {}).get("key") else {})} for p in posts]
    return {**campaign, "active": bool(campaign["active"]), "posts": rows,
            "views": sum(p["views"] for p in rows), "owed": round(sum(p["payout"] for p in rows), 2)}


def clean_sources(urls) -> list[str]:
    """Public video links only, each once, at most MAX_SOURCES."""
    out: list[str] = []
    for url in urls or []:
        url = str(url).strip()
        if not url or url in out:
            continue
        try:
            require_public_url(url)
        except ClippyError:
            continue
        out.append(url)
    return out[:MAX_SOURCES]


def view_count(url: str, downloads) -> int:
    info = probe(url, downloads).info
    return int(info.get("view_count") or 0)


class Campaigns:
    def __init__(self, db, runner, hub, counter: Callable[[str], int] | None = None, clock=time.time):
        self.db, self.runner, self.hub, self.clock = db, runner, hub, clock
        self.counter = counter or (lambda url: view_count(url, runner.storage.cache("downloads")))

    def get(self, campaign_id: int) -> dict:
        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            raise ClippyError(key="error.no_campaign")
        return campaign

    def view(self, campaign_id: int) -> dict:
        return present(self.get(campaign_id), self.db.campaign_posts(campaign_id))

    def list(self) -> list[dict]:
        return [present(c, self.db.campaign_posts(c["id"])) for c in self.db.list_campaigns()]

    def save(self, campaign_id: int | None, fields: dict) -> dict:
        fields = {**fields, "sources": clean_sources(fields.get("sources"))}
        if fields.get("preset") not in {p.key for p in presets.WATCH_PRESETS}:
            fields["preset"] = "gameplay"
        if campaign_id is None:
            campaign = self.db.add_campaign(fields.pop("name"), **fields)
        else:
            self.get(campaign_id)
            campaign = self.db.update_campaign(campaign_id, **fields)
        self.changed()
        return self.view(campaign["id"])

    def delete(self, campaign_id: int) -> bool:
        gone = self.db.delete_campaign(campaign_id)
        self.changed()
        return gone

    def clip(self, campaign_id: int, n: int = CLIPS_PER_SOURCE) -> dict:
        """One chat per source that has none yet, clipped with the campaign's preset and its brief as the picking
        rules; the chats are kept on the campaign."""
        campaign = self.get(campaign_id)
        chats = dict(campaign["chat_ids"]) if isinstance(campaign.get("chat_ids"), dict) else {}
        alive = {c["id"] for c in self.db.list_chats()}
        chats = {url: cid for url, cid in chats.items() if cid in alive}
        started = 0
        for url in campaign["sources"] or []:
            if url in chats:
                continue
            require_public_url(url)
            chat = self.db.create_chat(f"{campaign['name']} · {campaign['client'] or url}"[:80])
            note = tx("campaign.chat_note", name=campaign["name"], client=campaign["client"] or "-")
            self.db.add_message(chat["id"], "assistant", english(note), data={"t": note})
            self.hub.publish(chat["id"], {"type": "chat", "chat": self.db.get_chat(chat["id"])})
            payload = generate_payload(url, {"preset": campaign["preset"], "n": n, "instructions": campaign["brief"]})
            self.runner.submit(chat["id"], "generate", payload)
            chats[url] = chat["id"]
            started += 1
        self.db.update_campaign(campaign_id, chat_ids=chats)
        self.changed()
        return {"started": started, "campaign": self.view(campaign_id)}

    def add_post(self, campaign_id: int, clipper: str, url: str) -> dict:
        self.get(campaign_id)
        require_public_url(url)
        if any(p["url"] == url for p in self.db.campaign_posts(campaign_id)):
            raise ClippyError(key="error.post_known")
        self.db.add_campaign_post(campaign_id, clipper.strip()[:60], url)
        self.changed()
        return self.view(campaign_id)

    def remove_post(self, campaign_id: int, post_id: int) -> bool:
        gone = self.db.delete_campaign_post(campaign_id, post_id)
        self.changed()
        return gone

    def refresh(self, campaign_id: int, force: bool = True) -> dict:
        """Reads every post's public view count (a link that fails keeps its last count and says why)."""
        now = self.clock()
        for post in self.db.campaign_posts(self.get(campaign_id)["id"]):
            if not force and now - float(post["checked"] or 0) < CHECK_EVERY:
                continue
            try:
                self.db.update_campaign_post(post["id"], views=self.counter(post["url"]), checked=now, problem={})
            except Exception as exc:
                self.db.update_campaign_post(post["id"], checked=now,
                                             problem=tx("error.views_unread", error=type(exc).__name__))
        self.changed()
        return self.view(campaign_id)

    def changed(self) -> None:
        self.hub.publish(None, {"type": "campaigns"})
