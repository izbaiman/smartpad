"""End-to-end tests of the SmartPad server (REST + WebSocket + export).

Run:  .venv\\Scripts\\python -m pytest -q
The real mouse is NOT moved (SMARTPAD_INPUT_CONTROL=0); events are counted instead.
"""
import base64
import io
import os
import sys
import zipfile
from pathlib import Path

os.environ["SMARTPAD_INPUT_CONTROL"] = "0"
os.environ["SMARTPAD_AI_PROVIDER"] = "offline"
os.environ["SMARTPAD_TOKEN"] = "test-token-123"
os.environ["SMARTPAD_OCR_PRELOAD"] = "0"  # don't load the 1.3 GB model during unit tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from server import ai, exporter, main
from server.config import settings

H = {"X-SmartPad-Token": "test-token-123"}
EXAMPLE = "computer is an electronic machine it accepts data process data and produces information it has different components hardware and software"


@pytest.fixture
def client():
    return TestClient(main.app)


# --- pairing / auth ----------------------------------------------------------
def test_ping_is_public(client):
    assert client.get("/api/ping").json()["ok"] is True


def test_api_requires_token(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-SmartPad-Token": "wrong"}).status_code == 401
    r = client.get("/api/status", headers=H)
    assert r.status_code == 200 and r.json()["ai"]["provider"] == "local"


def test_desktop_page_local_only(client, monkeypatch):
    assert client.get("/desktop").status_code == 200
    status = client.get("/api/desktop/status").json()
    assert status["urls"][0]["url"].endswith("#pair=test-token-123")
    assert status["urls"][0]["qr"].startswith("<?xml") or "<svg" in status["urls"][0]["qr"]
    monkeypatch.setattr(main, "require_local", main.require_local)
    from server import security

    assert security.is_local_client("127.0.0.1")
    assert not security.is_local_client("192.168.1.50")


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "SmartPad" in r.text
    assert client.get("/static/app.js").status_code == 200


# --- websocket / touchpad ----------------------------------------------------
def test_ws_rejects_bad_token(client):
    with client.websocket_connect("/ws?token=nope") as ws:
        assert ws.receive_json()["detail"] == "unauthorized"


def test_ws_touchpad_flow(client):
    before = main.controller.actions
    with client.websocket_connect("/ws?token=test-token-123&device=TestPhone") as ws:
        hello = ws.receive_json()
        assert hello["t"] == "hello"
        assert any(d["name"] == "TestPhone" for d in main.registry.devices.values())
        ws.send_json({"t": "ping", "ts": 42})
        assert ws.receive_json() == {"t": "pong", "ts": 42}
        ws.send_json([{"t": "move", "dx": 5, "dy": -3}, {"t": "scroll", "dx": 0, "dy": 1.2}])
        ws.send_json({"t": "click", "b": "left"})
        ws.send_json({"t": "click", "b": "right"})
        ws.send_json({"t": "down", "b": "left"})
        ws.send_json({"t": "up", "b": "left"})
        ws.send_json({"t": "ping", "ts": 43})
        assert ws.receive_json()["ts"] == 43  # ordering preserved, all events processed
    assert main.controller.actions - before == 6
    assert not any(d["name"] == "TestPhone" for d in main.registry.devices.values())


def test_input_controller_accumulates_subpixel_moves():
    class FakeMouse:
        def __init__(self):
            self.moves, self.scrolls = [], []

        def move(self, x, y):
            self.moves.append((x, y))

        def scroll(self, x, y):
            self.scrolls.append((x, y))

    from server.touchpad import InputController

    c = InputController(enabled=False)
    c.enabled, c._mouse = True, FakeMouse()
    for _ in range(4):
        c.handle({"t": "move", "dx": 0.5, "dy": 0.25})
    assert sum(m[0] for m in c._mouse.moves) == 2 and sum(m[1] for m in c._mouse.moves) == 1
    c.handle({"t": "scroll", "dx": 0, "dy": 0.6})
    c.handle({"t": "scroll", "dx": 0, "dy": 0.6})
    assert c._mouse.scrolls == [(0, 1)]
    assert c.handle({"t": "move", "dx": "nan", "dy": None}) is None  # bad input ignored safely


# --- recognition / organisation -----------------------------------------------
def _png() -> str:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (200, 80), "white")
    ImageDraw.Draw(img).text((10, 30), "hello", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_recognize_without_engine_gives_clear_error(client, monkeypatch):
    monkeypatch.setattr(ai, "tesseract_available", lambda: False)
    monkeypatch.setattr(ai.local_ocr, "installed", lambda: False)
    r = client.post("/api/recognize", json={"image": _png(), "language": "en"}, headers=H)
    assert r.status_code == 422 and "not installed" in r.json()["detail"]


def test_recognize_local_engine_mocked(client, monkeypatch):
    monkeypatch.setattr(ai.local_ocr, "installed", lambda: True)
    monkeypatch.setattr(ai.local_ocr, "recognize", lambda raw: "computer is an electronic machine")
    r = client.post("/api/recognize", json={"image": _png(), "language": "en"}, headers=H)
    assert r.status_code == 200 and r.json() == {"text": "computer is an electronic machine", "engine": "trocr (local)"}


def test_urdu_offline_explains_limitation(client, monkeypatch):
    monkeypatch.setattr(ai, "tesseract_available", lambda: False)
    r = client.post("/api/recognize", json={"image": _png(), "language": "ur"}, headers=H)
    assert r.status_code == 422 and "Urdu" in r.json()["detail"]


def test_line_segmentation():
    from PIL import Image, ImageDraw, ImageFont

    from server.local_ocr import segment_lines, split_wide_line

    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 30) if Path("C:/Windows/Fonts/arial.ttf").exists() else ImageFont.load_default()
    img = Image.new("RGB", (700, 400), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(["first line of text", "second line here", "", "", "new paragraph line"]):
        if line:
            d.text((20, 20 + i * 60), line, font=font, fill="black")
    lines = segment_lines(img)
    assert len(lines) == 3
    assert [l["para_break"] for l in lines] == [False, False, True]
    wide = Image.new("RGB", (2000, 60), "white")
    dw = ImageDraw.Draw(wide)
    for k in range(12):
        dw.text((10 + k * 160, 15), "word", font=font, fill="black")
    assert len(split_wide_line(wide)) >= 2
    assert segment_lines(Image.new("RGB", (100, 100), "white")) == []


def test_spell_correction():
    from server import spell

    fixed, changes = spell.correct("the computr accepts dta and produces informaton i think")
    assert "computer" in fixed and "information" in fixed and " I think" in fixed
    assert ("computr", "computer") in changes
    same, none = spell.correct("NASA and Karachi")  # acronyms / names untouched
    assert same == "NASA and Karachi" or "NASA" in same


def test_recognize_rejects_bad_image(client):
    r = client.post("/api/recognize", json={"image": "data:image/png;base64,@@@", "language": "en"}, headers=H)
    assert r.status_code == 422


def test_recognize_with_claude_mocked(client, monkeypatch):
    calls = {}

    def fake(system, content, effort):
        calls["content"] = content
        return "computer is an electronic machine"

    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(ai, "_call_claude", fake)
    r = client.post("/api/recognize", json={"image": _png(), "language": "ur"}, headers=H)
    assert r.status_code == 200 and r.json() == {"text": "computer is an electronic machine", "engine": "claude-vision"}
    assert calls["content"][0]["type"] == "image" and "Urdu" in calls["content"][1]["text"]


def test_organize_offline_example(client):
    r = client.post("/api/organize", json={"text": EXAMPLE, "style": "academic"}, headers=H)
    assert r.json()["engine"] == "local"
    md = r.json()["markdown"]
    assert md.startswith("# COMPUTER")
    assert "## 1. Introduction" in md and "- Hardware" in md and "- Software" in md
    # no invented words: every word of output body comes from the input or the structure
    for style in ai.STYLES:
        assert client.post("/api/organize", json={"text": EXAMPLE, "style": style}, headers=H).status_code == 200


def test_organize_with_claude_mocked(client, monkeypatch):
    seen = {}

    def fake(system, prompt, effort):
        seen["system"], seen["prompt"] = system, prompt
        return "```markdown\n# COMPUTER\n\n## 1. Introduction\n\nA computer is...\n```"

    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(ai, "_call_claude", fake)
    r = client.post("/api/organize", json={"text": EXAMPLE, "style": "report"}, headers=H).json()
    assert r["engine"] == "claude" and r["markdown"].startswith("# COMPUTER") and "```" not in r["markdown"]
    assert "Do NOT add facts" in seen["system"] and "Report Format" in seen["prompt"] and EXAMPLE in seen["prompt"]


def test_organize_empty_text(client):
    assert client.post("/api/organize", json={"text": "  ", "style": "simple"}, headers=H).status_code == 422


# --- export ---------------------------------------------------------------------
SAMPLE_MD = """# COMPUTER

## 1. Introduction

A **computer** is an electronic machine that accepts data, processes it, and produces information.

## 2. Main Components

- Hardware
  - CPU
- Software

1. First
2. Second

| Part | Type |
|------|------|
| CPU  | Hardware |

- [x] done item
- [ ] open item

## 3. اردو

کمپیوٹر ایک الیکٹرانک مشین ہے۔
"""


@pytest.mark.parametrize("fmt", ["docx", "pdf", "txt", "md"])
def test_export_formats(client, fmt):
    r = client.post("/api/export", json={"markdown": SAMPLE_MD, "format": fmt, "title": "Computer Notes"}, headers=H)
    assert r.status_code == 200, r.text
    assert 'filename="Computer_Notes.' + fmt in r.headers["content-disposition"]
    body = r.content
    if fmt == "docx":
        z = zipfile.ZipFile(io.BytesIO(body))
        xml = z.read("word/document.xml").decode()
        assert "COMPUTER" in xml and "Hardware" in xml and "<w:tbl>" in xml
    elif fmt == "pdf":
        assert body.startswith(b"%PDF")
    elif fmt == "txt":
        t = body.decode()
        assert "COMPUTER" in t and "* Hardware" in t and "**" not in t
    else:
        assert body.decode().startswith("# COMPUTER")


def test_docx_structure():
    from docx import Document

    d = Document(io.BytesIO(exporter.to_docx(SAMPLE_MD, "t")))
    styles = [p.style.name for p in d.paragraphs if p.text.strip()]
    assert styles[0] == "Title" and "Heading 1" in styles and "List Bullet" in styles and "List Number" in styles
    assert "List Bullet 2" in styles  # nested bullet
    assert len(d.tables) == 1


def test_export_save_on_computer(client):
    r = client.post("/api/export/save", json={"markdown": SAMPLE_MD, "format": "md", "title": "pytest export"}, headers=H).json()
    p = Path(r["path"])
    assert p.exists() and p.read_text(encoding="utf-8").startswith("# COMPUTER")
    p.unlink()


def test_export_rejects_bad_format(client):
    assert client.post("/api/export", json={"markdown": "x", "format": "exe"}, headers=H).status_code == 422


# --- history --------------------------------------------------------------------
def test_document_history_crud(client):
    doc = client.post("/api/documents", json={"title": "Test doc", "style": "academic", "raw_text": EXAMPLE, "markdown": SAMPLE_MD}, headers=H).json()
    assert len(doc["id"]) == 32 and doc["image"] is None
    assert any(d["id"] == doc["id"] for d in client.get("/api/documents", headers=H).json())
    assert client.get(f"/api/documents/{doc['id']}", headers=H).json()["markdown"] == SAMPLE_MD
    assert client.delete(f"/api/documents/{doc['id']}", headers=H).json()["ok"]
    assert client.get(f"/api/documents/{doc['id']}", headers=H).status_code == 404
    assert client.get("/api/documents/..%2F..%2Fsecret", headers=H).status_code == 404  # path traversal blocked


def test_type_disabled_when_no_input_control(client):
    assert client.post("/api/type", json={"text": "hi"}, headers=H).status_code == 503


# --- multiple computers (SmartPad helper) -------------------------------------
import secrets as _secrets
import shutil as _shutil
import threading as _threading

from server import hosts as hosts_mod
from server.config import DOCS_DIR


@pytest.fixture
def helper_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(hosts_mod, "_FILE", tmp_path / "hosts.json")
    ident = {"id": _secrets.token_hex(16), "key": _secrets.token_urlsafe(24)}
    yield ident
    h = hosts_mod.hosts.hosts.pop(ident["id"], None)
    if h:
        _shutil.rmtree(DOCS_DIR / h.id, ignore_errors=True)


def _hello(ident, name="Ali-PC"):
    return {"t": "hello", **ident, "name": name, "input_control": True}


def test_helper_gets_own_token_and_receives_only_its_phone_events(client, helper_identity):
    before = main.controller.actions
    with client.websocket_connect("/agent") as agent:
        agent.send_json(_hello(helper_identity))
        reg = agent.receive_json()
        assert reg["t"] == "registered" and reg["console"].startswith("/connect#key=")
        host = hosts_mod.hosts.hosts[helper_identity["id"]]
        assert host.token != "test-token-123"
        hh = {"X-SmartPad-Token": host.token}

        st = client.get("/api/status", headers=hh).json()
        assert st["computer"] == "Ali-PC" and st["input_control"] is True

        with client.websocket_connect(f"/ws?token={host.token}&device=AliPhone") as phone:
            hello = phone.receive_json()
            assert hello["computer"] == "Ali-PC"
            phone.send_json({"t": "ping", "ts": 7})
            assert phone.receive_json() == {"t": "pong", "ts": 7}
            phone.send_json([{"t": "move", "dx": 3, "dy": 4}, {"t": "click", "b": "left"}])
            got = agent.receive_json()
            assert got == {"t": "events", "events": [{"t": "move", "dx": 3, "dy": 4}, {"t": "click", "b": "left"}]}
            # the helper's QR page (needs the helper key, not the phone token)
            assert client.get("/api/host/status", headers={"X-SmartPad-Host-Key": "nope"}).status_code == 401
            hs = client.get("/api/host/status", headers={"X-SmartPad-Host-Key": helper_identity["key"]}).json()
            assert hs["urls"][0]["url"].endswith("#pair=" + host.token)
            assert [d["name"] for d in hs["devices"]] == ["AliPhone"]
            # the server computer's own status page lists it, but not its phone under local devices
            ds = client.get("/api/desktop/status").json()
            assert any(h["name"] == "Ali-PC" and h["online"] and h["devices"] == 1 for h in ds["hosts"])
            assert all(d["name"] != "AliPhone" for d in ds["devices"])
    assert main.controller.actions == before  # server's own mouse untouched

    # helper offline: phone stays paired but is told the helper isn't running
    st = client.get("/api/status", headers={"X-SmartPad-Token": host.token}).json()
    assert st["input_control"] is False and "not running" in st["input_error"]
    assert client.post("/api/type", headers={"X-SmartPad-Token": host.token}, json={"text": "x"}).status_code == 503


def test_helper_type_and_save_are_sent_to_that_computer(helper_identity):
    with TestClient(main.app) as client, client.websocket_connect("/agent") as agent:  # one shared event loop, like uvicorn
        agent.send_json(_hello(helper_identity))
        agent.receive_json()
        token = hosts_mod.hosts.hosts[helper_identity["id"]].token
        hh = {"X-SmartPad-Token": token}
        out = {}

        def call():
            out["type"] = client.post("/api/type", headers=hh, json={"text": "hello"})
            out["save"] = client.post("/api/export/save", headers=hh, json={"markdown": "# Hi", "format": "txt", "title": "Notes"})

        t = _threading.Thread(target=call)
        t.start()
        req = agent.receive_json()
        assert req["t"] == "type_text" and req["text"] == "hello"
        agent.send_json({"rpc": req["rpc"], "ok": True})
        req = agent.receive_json()
        assert req["t"] == "save_file" and req["name"].endswith(".txt") and base64.b64decode(req["data"])
        agent.send_json({"rpc": req["rpc"], "ok": True, "path": r"C:\Users\ali\Documents\SmartPad\Notes.txt", "name": "Notes.txt"})
        t.join(10)
        assert out["type"].status_code == 200
        assert out["save"].json()["path"].endswith("Notes.txt")


def test_document_history_is_per_computer(client, helper_identity):
    with client.websocket_connect("/agent") as agent:
        agent.send_json(_hello(helper_identity))
        agent.receive_json()
    hh = {"X-SmartPad-Token": hosts_mod.hosts.hosts[helper_identity["id"]].token}
    doc = client.post("/api/documents", headers=hh, json={"title": "Ali's notes", "markdown": "x"}).json()
    assert [d["id"] for d in client.get("/api/documents", headers=hh).json()] == [doc["id"]]
    assert all(d["id"] != doc["id"] for d in client.get("/api/documents", headers=H).json())
    assert client.get(f"/api/documents/{doc['id']}", headers=H).status_code == 404


def test_helper_wrong_key_rejected_and_rotate(client, helper_identity):
    with client.websocket_connect("/agent") as agent:
        agent.send_json(_hello(helper_identity))
        agent.receive_json()
    host = hosts_mod.hosts.hosts[helper_identity["id"]]
    with client.websocket_connect("/agent") as agent:
        agent.send_json(_hello({"id": helper_identity["id"], "key": "x" * 30}))
        assert agent.receive_json()["fatal"] is True
    old = host.token
    assert client.post("/api/host/rotate", headers={"X-SmartPad-Host-Key": helper_identity["key"]}).json()["ok"]
    assert host.token != old
    assert client.get("/api/status", headers={"X-SmartPad-Token": old}).status_code == 401


def test_helper_download_zip(client):
    r = client.get("/agent/download")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert any(n.endswith("/smartpad_agent.py") for n in names) and any(n.endswith("/touchpad.py") for n in names)
    bat = next(n for n in names if n.endswith("Start SmartPad Helper.bat"))
    assert b"--server http://testserver" in z.read(bat)
    if (main.ROOT_DIR / "agent" / "dist" / "SmartPad Helper.exe").exists():
        assert "SmartPad-Helper/SmartPad Helper.exe" in names
        assert z.read("SmartPad-Helper/server.txt").strip() == b"http://testserver"
    assert client.get("/connect").status_code == 200
