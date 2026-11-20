"""Other computers ("hosts") that phones can control through the SmartPad helper.

Why a helper is needed: a web page cannot move the mouse of the computer it is opened on.
So every computer that wants its OWN phone touchpad runs `smartpad_agent.py` (downloaded from
http://<server>:<port>/connect). The helper connects to this server over a WebSocket (/agent).
The server then gives that computer its own pairing token / QR code, and every phone paired with
that token has its touchpad events forwarded to that helper only.

    phone A --(token A)--> server --(/agent ws)--> helper on computer A --> A's mouse
    phone B --(token B)--> server --(/agent ws)--> helper on computer B --> B's mouse
    phone C --(main token)--> server --> pynput on the server computer itself (unchanged)

Identity: the helper keeps a random `id` + secret `key` in a file next to itself, so it gets the
same pairing token back after restarts. The server keeps the tokens in data/hosts.json so phones
stay paired while the helper or the server restarts.
"""
from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import logging
import re
import secrets
import threading
import time

from fastapi import WebSocket

from .config import DATA_DIR

log = logging.getLogger("smartpad.hosts")

_FILE = DATA_DIR / "hosts.json"
_ID_RE = re.compile(r"^[a-f0-9]{16,32}$")
MAX_HOSTS = 200


class HostOffline(Exception):
    pass


class Host:
    def __init__(self, host_id: str, key: str, token: str, name: str) -> None:
        self.id = host_id
        self.key = key
        self.token = token
        self.name = name
        self.ws: WebSocket | None = None
        self.ip: str | None = None
        self.input_control = False
        self.input_error: str | None = None
        self.last_seen = 0.0
        self.diag: dict | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._send_lock = asyncio.Lock()

    @property
    def online(self) -> bool:
        return self.ws is not None

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
            "online": self.online,
            "input_control": self.input_control,
            "input_error": self.input_error,
            "last_seen": self.last_seen,
            "diag": self.diag,
        }

    async def send(self, msg) -> None:
        ws = self.ws
        if ws is None:
            raise HostOffline(self.name)
        async with self._send_lock:
            await ws.send_text(json.dumps(msg))

    async def forward(self, events: list[dict]) -> None:
        """Fire-and-forget touchpad events (move, click, scroll...)."""
        if events:
            await self.send({"t": "events", "events": events})

    async def call(self, msg: dict, timeout: float = 120) -> dict:
        """Request/response to the helper (typing text, saving a file)."""
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.send({**msg, "rpc": rid})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def resolve(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("rpc"))
        if fut and not fut.done():
            fut.get_loop().call_soon_threadsafe(lambda: fut.done() or fut.set_result(msg))

    def fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(HostOffline(self.name))
        self._pending.clear()


class HostRegistry:
    def __init__(self) -> None:
        self.hosts: dict[str, Host] = {}
        self._lock = threading.Lock()
        self._load()

    # ---- persistence ----------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for h in raw.get("hosts", []):
            try:
                self.hosts[h["id"]] = Host(h["id"], h["key"], h["token"], h.get("name") or "Computer")
            except KeyError:
                continue

    def _save(self) -> None:
        data = {"hosts": [{"id": h.id, "key": h.key, "token": h.token, "name": h.name} for h in self.hosts.values()]}
        try:
            _FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            log.warning("could not write %s", _FILE)

    # ---- lookup ---------------------------------------------------------
    def by_token(self, token: str | None) -> Host | None:
        if not token:
            return None
        for h in self.hosts.values():
            if hmac.compare_digest(token.encode(), h.token.encode()):
                return h
        return None

    def by_key(self, key: str | None) -> Host | None:
        if not key:
            return None
        for h in self.hosts.values():
            if hmac.compare_digest(key.encode(), h.key.encode()):
                return h
        return None

    # ---- helper registration -------------------------------------------
    def register(self, host_id: str, key: str, name: str, reserved_tokens: set[str]) -> Host:
        """Return the host for this helper identity, creating it on first contact."""
        if not _ID_RE.match(host_id or "") or len(key or "") < 20:
            raise ValueError("invalid helper identity")
        name = (name or "Computer").strip()[:60] or "Computer"
        with self._lock:
            h = self.hosts.get(host_id)
            if h:
                if not hmac.compare_digest(key.encode(), h.key.encode()):
                    raise PermissionError("helper key does not match")
                if h.name != name:
                    h.name = name
                    self._save()
                return h
            if len(self.hosts) >= MAX_HOSTS:
                raise ValueError("too many computers registered on this server")
            token = self._new_token(reserved_tokens)
            h = Host(host_id, key, token, name)
            self.hosts[host_id] = h
            self._save()
            return h

    def rotate(self, h: Host, reserved_tokens: set[str]) -> str:
        with self._lock:
            h.token = self._new_token(reserved_tokens)
            self._save()
        return h.token

    def forget(self, h: Host) -> None:
        with self._lock:
            self.hosts.pop(h.id, None)
            self._save()

    def _new_token(self, reserved: set[str]) -> str:
        used = reserved | {h.token for h in self.hosts.values()}
        while True:
            t = secrets.token_urlsafe(18)
            if t not in used:
                return t

    def attach(self, h: Host, ws: WebSocket, hello: dict) -> None:
        h.ws = ws
        h.ip = ws.client.host if ws.client else None
        h.input_control = bool(hello.get("input_control"))
        h.input_error = (str(hello.get("input_error"))[:200] or None) if hello.get("input_error") else None
        h.last_seen = time.time()

    def detach(self, h: Host, ws: WebSocket) -> None:
        if h.ws is ws:
            h.ws = None
            h.fail_pending()


hosts = HostRegistry()
