"""
Dimension Hopper - the joystick: a black stick drawn on a canvas, dragged with the mouse or pushed with WASD / arrow
keys. Used by the Controller tab (on the drawn board) and the Diagnostics tab (driving over USB).
"""
from __future__ import annotations

import math
import tkinter as tk
from typing import Callable

KEYS = {"w": (0, 1), "up": (0, 1), "s": (0, -1), "down": (0, -1), "a": (-1, 0), "left": (-1, 0),
        "d": (1, 0), "right": (1, 0)}


def mix(x: float, y: float, max_speed: float = 100.0) -> tuple[int, int]:
    """Stick (-100..100 each way) to wheel speeds, like the robot's RC mode: speed follows how far the stick is pushed,
    turning blends in, and any real push starts at 30 (below that the motors stall)."""
    if abs(x) < 1 and abs(y) < 1:
        return 0, 0
    turn = 1.0 if y >= 0 else -1.0
    left, right = y + 0.5 * turn * x, y - 0.5 * turn * x
    peak = max(abs(left), abs(right))
    deflection = min(1.0, math.hypot(x, y) / 100.0)
    target = 30 + (max(30.0, max_speed) - 30) * deflection
    return int(left / peak * target), int(right / peak * target)


class Stick:
    """The stick, drawn on `cv` around (cx, cy) in pixels. on_change(x, y) gets -100..100 each way (y up = forward)."""

    def __init__(self, cv: tk.Canvas, cx: float, cy: float, base_r: float, knob_r: float,
                 on_change: Callable[[int, int], None]):
        self.cv, self.cx, self.cy, self.knob_r = cv, cx, cy, knob_r
        self.reach = base_r - knob_r * 0.6
        self.on_change = on_change
        self.value = (0, 0)
        self.dragging = False
        self.keys: set[str] = set()

        def oval(x, y, r, **kw):
            return cv.create_oval(x - r, y - r, x + r, y + r, **kw)
        oval(cx, cy, base_r, fill="#161819", outline="#34383c", width=2)
        oval(cx, cy, base_r * 0.75, fill="#202326", outline="")
        self.knob = oval(cx, cy, knob_r, fill="#2a2d31", outline="#4a4f55", width=2)
        self.hi = oval(cx - knob_r * 0.25, cy - knob_r * 0.28, knob_r * 0.28, fill="#5a5f66", outline="")
        for item in (self.knob, self.hi):
            cv.tag_bind(item, "<ButtonPress-1>", self._grab)
        cv.bind("<B1-Motion>", self._drag, add="+")
        cv.bind("<ButtonRelease-1>", self._drop, add="+")

    # --- mouse ---------------------------------------------------------------------------------------------
    def _grab(self, e):
        self.dragging = True
        self._drag(e)

    def _drag(self, e):
        if not self.dragging:
            return
        dx, dy = e.x - self.cx, e.y - self.cy
        d = math.hypot(dx, dy)
        if d > self.reach:
            dx, dy = dx * self.reach / d, dy * self.reach / d
        self.set(int(dx / self.reach * 100), int(-dy / self.reach * 100))

    def _drop(self, _e):
        if self.dragging:
            self.dragging = False
            self.set(0, 0)

    # --- keys (the tab passes its key events in) -----------------------------------------------------------
    def key(self, keysym: str, down: bool) -> bool:
        k = keysym.lower()
        if k not in KEYS:
            return False
        (self.keys.add if down else self.keys.discard)(k)
        x = max(-1, min(1, sum(KEYS[k][0] for k in self.keys)))
        y = max(-1, min(1, sum(KEYS[k][1] for k in self.keys)))
        self.set(x * 80, y * 80)
        return True

    # --- position ------------------------------------------------------------------------------------------
    def set(self, x: int, y: int):
        x, y = max(-100, min(100, x)), max(-100, min(100, y))
        ox, oy = x / 100 * self.reach, -y / 100 * self.reach
        r, hr = self.knob_r, self.knob_r * 0.28
        hx, hy = self.cx - self.knob_r * 0.25 + ox, self.cy - self.knob_r * 0.28 + oy
        self.cv.coords(self.knob, self.cx - r + ox, self.cy - r + oy, self.cx + r + ox, self.cy + r + oy)
        self.cv.coords(self.hi, hx - hr, hy - hr, hx + hr, hy + hr)
        if (x, y) != self.value:
            self.value = (x, y)
            self.on_change(x, y)

    def release(self):
        self.keys.clear()
        self.dragging = False
        self.set(0, 0)
