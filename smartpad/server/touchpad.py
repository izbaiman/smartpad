"""Translates touchpad events from the phone into real mouse/keyboard actions.

Protocol (JSON over WebSocket, sent by the phone):
  {"t": "move",   "dx": float, "dy": float}          relative cursor move (already scaled)
  {"t": "scroll", "dx": float, "dy": float}          scroll in "lines"; fractional amounts accumulate
  {"t": "click",  "b": "left"|"right"|"middle", "n": 1|2}
  {"t": "down",   "b": "left"}  /  {"t": "up", "b": "left"}   for drag-and-drop
  {"t": "type",   "text": str}                        type text on the computer
  {"t": "key",    "k": "enter"|"backspace"|"tab"|"esc"|"left"|"right"|"up"|"down"}
  {"t": "ping",   "ts": number}                       -> server replies {"t": "pong", "ts": ...}
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("smartpad.touchpad")

_MAX_STEP = 400  # clamp a single move to avoid wild jumps from bad packets


class InputController:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._mouse = None
        self._kb = None
        self._Button = None
        self._Key = None
        self._scroll_acc_x = 0.0
        self._scroll_acc_y = 0.0
        self._move_acc_x = 0.0
        self._move_acc_y = 0.0
        self.error: str | None = None
        self.actions = 0  # counter, useful for tests/status
        if enabled:
            try:
                from pynput.keyboard import Controller as KController, Key
                from pynput.mouse import Button, Controller as MController

                self._mouse = MController()
                self._kb = KController()
                self._Button = Button
                self._Key = Key
            except Exception as exc:  # pragma: no cover - depends on OS/session
                self.error = f"Mouse control unavailable: {exc}"
                self.enabled = False
                log.warning(self.error)

    # ---- helpers -------------------------------------------------------
    def _button(self, name: str):
        return {
            "left": self._Button.left,
            "right": self._Button.right,
            "middle": self._Button.middle,
        }.get(name, self._Button.left)

    @staticmethod
    def _num(v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if f != f:  # NaN
            return 0.0
        return max(-_MAX_STEP, min(_MAX_STEP, f))

    # ---- main entry ----------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        """Apply one event. Returns an optional reply for the phone."""
        t = msg.get("t")
        if t == "ping":
            return {"t": "pong", "ts": msg.get("ts")}
        if not self.enabled:
            # Still count so the data flow can be verified without moving the real mouse.
            self.actions += 1
            return None
        try:
            if t == "move":
                # keep sub-pixel remainders so slow finger motion is not lost
                self._move_acc_x += self._num(msg.get("dx"))
                self._move_acc_y += self._num(msg.get("dy"))
                ix, iy = int(self._move_acc_x), int(self._move_acc_y)
                if ix or iy:
                    self._move_acc_x -= ix
                    self._move_acc_y -= iy
                    self._mouse.move(ix, iy)
            elif t == "scroll":
                self._scroll_acc_x += self._num(msg.get("dx"))
                self._scroll_acc_y += self._num(msg.get("dy"))
                ix, iy = int(self._scroll_acc_x), int(self._scroll_acc_y)
                if ix or iy:
                    self._scroll_acc_x -= ix
                    self._scroll_acc_y -= iy
                    self._mouse.scroll(ix, iy)
            elif t == "click":
                n = 2 if msg.get("n") == 2 else 1
                self._mouse.click(self._button(msg.get("b", "left")), n)
            elif t == "down":
                self._mouse.press(self._button(msg.get("b", "left")))
            elif t == "up":
                self._mouse.release(self._button(msg.get("b", "left")))
            elif t == "type":
                text = str(msg.get("text", ""))[:5000]
                if text:
                    self._kb.type(text)
            elif t == "key":
                key = {
                    "enter": self._Key.enter,
                    "backspace": self._Key.backspace,
                    "tab": self._Key.tab,
                    "esc": self._Key.esc,
                    "left": self._Key.left,
                    "right": self._Key.right,
                    "up": self._Key.up,
                    "down": self._Key.down,
                    "space": self._Key.space,
                }.get(str(msg.get("k")))
                if key is not None:
                    self._kb.press(key)
                    self._kb.release(key)
            else:
                return {"t": "error", "detail": f"unknown event {t!r}"}
            self.actions += 1
        except Exception as exc:  # pragma: no cover - OS level failures
            log.exception("input action failed")
            return {"t": "error", "detail": str(exc)}
        return None
