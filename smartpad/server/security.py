"""Pairing and request authentication.

Model:
  * The server creates a random pairing token once and keeps it in data/pairing_token
    (or uses SMARTPAD_TOKEN). "New pairing code" on the status page rotates it.
  * The token is shown ONLY on the computer (terminal + /desktop page, which is
    restricted to localhost) inside a QR code / pairing URL.
  * The phone stores the token and sends it on every REST call
    (`X-SmartPad-Token` header) and on the WebSocket (`?token=` query).
  * Anyone else on the Wi-Fi who opens the page without the token gets 401.
  * Other computers running the SmartPad helper get their OWN token (see hosts.py);
    the token decides which computer's mouse a phone controls.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets

from fastapi import HTTPException, Request, WebSocket

from .config import DATA_DIR, settings


_TOKEN_FILE = DATA_DIR / "pairing_token"


class Pairing:
    """Token persists in data/pairing_token so phones stay paired across restarts."""

    def __init__(self) -> None:
        if settings.pairing_token:
            self.token = settings.pairing_token
            return
        try:
            self.token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            self.token = ""
        if len(self.token) < 16:
            self.rotate()

    def rotate(self) -> str:
        self.token = secrets.token_urlsafe(18)
        if not settings.pairing_token:
            try:
                _TOKEN_FILE.write_text(self.token, encoding="utf-8")
            except OSError:
                pass
        return self.token

    def check(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode(), self.token.encode())


pairing = Pairing()


def is_local_client(host: str | None) -> bool:
    if not host:
        return False
    if host in {"testclient", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


LOCAL = None  # target meaning "the computer running the server itself"


def resolve_target(token: str | None):
    """Which computer does this pairing token control?

    Returns (True, None) for the server computer, (True, Host) for a computer running the
    helper, or (False, None) if the token is unknown.
    """
    from .hosts import hosts  # local import: hosts imports config only

    if pairing.check(token):
        return True, LOCAL
    h = hosts.by_token(token)
    return (h is not None), h


def require_token(request: Request):
    """FastAPI dependency for paired-only REST endpoints. Returns the target computer."""
    token = request.headers.get("X-SmartPad-Token") or request.query_params.get("token")
    ok, target = resolve_target(token)
    if not ok:
        raise HTTPException(status_code=401, detail="Not paired. Scan the QR code shown on your computer.")
    return target


def require_local(request: Request) -> None:
    """Only the computer itself may see the pairing token."""
    if not is_local_client(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="This page is only available on the computer running SmartPad.")


def websocket_target(ws: WebSocket):
    return resolve_target(ws.query_params.get("token"))


def websocket_authorized(ws: WebSocket) -> bool:
    return websocket_target(ws)[0]
