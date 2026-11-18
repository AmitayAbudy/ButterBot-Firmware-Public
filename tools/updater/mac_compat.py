"""
Makes the Tk UI, written and tuned on Windows, look and behave the same on a Mac. install() is called once, before
the first window; on Windows it does nothing.

- Fonts: the UI names Windows fonts (Segoe UI, Segoe UI Black/Semibold, Segoe Print, Consolas, Cascadia Mono).
  They're swapped for the nearest Mac fonts wherever a font is set (widgets, canvas text, ttk styles, tree tags).
- Sizes: Tk on Windows draws 1 pt as 1.33 px (96 dpi), on a Mac as 1 px (and ignores "tk scaling" for fonts).
  The layout (wrap lengths, canvas drawings) is in pixels, so the mapped fonts get Windows' pixel size and every
  label stays the size it was designed at.
- Buttons: macOS draws tk.Button natively and ignores its colours (light grey buttons, unreadable lime text on
  them). tk.Button becomes a label that acts like a button, so the dark portal buttons look the same.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

IS_MAC = sys.platform == "darwin"

# Windows family -> (Mac family, bold)
FONT_MAP = {
    "Segoe UI": ("Helvetica Neue", False),
    "Segoe UI Semibold": ("Helvetica Neue", True),
    "Segoe UI Black": ("Helvetica Neue", True),
    "Segoe Print": ("Chalkboard SE", False),
    "Consolas": ("Menlo", False),
    "Cascadia Mono": ("Menlo", False),
}
WINDOWS_SCALING = 96 / 72  # Tk's pixels per point on a standard Windows screen

_installed = False


def mac_font(font):
    """The Mac equivalent of a Windows font tuple like ("Segoe UI Black", 12, "italic"); anything else unchanged."""
    if not isinstance(font, (tuple, list)) or not font or not isinstance(font[0], str):
        return font
    mapped = FONT_MAP.get(font[0])
    if mapped is None:
        return font
    family, bold = mapped
    styles = [s for s in font[2:] if isinstance(s, str)]
    if bold and "bold" not in styles:
        styles.insert(0, "bold")
    size = font[1] if len(font) > 1 else 9
    if isinstance(size, int) and size > 0:  # points as Windows draws them (negative sizes are pixels already)
        size = round(size * WINDOWS_SCALING)
    return (family, size, *styles)


def _fix_fonts(cnf):
    if isinstance(cnf, dict) and "font" in cnf:
        new = mac_font(cnf["font"])
        if new is not cnf["font"]:
            cnf = dict(cnf, font=new)
    return cnf


def wheel_steps(event) -> int:
    """How many units to scroll for a <MouseWheel> event: Windows sends multiples of 120 per notch, macOS small
    numbers (trackpads several per gesture)."""
    if IS_MAC:
        return -int(event.delta) if event.delta else 0
    return int(-event.delta / 120)


class MacButton(tk.Label):
    """tk.Button's look on a Mac: a Label that takes the button's options and clicks like one (pressed colours
    while held, the command on release over the button, nothing while disabled)."""

    _BUTTON_ONLY = ("command", "overrelief", "default", "repeatdelay", "repeatinterval")
    _bound = False

    def __init__(self, master=None, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        self._command = kw.pop("command", None)
        for k in self._BUTTON_ONLY:
            kw.pop(k, None)
        kw.setdefault("relief", "raised")
        kw.setdefault("padx", 3)
        kw.setdefault("pady", 1)
        super().__init__(master, **kw)
        self.bindtags((self.bindtags()[0], "HopMacButton", *self.bindtags()[1:]))
        if not MacButton._bound:
            MacButton._bound = True
            self.bind_class("HopMacButton", "<ButtonPress-1>", MacButton._press)
            self.bind_class("HopMacButton", "<ButtonRelease-1>", MacButton._release)
            self.bind_class("HopMacButton", "<B1-Leave>", MacButton._left)
            self.bind_class("HopMacButton", "<B1-Enter>", MacButton._back)

    # --- clicking ---
    @staticmethod
    def _press(e):
        w = e.widget
        if str(w.cget("state")) != "disabled":
            w._pressed = True
            super(MacButton, w).configure(state="active")

    @staticmethod
    def _left(e):
        w = e.widget
        if getattr(w, "_pressed", False):
            super(MacButton, w).configure(state="normal")

    @staticmethod
    def _back(e):
        w = e.widget
        if getattr(w, "_pressed", False):
            super(MacButton, w).configure(state="active")

    @staticmethod
    def _release(e):
        w = e.widget
        if not getattr(w, "_pressed", False):
            return
        w._pressed = False
        if str(w.cget("state")) == "disabled":
            return
        super(MacButton, w).configure(state="normal")
        if 0 <= e.x < w.winfo_width() and 0 <= e.y < w.winfo_height():
            w.invoke()

    # --- the parts of tk.Button's API the app uses ---
    def invoke(self):
        if self._command is not None and str(self.cget("state")) != "disabled":
            return self._command()

    def flash(self):
        pass

    def configure(self, cnf=None, **kw):
        if cnf is None and not kw:
            return super().configure()
        if isinstance(cnf, str):  # configure("text") asks for one option
            return self.cget(cnf) if cnf == "command" else super().configure(cnf)
        kw = {**(cnf or {}), **kw}
        if "command" in kw:
            self._command = kw.pop("command")
        for k in self._BUTTON_ONLY:
            kw.pop(k, None)
        if kw.get("state") == "active":  # the app never means "show as pressed"
            kw["state"] = "normal"
        if not kw:
            return None
        return super().configure(**kw)

    config = configure

    def cget(self, key):
        if key == "command":
            return self._command
        if key in self._BUTTON_ONLY:
            return ""
        return super().cget(key)

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def __getitem__(self, key):
        return self.cget(key)


def install() -> None:
    """Patches tkinter for the Mac. Call before creating the root window."""
    global _installed
    if not IS_MAC or _installed:
        return
    _installed = True

    orig_options = tk.Misc._options

    def _options(self, cnf, kw=None):
        if kw:
            cnf = tk._cnfmerge((cnf, kw))
        elif isinstance(cnf, (tuple, list)):
            cnf = tk._cnfmerge(cnf)
        return orig_options(self, _fix_fonts(cnf))
    tk.Misc._options = _options

    orig_format = ttk._format_optdict  # ttk styles and Treeview tags

    def _format_optdict(optdict, script=False, ignore=None):
        return orig_format(_fix_fonts(optdict), script, ignore)
    ttk._format_optdict = _format_optdict

    tk.Button = MacButton


def setup_root(root: tk.Tk, on_quit=None, icon_png=None) -> None:
    """Per-window Mac touches: Cmd+Q / the app menu's Quit, and the Dock icon."""
    if not IS_MAC:
        return
    if on_quit is not None:
        root.createcommand("tk::mac::Quit", on_quit)
    if icon_png is not None:
        try:
            root._hop_icon = tk.PhotoImage(file=str(icon_png))
            root.iconphoto(True, root._hop_icon)
        except tk.TclError:
            pass
