"""FastAPI application: serves the phone web app, REST API and the real-time WebSocket."""
from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import logging
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, ai, exporter, storage
from .config import EXPORT_DIR, ROOT_DIR, WEB_DIR, settings
from .hosts import Host, HostOffline, hosts
from .local_ocr import ocr as local_ocr
from .network import all_lan_ips, qr_svg
from .security import pairing, require_local, require_token, websocket_target
from .touchpad import InputController

log = logging.getLogger("smartpad")

@asynccontextmanager
async def lifespan(_app):
    # Load the local handwriting model in the background so the first "Convert" is fast.
    if settings.preload_ocr and ai.active_provider() == "local":
        local_ocr.start_background_load()
    yield


app = FastAPI(title="SmartPad", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

controller = InputController(enabled=settings.enable_input_control)


# ---------------------------------------------------------------------------
# Connected devices (shown on the computer's status page)
# ---------------------------------------------------------------------------
class DeviceRegistry:
    def __init__(self) -> None:
        self.devices: dict[str, dict] = {}
        self.sockets: dict[str, WebSocket] = {}

    def add(self, ws: WebSocket, name: str, host: Host | None = None) -> str:
        cid = uuid.uuid4().hex[:8]
        self.devices[cid] = {
            "id": cid,
            "host": host.id if host else None,
            "name": name[:60] or "Phone",
            "ip": ws.client.host if ws.client else "?",
            "since": time.time(),
            "last_seen": time.time(),
            "latency_ms": None,
            "events": 0,
        }
        self.sockets[cid] = ws
        return cid

    def remove(self, cid: str) -> None:
        self.devices.pop(cid, None)
        self.sockets.pop(cid, None)

    def for_host(self, host_id: str | None) -> list[dict]:
        return [d for d in self.devices.values() if d["host"] == host_id]

    async def disconnect_all(self, code: int = 4001, host_id: str | None = None) -> None:
        """Disconnect the phones of one computer (None = the server computer)."""
        for cid, ws in list(self.sockets.items()):
            if self.devices.get(cid, {}).get("host") != host_id:
                continue
            try:
                await ws.close(code=code)
            except Exception:
                pass


registry = DeviceRegistry()


def public_base_url(ip: str | None = None) -> str:
    scheme = "https" if settings.https else "http"
    return f"{scheme}://{ip or all_lan_ips()[0]}:{settings.port}"


def pairing_url(ip: str | None = None, token: str | None = None) -> str:
    # Token travels in the URL fragment, which browsers never send to servers or in Referer headers.
    return f"{public_base_url(ip)}/#pair={token or pairing.token}"


def reserved_tokens() -> set[str]:
    return {pairing.token}


def lan_host_of(request: Request) -> str | None:
    """The server address as seen by the caller, if phones on the LAN can use it too."""
    host = request.url.hostname
    if not host or host in {"localhost", "testserver"}:
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return host


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
_ASSET_VERSION = str(int(time.time()))


@app.get("/", include_in_schema=False)
def index():
    # Version the asset URLs so phones always load the current JS/CSS after an update.
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("styles.css", "app.js", "markdown.js"):
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={_ASSET_VERSION}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/desktop", include_in_schema=False, dependencies=[Depends(require_local)])
def desktop_page():
    return FileResponse(WEB_DIR / "desktop.html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Computer-only API (localhost)
# ---------------------------------------------------------------------------
@app.get("/api/desktop/status", dependencies=[Depends(require_local)])
def desktop_status():
    ips = all_lan_ips()
    return {
        "version": __version__,
        "urls": [{"ip": ip, "url": pairing_url(ip), "qr": qr_svg(pairing_url(ip))} for ip in ips],
        "devices": registry.for_host(None),
        "hosts": [h.public() | {"devices": len(registry.for_host(h.id))} for h in hosts.hosts.values()],
        "input_control": controller.enabled,
        "input_error": controller.error,
        "ai": ai.capabilities(),
        "https": settings.https,
        "export_dir": str(EXPORT_DIR),
    }


@app.post("/api/desktop/rotate", dependencies=[Depends(require_local)])
async def desktop_rotate():
    pairing.rotate()
    await registry.disconnect_all(code=4001, host_id=None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Other computers: download the helper, then each computer gets its own QR code
# ---------------------------------------------------------------------------
@app.get("/connect", include_in_schema=False)
def connect_page():
    """Opened in the browser of ANY computer on the network. Explains / downloads the helper,
    and (once the helper opens it with its key) shows that computer's own pairing QR code."""
    return FileResponse(WEB_DIR / "connect.html", headers={"Cache-Control": "no-cache"})


_AGENT_BAT = """@echo off
REM SmartPad helper: lets a phone control THIS computer's mouse through the SmartPad server.
cd /d "%~dp0"
if not exist ".venv\\Scripts\\python.exe" (
    where python >nul 2>nul || (echo Python 3.9+ is required: https://www.python.org/downloads/  - tick "Add to PATH" & pause & exit /b 1)
    echo First run: installing the helper, about 10 MB...
    python -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
    ".venv\\Scripts\\python.exe" -m pip install --disable-pip-version-check -q pynput websockets || (echo Install failed. & pause & exit /b 1)
)
".venv\\Scripts\\python.exe" smartpad_agent.py --server {server} %*
pause
"""

_AGENT_SH = """#!/bin/sh
# SmartPad helper: lets a phone control THIS computer's mouse through the SmartPad server.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/python -m pip install -q pynput websockets || exit 1
fi
exec .venv/bin/python smartpad_agent.py --server {server} "$@"
"""


@app.get("/agent/download", include_in_schema=False)
def agent_download(request: Request):
    """Zip with the helper script, pre-configured with this server's address."""
    server = f"{request.url.scheme}://{request.url.netloc}"
    exe = ROOT_DIR / "agent" / "dist" / "SmartPad Helper.exe"  # built by agent/build_helper_exe.bat
    root, py = "SmartPad-Helper/", "SmartPad-Helper/"
    if exe.exists():
        py = "SmartPad-Helper/with-python (macOS, Linux)/"
        start = "Windows: double-click 'SmartPad Helper.exe' (no Python needed).\n" \
                "  If Windows SmartScreen warns: More info -> Run anyway.\n" \
                "macOS/Linux: see the 'with-python' folder (needs Python 3.9+): sh start_helper.sh\n"
    else:
        start = "Windows: double-click 'Start SmartPad Helper.bat' (needs Python 3.9+).\n" \
                "macOS/Linux: sh start_helper.sh\n"
    readme = (
        "SmartPad helper\n\n" + start + "\n"
        "Your browser then opens a page with a QR code. Scan it with your phone:\n"
        "that phone will control THIS computer only. Keep the helper window open.\n"
        f"Server: {server}\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if exe.exists():
            z.write(exe, root + "SmartPad Helper.exe", compress_type=zipfile.ZIP_STORED)
            z.writestr(root + "server.txt", server + "\r\n")
        z.write(ROOT_DIR / "agent" / "smartpad_agent.py", py + "smartpad_agent.py")
        z.write(ROOT_DIR / "server" / "touchpad.py", py + "touchpad.py")
        z.writestr(py + "Start SmartPad Helper.bat", _AGENT_BAT.replace("{server}", server).replace("\n", "\r\n"))
        z.writestr(py + "start_helper.sh", _AGENT_SH.replace("{server}", server))
        z.writestr(root + "README.txt", readme.replace("\n", "\r\n"))
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="SmartPad-Helper.zip"'},
    )


def _host_from_key(request: Request) -> Host:
    h = hosts.by_key(request.headers.get("X-SmartPad-Host-Key"))
    if not h:
        raise HTTPException(401, "Unknown computer. Start the SmartPad helper on this computer again.")
    return h


@app.get("/api/host/status")
def host_status(request: Request):
    """Pairing QR + connected phones for ONE helper computer (needs that helper's secret key)."""
    h = _host_from_key(request)
    seen = lan_host_of(request)
    ips = ([seen] if seen else []) + [ip for ip in all_lan_ips() if ip != seen]
    return {
        "version": __version__,
        "host": h.public(),
        "urls": [{"ip": ip, "url": pairing_url(ip, h.token), "qr": qr_svg(pairing_url(ip, h.token))} for ip in ips],
        "devices": registry.for_host(h.id),
        "ai": ai.capabilities(),
        "https": settings.https,
    }


@app.post("/api/host/rotate")
async def host_rotate(request: Request):
    h = _host_from_key(request)
    hosts.rotate(h, reserved_tokens())
    await registry.disconnect_all(code=4001, host_id=h.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Phone API (requires pairing token)
# ---------------------------------------------------------------------------
@app.get("/api/ping")
def ping():
    """Unauthenticated reachability check (reveals nothing sensitive)."""
    return {"ok": True, "app": "smartpad", "version": __version__}


def target_input(target: Host | None) -> tuple[bool, str | None]:
    if target is None:
        return controller.enabled, controller.error
    if not target.online:
        return False, f"The SmartPad helper is not running on {target.name}. Start it on that computer."
    return target.input_control, target.input_error


def target_name(target: Host | None) -> str:
    return target.name if target else "server computer"


def ns(target: Host | None) -> str | None:
    return target.id if target else None


@app.get("/api/status")
def status(target: Host | None = Depends(require_token)):
    enabled, error = target_input(target)
    return {
        "ok": True,
        "version": __version__,
        "computer": target_name(target),
        "input_control": enabled,
        "input_error": error,
        "ai": ai.capabilities(),
        "devices": len(registry.for_host(ns(target))),
    }


class RecognizeIn(BaseModel):
    image: str = Field(..., description="PNG/JPEG data URL of the canvas")
    language: Literal["en", "ur"] = "en"


class OrganizeIn(BaseModel):
    text: str
    style: str = "academic"
    use_ai: bool = True


class ExportIn(BaseModel):
    markdown: str
    format: Literal["docx", "pdf", "txt", "md"]
    title: str = "SmartPad Document"


class TypeIn(BaseModel):
    text: str = Field(..., max_length=20000)


class DocIn(BaseModel):
    id: str | None = None
    title: str = "Untitled"
    style: str = "simple"
    raw_text: str = ""
    markdown: str = ""
    image: str | None = None
    save_image: bool = False


def _ai_error(exc: ai.AIError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.post("/api/recognize", dependencies=[Depends(require_token)])
def recognize(body: RecognizeIn):
    try:
        return ai.recognize(body.image, body.language)
    except ai.AIError as exc:
        return _ai_error(exc)


@app.post("/api/organize", dependencies=[Depends(require_token)])
def organize(body: OrganizeIn):
    try:
        return ai.organize(body.text, body.style, body.use_ai)
    except ai.AIError as exc:
        return _ai_error(exc)


@app.post("/api/export", dependencies=[Depends(require_token)])
def export(body: ExportIn):
    if not body.markdown.strip():
        raise HTTPException(400, "Nothing to export.")
    try:
        data, mime = exporter.export(body.markdown, body.format, body.title)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.exception("export failed")
        raise HTTPException(500, f"Export failed: {exc}") from exc
    name = exporter.safe_filename(body.title, body.format)
    return Response(content=data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/export/save")
async def export_to_computer(body: ExportIn, target: Host | None = Depends(require_token)):
    """Write the exported file onto the paired computer (exports/ folder, or the helper's folder)."""
    if not body.markdown.strip():
        raise HTTPException(400, "Nothing to export.")
    data, _ = await run_in_threadpool(exporter.export, body.markdown, body.format, body.title)
    name = exporter.safe_filename(body.title, body.format)
    if target is not None:
        reply = await _host_call(target, {"t": "save_file", "name": name, "data": base64.b64encode(data).decode()}, 60)
        return {"ok": True, "path": reply.get("path", name), "name": reply.get("name", name)}
    return await run_in_threadpool(_save_local_export, data, name)


def _save_local_export(data: bytes, name: str) -> dict:
    target = EXPORT_DIR / name
    stem, n = target.stem, 1
    while target.exists():
        target = EXPORT_DIR / f"{stem}_{n}{target.suffix}"
        n += 1
    target.write_bytes(data)
    return {"ok": True, "path": str(target), "name": target.name}


async def _host_call(target: Host, msg: dict, timeout: float) -> dict:
    try:
        reply = await target.call(msg, timeout=timeout)
    except HostOffline:
        raise HTTPException(503, f"The SmartPad helper is not running on {target.name}.")
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(504, f"{target.name} did not answer in time.")
    if not reply.get("ok"):
        raise HTTPException(500, reply.get("detail") or "The helper reported an error.")
    return reply


@app.post("/api/type")
async def type_on_computer(body: TypeIn, target: Host | None = Depends(require_token)):
    """Types text at the paired computer's current cursor position (e.g. into Word)."""
    if target is not None:
        await _host_call(target, {"t": "type_text", "text": body.text}, 180)
        return {"ok": True}
    if not controller.enabled:
        raise HTTPException(503, controller.error or "Input control disabled on the computer.")
    reply = await run_in_threadpool(controller.handle, {"t": "type", "text": body.text})
    if reply and reply.get("t") == "error":
        raise HTTPException(500, reply["detail"])
    return {"ok": True}


# Each computer has its own document history.
@app.get("/api/documents")
def documents(target: Host | None = Depends(require_token)):
    return storage.list_docs(ns(target))


@app.post("/api/documents")
def save_document(body: DocIn, target: Host | None = Depends(require_token)):
    return storage.save(body.model_dump(), ns(target))


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, target: Host | None = Depends(require_token)):
    try:
        return storage.get(doc_id, ns(target))
    except KeyError:
        raise HTTPException(404, "Document not found")


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, target: Host | None = Depends(require_token)):
    try:
        storage.delete(doc_id, ns(target))
    except KeyError:
        raise HTTPException(404, "Document not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Real-time channel (touchpad + heartbeat)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ok, target = websocket_target(ws)
    if not ok:
        await ws.send_json({"t": "error", "detail": "unauthorized"})
        await ws.close(code=4001)
        return
    cid = registry.add(ws, ws.query_params.get("device", "Phone"), target)
    log.info("device connected: %s (%s) -> %s", registry.devices[cid]["name"], registry.devices[cid]["ip"], target_name(target))
    enabled, error = target_input(target)
    await ws.send_json(
        {"t": "hello", "id": cid, "computer": target_name(target), "input_control": enabled, "input_error": error, "version": __version__}
    )
    last_notice = 0.0
    try:
        while True:
            raw = await ws.receive_text()
            dev = registry.devices.get(cid)
            if dev:
                dev["last_seen"] = time.time()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"t": "error", "detail": "bad json"})
                continue
            # Allow batching: the phone may send a list of events in one frame.
            events = msg if isinstance(msg, list) else [msg]
            forward: list[dict] = []
            for ev in events[:200]:
                if not isinstance(ev, dict):
                    continue
                if ev.get("t") == "latency" and dev:
                    dev["latency_ms"] = ev.get("ms")
                    continue
                if target is not None:
                    # Another computer: answer heartbeats here, send the rest to its helper.
                    if ev.get("t") == "ping":
                        await ws.send_json({"t": "pong", "ts": ev.get("ts")})
                        continue
                    forward.append(ev)
                    if dev:
                        dev["events"] += 1
                    continue
                reply = controller.handle(ev)
                if dev and ev.get("t") != "ping":
                    dev["events"] += 1
                if reply:
                    await ws.send_json(reply)
            if forward:
                try:
                    await target.forward(forward)
                except Exception:
                    if time.time() - last_notice > 5:
                        last_notice = time.time()
                        await ws.send_json({"t": "notice", "detail": f"The SmartPad helper is not running on {target.name}."})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        registry.remove(cid)
        log.info("device disconnected: %s", cid)


@app.websocket("/agent")
async def agent_endpoint(ws: WebSocket):
    """The SmartPad helper on another computer connects here and receives its phones' events.

    helper -> {"t": "hello", "id", "key", "name", "input_control", "input_error"}
    server -> {"t": "registered", "name", "console": "/connect#key=..."}
    server -> {"t": "events", "events": [...]}                 touchpad events to apply
    server -> {"t": "type_text" | "save_file", "rpc": n, ...}  helper replies {"rpc": n, "ok": ...}
    """
    await ws.accept()
    try:
        hello = json.loads(await ws.receive_text())
        host = hosts.register(str(hello.get("id", "")), str(hello.get("key", "")), str(hello.get("name", "")), reserved_tokens())
    except PermissionError as exc:
        await ws.send_json({"t": "error", "detail": str(exc), "fatal": True})
        await ws.close(code=4003)
        return
    except Exception as exc:
        await ws.send_json({"t": "error", "detail": f"bad hello: {exc}", "fatal": True})
        await ws.close(code=4000)
        return
    old = host.ws
    hosts.attach(host, ws, hello)
    if old is not None and old is not ws:
        try:  # the same computer reconnected: drop the stale socket
            await old.close(code=4002)
        except Exception:
            pass
    log.info("computer connected: %s (%s)", host.name, host.ip)
    await ws.send_json({"t": "registered", "name": host.name, "console": f"/connect#key={host.key}"})
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            host.last_seen = time.time()
            if not isinstance(msg, dict):
                continue
            if "rpc" in msg:
                host.resolve(msg)
            elif msg.get("t") == "state":
                host.input_control = bool(msg.get("input_control"))
                host.input_error = msg.get("input_error") or None
                if isinstance(msg.get("diag"), dict):
                    host.diag = msg["diag"]
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("helper websocket error")
    finally:
        hosts.detach(host, ws)
        log.info("computer disconnected: %s", host.name)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Server error: {exc}"})
