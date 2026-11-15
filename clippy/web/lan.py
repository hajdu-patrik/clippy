"""Phones and tablets on the same Wi-Fi: the server listens on the local network only when started with `--lan`, and
only devices paired through a one-time link (QR code) get in. The computer itself (127.0.0.1) needs no pairing."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import socket
import threading
import time
from pathlib import Path

from ..tools import read_json, write_json

COOKIE = "clippy_device"
COOKIE_AGE = 365 * 24 * 3600
CODE_SECONDS = 600
MAX_TRIES = 20


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback and not ip.is_link_local


def local_addresses() -> list[str]:
    """This computer's private IPv4 addresses, the one with the default route first."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(found) if is_private(ip)]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Lan:
    """Paired devices (only hashes of their tokens are stored) and the current one-time pairing code."""

    def __init__(self, devices_file: Path, port: int, addresses: list[str] | None = None):
        self.file, self.port = devices_file, port
        self.addresses = local_addresses() if addresses is None else addresses
        self._lock = threading.Lock()
        self._code: tuple[str, float] | None = None
        self._tries = 0
        self._devices: dict[str, dict] = read_json(self.file) if self.file.is_file() else {}

    @property
    def hosts(self) -> tuple[str, ...]:
        return tuple(self.addresses)

    def new_code(self) -> dict:
        """A fresh pairing link (valid for 10 minutes, once); an older link stops working."""
        with self._lock:
            code = secrets.token_urlsafe(12)
            self._code, self._tries = (code, time.monotonic() + CODE_SECONDS), 0
        host = self.addresses[0] if self.addresses else "127.0.0.1"
        return {"url": f"http://{host}:{self.port}/pair?code={code}", "minutes": CODE_SECONDS // 60}

    def pair(self, code: str, agent: str = "") -> str | None:
        """The device's new token when the code is right, else None."""
        with self._lock:
            current = self._code
            if current is None or time.monotonic() > current[1]:
                return None
            if not secrets.compare_digest(code.encode("utf-8"), current[0].encode("utf-8")):
                self._tries += 1
                if self._tries >= MAX_TRIES:
                    self._code = None
                return None
            self._code = None
            token = secrets.token_urlsafe(32)
            self._devices[_digest(token)] = {"paired": int(time.time()), "agent": agent[:120]}
            write_json(self.file, self._devices)
            return token

    def known(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return _digest(token) in self._devices

    def forget_all(self) -> int:
        with self._lock:
            count = len(self._devices)
            self._devices, self._code = {}, None
            write_json(self.file, self._devices)
            return count
