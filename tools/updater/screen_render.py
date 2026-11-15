"""
Dimension Hopper - draws the Controller tab's screen like the real controller's 128x128 display: its top bar, its
windows (a title tab with a cut corner over a gridded panel with a 1 px border), its 5 px pixel font and its texts
(screen_data.json, made by gen_screen_data.py from the controller firmware). The icons and animations are drawn here
(our own pixel art), not taken from the controller.
"""
from __future__ import annotations

import json
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 128
GREEN = (160, 250, 0)       # the controller theme's primary colour
GREEN_DARK = (0, 180, 0)    # secondary
BLACK = (0, 0, 0)           # tertiary (background)
GRID = (0, 46, 0)

# the controller's themes (ThemeService.cpp): primary, secondary, background
THEMES = [((160, 250, 0), (0, 180, 0), (0, 0, 0)), ((233, 0, 129), (92, 0, 215), (0, 0, 0)),
          ((225, 56, 0), (255, 174, 0), (42, 22, 0)), ((3, 0, 163), (255, 174, 0), (255, 255, 255)),
          ((0, 230, 190), (120, 255, 60), (0, 0, 0)), ((255, 214, 64), (214, 140, 0), (30, 18, 0)),
          ((120, 230, 255), (0, 140, 255), (0, 10, 40)), ((255, 255, 255), (150, 150, 150), (0, 0, 0)),
          ((200, 120, 255), (255, 80, 200), (16, 0, 32))]


def set_theme(n: int):
    """Draw with one of the controller's colour themes (0 Main, 1 Rose, 2 Red, 3 Blue, then the fork's Portal, Butter,
    Ice, Mono, Purple)."""
    global GREEN, GREEN_DARK, BLACK, GRID
    GREEN, GREEN_DARK, BLACK = THEMES[n if 0 <= n < len(THEMES) else 0]
    GRID = tuple(int(b + (p - b) * 0.18) for p, b in zip(GREEN, BLACK))


def _data() -> dict:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    try:
        return json.loads((base / "screen_data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"font": {"line_height": 7, "base_line": 1, "glyphs": {}}, "phrases": {}, "windows": {}, "listen": {}}


DATA = _data()
FONT = DATA["font"]
GLYPHS = FONT["glyphs"]


# --- text --------------------------------------------------------------------------------------------------------
def text_width(s: str) -> int:
    return sum(GLYPHS.get(c, GLYPHS.get("?", {"adv": 4}))["adv"] for c in s)


def draw_text(img: Image.Image, x: int, y: int, s: str, color):
    """y is the top of the line (line_height 7, like LVGL)."""
    px = img.load()
    lh, base = FONT["line_height"], FONT["base_line"]
    for c in s:
        g = GLYPHS.get(c) or GLYPHS.get(c.upper()) or GLYPHS.get("?")
        if g is None:
            x += 4
            continue
        top = y + lh - base - g["h"] - g["y"]
        for r, row in enumerate(g["rows"]):
            for col, bit in enumerate(row):
                if bit == "1":
                    xx, yy = x + g["x"] + col, top + r
                    if 0 <= xx < SIZE and 0 <= yy < SIZE:
                        px[xx, yy] = color
        x += g["adv"]


def wrap(s: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in s.split():
        trial = f"{line} {word}".strip()
        if text_width(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


# --- what's on screen --------------------------------------------------------------------------------------------
@dataclass
class Screen:
    title: str = ""                 # window title; "" = home screen
    lines: list[str] = field(default_factory=list)   # body text (wrapped here)
    icon: str = ""                  # dice, battery_low, battery_charging, battery_full, dance, portal, fall, pickup,
                                    # upside_down, shake, clock, eight_ball, listen, chat, status
    big: str = ""                   # a big number / word under the icon (dice result, time)
    width: int = 90                 # text width, like the controller's labels (80-90 px)


def phrase(name: str, i: int) -> str:
    lst = DATA["phrases"].get(name, [])
    return lst[i] if 0 <= i < len(lst) else ""


def window_title(action: str) -> str:
    return DATA["windows"].get(f"{action}Window", {}).get("title", "")


def window_sentence(action: str, key: str = "Sentence") -> str:
    return DATA["windows"].get(f"{action}Window", {}).get("sentences", {}).get(key, "")


PHRASE_LIST = {"Fact": "FactPhrases", "Joke": "JokePhrases", "PassTheButter": "PassTheButterPhrases",
               "Profanity": "ProfanityPhrases", "YouPassButter": "YouPassButterPhrases", "Ramble": "Ramble"}
ICON_FOR = {"Fall": "fall", "PickUp": "pickup", "UpsideDown": "upside_down", "Shake": "shake", "Dance": "dance",
            "OpenPortal": "portal", "BatteryLow": "battery_low", "Charging": "battery_charging",
            "ChargingFull": "battery_full", "CurrentTime": "clock", "EightBall": "eight_ball"}
DICE = ["D4", "D6", "D8", "D10", "D12", "D20", "D100", ""]


def screen_for(state: str, action: str, d: bytes, chat_replies: list[str] | None = None) -> Screen | None:
    """The window the real controller would show for a robot message (None: no window, e.g. Idle::None)."""
    b0 = d[0] if d else 0
    if state == "Listen":
        phase = ["Listening", "NotUnderstand", "Abort", "Heard", "Thinking"]
        key = phase[b0] if b0 < len(phase) else ""
        return Screen("Listen", [DATA["listen"].get(key, "...")], icon="listen")
    if state == "Idle" and action == "None":
        return None
    title = window_title(action) or action
    if action in PHRASE_LIST:
        return Screen(title, [phrase(PHRASE_LIST[action], b0)])
    if action == "Wander":
        import random
        return Screen(title, [random.choice(DATA["phrases"].get("Ramble") or ["..."])])
    if action == "Poke" and len(d) >= 2:
        return Screen(title, [phrase(f"PokeLvl{min(d[0], 2) + 1}Phrases", d[1])])
    if action == "Person" and len(d) >= 2:
        return Screen(title, [phrase("PersonOwnerGreetingPhrases" if d[1] else "PersonStrangerGreetingPhrases", d[0])
                              or ("Welcome back" if d[1] else "Hello, stranger")])
    if action == "EightBall":
        resp = d[1] if len(d) > 1 else 0
        if b0 == 2:
            return Screen(title, [phrase("EightBall", resp)], icon="eight_ball")
        if b0 == 1:
            return Screen(title, [window_sentence("EightBall", "ThinkingSentence") or "Thinking"], icon="eight_ball")
        return Screen(title, [phrase("EightBallListeningPhrases", 0) or "Ask your question"], icon="eight_ball")
    if action == "DiceRoll" and len(d) >= 3:
        phase, kind, count = d[0], d[1], d[2]
        result = struct.unpack_from("<H", d, 4)[0] if len(d) >= 6 else 0
        name = DICE[kind] if kind < len(DICE) else ""
        if phase == 3:
            return Screen(title, [f"{count}x {name}" if count > 1 else name], icon="dice", big=str(result))
        if phase == 2:
            return Screen(title, ["Rolling..."], icon="dice", big="?")
        return Screen(title, ["How many dice?" if phase == 0 else "Which dice?"], icon="dice")
    if action == "CurrentTime":
        if b0 == 1 and len(d) >= 16:
            sec, minute, hour = struct.unpack_from("<iii", d, 4)
            return Screen(title, [], icon="clock", big=f"{hour:02d}:{minute:02d}")
        return Screen(title, ["No idea what time it is"], icon="clock")
    if action in ("Charging", "BatteryLow"):
        text = phrase("BatteryChargingPhrases" if action == "Charging" else "BatteryLowPhrases", b0)
        return Screen(title, [text] if text else [], icon=ICON_FOR[action])
    if action == "Chat":
        reply = (chat_replies or [])[b0] if chat_replies and b0 < len(chat_replies) else ""
        return Screen(window_title("Chat") or "Butter Bot", [reply or "..."])
    if action == "Subtitle":
        from controller_link import Message
        s = Message("Scenario", "Subtitle", d).strings() + ["", ""]
        return Screen("Butter Bot", ([f"You: {s[0]}"] if s[0] else []) + [s[1]], width=100)
    if action == "Dance":
        return Screen(title, ["Rhythm.exe unleashed"], icon="dance")
    if action == "OpenPortal":
        return Screen(window_title("Portal") or "Portal", [], icon="portal")
    if action in ICON_FOR:
        return Screen(title, [window_sentence(action)] if window_sentence(action) else [], icon=ICON_FOR[action])
    sentence = window_sentence(action)
    return Screen(title, [sentence] if sentence else [])


# --- drawing -----------------------------------------------------------------------------------------------------
def _grid(img, box):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    for x in range(x0 + 3, x1, 6):
        d.line((x, y0, x, y1), fill=GRID)
    for y in range(y0 + 3, y1, 6):
        d.line((x0, y, x1, y), fill=GRID)


def draw_top_bar(img, robot_batt: int | None, linked: bool, ctrl_batt: int = 100):
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, SIZE - 1, 8), fill=GREEN)

    def battery(x, level):
        d.rectangle((x, 2, x + 11, 6), outline=BLACK)
        d.rectangle((x + 12, 3, x + 12, 5), fill=BLACK)
        if level is not None:
            bars = max(0, min(4, round(level / 25)))
            for i in range(bars):
                d.rectangle((x + 2 + i * 2 + i // 2, 4, x + 3 + i * 2 + i // 2, 4), fill=BLACK)
    battery(2, robot_batt)
    d.rectangle((17, 2, 20, 6), outline=BLACK)  # a tiny robot head next to its battery
    d.point((19, 4), fill=BLACK)
    if linked:  # link "waves"
        for r in (1, 3, 5):
            d.arc((64 - r, 6 - r, 64 + r, 6 + r), 200, 340, fill=BLACK)
    battery(SIZE - 22, ctrl_batt)
    d.rectangle((SIZE - 8, 2, SIZE - 5, 6), outline=BLACK)  # controller


def draw_home(img, hints: list[str]):
    y = 36
    for line in hints:
        draw_text(img, (SIZE - text_width(line)) // 2, y, line, GREEN)
        y += 15


def draw_icon(img, icon: str, cx: int, cy: int, t: float):
    """Our own pixel art, about 32 px, centred on (cx, cy). t = seconds since the window opened (animations)."""
    d = ImageDraw.Draw(img)
    g, dk = GREEN, GREEN_DARK
    if icon == "dice":
        spin = int(t * 8) % 4 if t < 1.2 else 0
        o = [0, 2, 0, -2][spin]
        d.rounded_rectangle((cx - 12 + o, cy - 12 - o, cx + 12 + o, cy + 12 - o), radius=4, outline=g, width=2)
        for px, py in ((-6, -6), (6, 6), (0, 0), (-6, 6), (6, -6)):
            d.rectangle((cx + px - 1 + o, cy + py - 1 - o, cx + px + 1 + o, cy + py + 1 - o), fill=dk)
    elif icon.startswith("battery"):
        d.rectangle((cx - 16, cy - 8, cx + 13, cy + 8), outline=g, width=2)
        d.rectangle((cx + 14, cy - 3, cx + 16, cy + 3), fill=g)
        if icon == "battery_low":
            if int(t * 2) % 2 == 0:
                d.rectangle((cx - 13, cy - 5, cx - 9, cy + 5), fill=(225, 56, 0))
        else:
            n = 4 if icon == "battery_full" else (int(t * 2) % 4) + 1
            for i in range(n):
                d.rectangle((cx - 13 + i * 6, cy - 5, cx - 9 + i * 6, cy + 5), fill=g)
    elif icon == "dance":
        for i in range(3):
            ph = t * 4 + i * 2.1
            x, y = cx - 14 + i * 13, cy + int(math.sin(ph) * 5)
            d.ellipse((x - 3, y + 3, x + 2, y + 7), fill=g)
            d.line((x + 2, y + 5, x + 2, y - 8), fill=g)
            d.line((x + 2, y - 8, x + 6, y - 6), fill=g)
    elif icon == "portal":
        for i in range(24):
            a = t * 3 + i * 0.5
            r = 4 + i * 0.6
            x, y = cx + math.cos(a) * r, cy + math.sin(a) * r * 0.8
            d.rectangle((x - 1, y - 1, x + 1, y + 1), fill=g if i % 3 else dk)
    elif icon in ("fall", "pickup", "upside_down", "shake"):
        flip = icon == "upside_down"
        sx = int(math.sin(t * 25) * 3) if icon == "shake" else 0
        lift = -int(abs(math.sin(t * 3)) * 6) if icon == "pickup" else 0
        tilt = 6 if icon == "fall" else 0
        x0, y0 = cx - 9 + sx + tilt, cy - 10 + lift
        body = [(x0, y0), (x0 + 18, y0), (x0 + 18 - tilt // 2, y0 + 16), (x0 - tilt // 2, y0 + 16)]
        if flip:
            body = [(x, 2 * cy - y) for x, y in body]
        d.polygon(body, outline=g)
        ey = (y0 + 6) if not flip else (2 * cy - (y0 + 6))
        d.ellipse((x0 + 5, ey - 3, x0 + 11, ey + 3), outline=g)
        d.point((x0 + 8, ey), fill=g)
        if icon == "pickup":
            d.line((cx, cy - 22 + lift, cx, cy - 12 + lift), fill=dk)
    elif icon == "clock":
        d.ellipse((cx - 13, cy - 13, cx + 13, cy + 13), outline=g, width=2)
        a = t * 2
        d.line((cx, cy, cx + math.cos(a) * 9, cy + math.sin(a) * 9), fill=g)
        d.line((cx, cy, cx, cy - 6), fill=g)
    elif icon == "eight_ball":
        d.ellipse((cx - 13, cy - 13, cx + 13, cy + 13), outline=g, width=2)
        d.ellipse((cx - 6, cy - 7, cx + 6, cy + 5), fill=g)
        draw_text(img, cx - 2, cy - 5, "8", BLACK)
    elif icon == "listen":
        for i in range(5):
            h = 3 + int(abs(math.sin(t * 6 + i)) * 8)
            d.rectangle((cx - 12 + i * 6, cy - h, cx - 9 + i * 6, cy + h), fill=g)


def draw_window(img, s: Screen, t: float):
    """A controller window: title tab with a cut corner, gridded panel, 1 px border, wrapped text, optional icon."""
    lh, gap = FONT["line_height"], 3
    lines = [w for line in s.lines if line for w in wrap(line, s.width)][:8]
    text_w = max([text_width(line) for line in lines] + [0])
    icon_h = 34 if s.icon else 0
    big_h = 11 if s.big else 0
    inner_w = max(text_w + 6, 40 if s.icon else 0, text_width(s.title) + 10)
    inner_h = icon_h + big_h + (len(lines) * (lh + gap) + 3 if lines else 2)
    w, h = inner_w + 2, inner_h + 2 + 6
    # like the controller: top left, just under the top bar
    x0 = 4 if w <= SIZE - 8 else max(0, (SIZE - w) // 2)
    y0 = 12
    d = ImageDraw.Draw(img)
    d.rectangle((x0, y0, x0 + w - 1, y0 + h - 1), fill=BLACK)
    _grid(img, (x0, y0 + 6, x0 + w - 1, y0 + h - 1))
    # title tab, with the cut corner at its right end
    tw = text_width(s.title) + 8
    d.rectangle((x0, y0, x0 + tw - 1, y0 + 5), fill=GREEN)
    d.polygon([(x0 + tw - 6, y0), (x0 + tw, y0), (x0 + tw, y0 + 6)], fill=BLACK)
    draw_text(img, x0 + 2, y0 - 1, s.title, BLACK)
    # inner panel border
    d.rectangle((x0, y0 + 6, x0 + w - 1, y0 + h - 1), outline=GREEN)
    y = y0 + 8
    if s.icon:
        draw_icon(img, s.icon, x0 + w // 2, y + 16, t)
        y += icon_h
    if s.big:
        bw = text_width(s.big)
        # double-size big text: draw small and scale up in place
        tmp = Image.new("RGB", (bw + 2, lh + 1), BLACK)
        draw_text(tmp, 1, 0, s.big, GREEN)
        tmp = tmp.resize((tmp.width * 2, tmp.height * 2), Image.NEAREST)
        img.paste(tmp, (x0 + (w - tmp.width) // 2, y - 2))
        y += big_h + 6
    for line in lines:  # left-aligned, like the controller's labels
        draw_text(img, x0 + 3 if not s.icon else x0 + (w - text_width(line)) // 2, y, line, GREEN)
        y += lh + gap


def render(screen: Screen | None, home_hints: list[str], robot_batt: int | None, linked: bool, t: float) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BLACK)
    draw_top_bar(img, robot_batt, linked)
    if screen is None:
        draw_home(img, home_hints)
    else:
        draw_window(img, screen, t)
    return img
