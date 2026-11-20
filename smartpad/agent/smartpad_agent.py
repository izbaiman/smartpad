"""SmartPad helper: lets a phone control THIS computer through a SmartPad server on the network.

A web page can't move the mouse of the computer it's opened on, so each computer that wants its
own phone touchpad runs this small helper. It connects to the SmartPad server, gets a pairing QR
code for this computer, and applies the touchpad events from phones paired with that code.

    python smartpad_agent.py --server http://192.168.1.20:8765
    options: --name "Ali's laptop"   --no-browser   --no-input (test: never move the mouse)

Without --server it reads server.txt next to itself (the download includes it), or asks.
Windows users get this as "SmartPad Helper.exe" (built with build_helper_exe.bat), no Python needed.
Needs (when run as a script): pip install pynput websockets
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import secrets
import socket
import ssl
import sys
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # running as the PyInstaller .exe
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency. Run:  pip install pynput websockets")

from touchpad import InputController  # shipped next to this file (same code as the server uses)

log = logging.getLogger("smartpad.helper")
ID_FILE = HERE / "smartpad_helper_id.json"


def identity() -> dict:
    """Random id + secret key, kept next to the helper so this computer keeps its pairing code."""
    try:
        data = json.loads(ID_FILE.read_text(encoding="utf-8"))
        if len(data.get("id", "")) == 32 and len(data.get("key", "")) >= 20:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    data = {"id": secrets.token_hex(16), "key": secrets.token_urlsafe(24)}
    ID_FILE.write_text(json.dumps(data), encoding="utf-8")
    return data


def export_dir() -> Path:
    docs = Path.home() / "Documents"
    d = (docs if docs.is_dir() else Path.home()) / "SmartPad"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_file(name: str, data: bytes) -> Path:
    safe = "".join(c for c in Path(name).name if c not in '<>:"/\\|?*').strip() or "SmartPad.txt"
    target = export_dir() / safe
    stem, n = target.stem, 1
    while target.exists():
        target = target.with_name(f"{stem}_{n}{target.suffix}")
        n += 1
    target.write_bytes(data)
    return target


class Helper:
    def __init__(self, server: str, name: str, open_browser: bool, input_control: bool) -> None:
        server = server.rstrip("/")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        self.http = server
        self.ws_url = ("wss://" + server[8:] if server.startswith("https://") else "ws://" + server[7:]) + "/agent"
        self.name = name
        self.open_browser = open_browser
        self.ident = identity()
        self.controller = InputController(enabled=input_control)
        self.browser_opened = False
        self.applied = self.errors = 0
        self.last_error: str | None = None

    def ssl_ctx(self):
        if not self.ws_url.startswith("wss://"):
            return None
        # SmartPad's HTTPS uses a self-signed certificate on the local network.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.ws_url, ssl=self.ssl_ctx(), max_size=64 * 1024 * 1024, ping_interval=20) as ws:
                    backoff = 1
                    await self.session(ws)
                    self.check_replaced(ws.close_code)
            except SystemExit:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                self.check_replaced(exc.rcvd.code if exc.rcvd else None)
                log.warning("Connection closed (%s). Reconnecting in %ss...", exc, backoff)
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("Server not reachable (%s). Retrying in %ss...", exc, backoff)
            finally:
                self.release_buttons()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    def apply(self, ev: dict) -> None:
        self.applied += 1
        reply = self.controller.handle(ev)
        if reply and reply.get("t") == "error":
            self.errors += 1
            if reply.get("detail") != self.last_error:
                print("Mouse/keyboard error:", reply.get("detail"))
            self.last_error = reply.get("detail")

    def diag(self) -> dict:
        """Sent to the server every few seconds so problems on this computer can be seen there."""
        pos = None
        try:
            if self.controller.enabled:
                pos = list(self.controller._mouse.position)
        except Exception as exc:
            pos = f"error: {exc}"
        d = {"applied": self.applied, "errors": self.errors, "last_error": self.last_error, "cursor": pos,
             "platform": platform.platform(), "frozen": FROZEN}
        if os.name == "nt":
            try:
                import ctypes
                d["admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
                d["foreground"] = self._foreground_title()
                sid = ctypes.c_ulong()
                ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
                d["session"] = sid.value
                d["console_session"] = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
            except Exception as exc:
                d["win_error"] = str(exc)
        return d

    @staticmethod
    def _foreground_title() -> str:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(200)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 200)
        return buf.value

    async def report(self, ws) -> None:
        while True:
            await asyncio.sleep(3)
            await ws.send(json.dumps({"t": "state", "input_control": self.controller.enabled,
                                      "input_error": self.controller.error, "diag": self.diag()}))

    async def session(self, ws) -> None:
        await ws.send(json.dumps({
            "t": "hello", **self.ident, "name": self.name,
            "input_control": self.controller.enabled, "input_error": self.controller.error,
            "platform": platform.system(),
        }))
        reporter = asyncio.create_task(self.report(ws))
        try:
            await self.receive(ws)
        finally:
            reporter.cancel()

    async def receive(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("t")
            if t == "registered":
                console = self.http + msg["console"]
                print("\n" + "=" * 64)
                print(f"  Connected to SmartPad as \"{msg.get('name')}\"")
                print("  Open this page on THIS computer to see your phone QR code:")
                print(f"    {console}")
                print("  Keep this window open while you use the phone. Ctrl+C to stop.")
                print("=" * 64 + "\n")
                if self.open_browser and not self.browser_opened:
                    self.browser_opened = True
                    webbrowser.open(console)
            elif t == "events":
                for ev in msg.get("events", [])[:200]:
                    if isinstance(ev, dict) and ev.get("t") != "ping":
                        self.apply(ev)
            elif t == "type_text":
                reply = await asyncio.to_thread(self.controller.handle, {"t": "type", "text": str(msg.get("text", ""))})
                if not self.controller.enabled:
                    out = {"ok": False, "detail": self.controller.error or "Mouse/keyboard control is off on this computer."}
                elif reply and reply.get("t") == "error":
                    out = {"ok": False, "detail": reply.get("detail")}
                else:
                    out = {"ok": True}
                await ws.send(json.dumps({"rpc": msg.get("rpc"), **out}))
            elif t == "save_file":
                try:
                    path = await asyncio.to_thread(save_file, str(msg.get("name", "")), base64.b64decode(msg.get("data", "")))
                    out = {"ok": True, "path": str(path), "name": path.name}
                except Exception as exc:
                    out = {"ok": False, "detail": f"Could not save on {self.name}: {exc}"}
                await ws.send(json.dumps({"rpc": msg.get("rpc"), **out}))
            elif t == "error":
                print("Server error:", msg.get("detail"))
                if msg.get("fatal"):
                    raise SystemExit(1)

    @staticmethod
    def check_replaced(code) -> None:
        if code == 4002:
            raise SystemExit("A newer SmartPad Helper for this computer was started, so this one is closing.")

    def release_buttons(self) -> None:
        """If the connection drops in the middle of a drag, don't leave the mouse button stuck."""
        if self.controller.enabled:
            for b in ("left", "right"):
                try:
                    self.controller.handle({"t": "up", "b": b})
                except Exception:
                    pass


def find_server(arg: str | None) -> str:
    if arg:
        return arg
    try:
        line = (HERE / "server.txt").read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if line:
            return line
    except (OSError, IndexError):
        pass
    addr = input("SmartPad server address (e.g. 172.16.1.81:8765): ").strip()
    if not addr:
        raise SystemExit("No server address given.")
    if ":" not in addr.split("//")[-1]:
        addr += ":8765"
    try:
        (HERE / "server.txt").write_text(addr + "\n", encoding="utf-8")
    except OSError:
        pass
    return addr


def main() -> None:
    parser = argparse.ArgumentParser(description="SmartPad helper for this computer")
    parser.add_argument("--server", help="SmartPad server address, e.g. http://192.168.1.20:8765 (default: server.txt)")
    parser.add_argument("--name", default=os.environ.get("SMARTPAD_COMPUTER_NAME") or socket.gethostname(), help="name shown on the phone")
    parser.add_argument("--no-browser", action="store_true", help="don't open the QR code page")
    parser.add_argument("--no-input", action="store_true", help="never move the real mouse (testing)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    helper = Helper(find_server(args.server), args.name, not args.no_browser, not args.no_input)
    print(f"SmartPad helper for \"{args.name}\" connecting to {helper.http} ...")
    if helper.controller.error:
        print("Warning:", helper.controller.error)
    try:
        asyncio.run(helper.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        # Double-clicked .exe: keep the window open so the message can be read.
        if FROZEN and exc.code not in (0, None):
            print(exc.code if isinstance(exc.code, str) else "")
            try:
                input("Press Enter to close...")
            except EOFError:
                pass
        raise
