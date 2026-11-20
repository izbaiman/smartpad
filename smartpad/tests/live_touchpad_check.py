"""Live check against a running server: moves the REAL mouse a little and back, measures round-trip latency.

    python run.py            (in one terminal)
    python tests/live_touchpad_check.py
Only moves are sent — no clicks — so it is safe to run on your desktop.
"""
import asyncio
import json
import statistics
import time
import urllib.request

import websockets
from pynput.mouse import Controller

BASE = "http://127.0.0.1:8765"


async def main():
    status = json.load(urllib.request.urlopen(f"{BASE}/api/desktop/status"))
    token = status["urls"][0]["url"].split("#pair=")[1]
    mouse = Controller()
    start = mouse.position
    async with websockets.connect(f"ws://127.0.0.1:8765/ws?token={token}&device=LiveCheck") as ws:
        hello = json.loads(await ws.recv())
        assert hello["t"] == "hello", hello
        print("connected, input control:", hello["input_control"])

        rtts = []
        for i in range(20):
            t0 = time.perf_counter()
            await ws.send(json.dumps({"t": "ping", "ts": i}))
            while json.loads(await ws.recv()).get("t") != "pong":
                pass
            rtts.append((time.perf_counter() - t0) * 1000)
        print(f"round-trip latency: median {statistics.median(rtts):.2f} ms, max {max(rtts):.2f} ms")

        for _ in range(10):  # 10 x 4px = 40px right, 20px down (sub-pixel accumulation exercised)
            await ws.send(json.dumps([{"t": "move", "dx": 4, "dy": 2}]))
        await ws.send(json.dumps({"t": "ping", "ts": 0}))
        await ws.recv()
        moved = mouse.position
        await ws.send(json.dumps({"t": "move", "dx": -40, "dy": -20}))
        await ws.send(json.dumps({"t": "ping", "ts": 0}))
        await ws.recv()
        back = mouse.position
    print("start", start, "-> moved", moved, "-> back", back)
    dx, dy = moved[0] - start[0], moved[1] - start[1]
    assert dx > 0 and dy > 0, "cursor did not move"
    print(f"OK: cursor moved by ({dx}, {dy}) and returned to {back}")


asyncio.run(main())
