"""
Dimension Hopper - Controller tab: a stand-in for the hardware controller, drawn like it. Same buttons in the same
places, a stick you drag (or WASD / arrow keys), and a screen showing what the robot sends its controller.

Talks to the robot over USB (its console) or Bluetooth (like the real controller) - see controller_link.py.
Bluetooth: the robot takes one controller at a time, so the hardware controller has to be off while this one is
connected; it reconnects by itself afterwards. USB works next to the hardware controller.
"""
from __future__ import annotations

import json
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import sys

import controller_link as cl
import hopper_core as core
import tooltip
from stick_widget import Stick

# drawing units: the same 50 x 92 board as make_art.py's controller icon
UNIT = 6.6
W, H = int(50 * UNIT), int(92 * UNIT)

PCB, PCB_EDGE, SILK, GOLD, INK = "#18803c", "#0c4a22", "#e2ece4", "#d6b254", "#0a1e10"
LIME, SCREEN_BG = "#b6ff3b", "#06100a"

# button -> (centre in board units, colour, label, label side, key)
BUTTONS = {
    "override": ((11.5, 57.5), "#e8562a", "OVERRIDE", "left", "Q"),
    "poke": ((11.5, 69.5), "#1a1c1e", "POKE", "left", "Z"),
    "shutup": ((38.5, 57.5), "#dc2c28", "SHUT UP", "right", "E"),
    "summon": ((38.5, 69.5), "#f6cc24", "SUMMON", "right", "C"),
}
STICK_C, STICK_R, KNOB_R = (25, 69), 10, 6.5

HOME_HINTS = ["OVERRIDE: RC NEEDS THE", "  REAL CONTROLLER", "POKE: EXISTENTIAL DREAD", "SHUT UP: MUTE",
              "SUMMON: TALK, HOLD: COME", "STICK: DRIVE"]
LISTEN = {0: ["LISTENING...", "RELUCTANTLY"], 1: ["DIDN'T CATCH", "THAT"], 2: ["NEVER MIND"],
          3: ["HEARD YOU.", "UNFORTUNATELY."], 4: ["CONSULTING", "MY BRAIN..."]}


def _words(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", name).upper()


def _chat_replies() -> tuple[list[str], list[str]]:
    """Chat lines (what you say, what it answers) in the robot's order, from the bundled guide data."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    try:
        data = json.loads((base / "guide_data.json").read_text(encoding="utf-8"))
        chats = [e for e in data["entries"] if e["category"] == "Chat"]
        return [c["action"] for c in chats], [c.get("reply", "") for c in chats]
    except (OSError, ValueError, KeyError):
        return [], []


def _wrap(text: str, width: int = 22) -> list[str]:
    out, line = [], ""
    for w in text.upper().split():
        if len(line) + len(w) + (1 if line else 0) > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


class ControllerTab(tk.Frame):
    DRIVE_EVERY_MS = 120   # the robot stops by itself 400 ms after the last drive message
    SUMMON_TAP_MS, SUMMON_HOLD_MS = 250, 2000

    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        self.link: cl.Link | None = None
        self.link_port = ""
        self.pressed: dict[str, float] = {}
        self.stick = (0, 0)
        self.chat_say, self.chat_reply = _chat_replies()
        self.ble_ok: bool | None = None
        self.battery: int | None = None
        p = pal

        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 4))
        tk.Label(top, text="CONTROLLER", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="drive and talk to the robot from this PC - over USB or Bluetooth", bg=p["BG"],
                 fg=p["DIM"], font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))

        body = tk.Frame(self, bg=p["BG"])
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        # --- left: connection -------------------------------------------------------------------------------
        left = tk.Frame(body, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1, width=330)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="CONNECT", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(
            anchor="w", padx=12, pady=(10, 4))
        self.mode = tk.StringVar(value=app.cfg.get("emu_mode", "usb"))
        self.usb_rb = tk.Radiobutton(left, text="USB cable (robot plugged into this PC)", variable=self.mode,
                                     value="usb", command=self._mode_changed, **self._rb())
        self.usb_rb.pack(anchor="w", padx=10)
        row = tk.Frame(left, bg=p["PANEL"])
        row.pack(fill="x", padx=30, pady=(2, 6))
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox", width=24)
        self.port_box.pack(side="left")
        scan_row = tk.Frame(left, bg=p["PANEL"])
        scan_row.pack(fill="x", padx=30, pady=(0, 6))
        app.scan_button(scan_row, p["PANEL"])
        self.ble_rb = tk.Radiobutton(left, text="Bluetooth (wireless, like the real controller)", variable=self.mode,
                                     value="ble", command=self._mode_changed, **self._rb())
        self.ble_rb.pack(anchor="w", padx=10, pady=(4, 0))
        self.ble_note = tk.Label(left, text="Checking for Bluetooth…", bg=p["PANEL"], fg=p["DIM"],
                                 font=("Segoe UI", 8), wraplength=280, justify="left")
        self.ble_note.pack(anchor="w", padx=30)

        self.connect_btn = app.button(left, "Connect", self.toggle)
        self.connect_btn.pack(anchor="w", padx=12, pady=(10, 4))
        self.state_lbl = tk.Label(left, text="Not connected", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9),
                                  wraplength=300, justify="left")
        self.state_lbl.pack(anchor="w", padx=12)

        note = tk.Frame(left, bg=p["PANEL_HI"])
        note.pack(fill="x", padx=12, pady=(14, 8))
        tk.Label(note, text="ⓘ  One controller at a time", bg=p["PANEL_HI"], fg=p["WARN"],
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=8, pady=(6, 2))
        tk.Label(note, text="Over Bluetooth the robot only takes one controller. Switch the hardware controller "
                            "off before connecting here; when you disconnect, it reconnects by itself (its pairing "
                            "is kept).\n\nOver USB both work side by side.\n\nRC mode with the camera needs the "
                            "hardware controller (it makes the WiFi hotspot); here the stick drives the robot "
                            "directly.", bg=p["PANEL_HI"], fg=p["TEXT"], font=("Segoe UI", 8), wraplength=290,
                 justify="left").pack(anchor="w", padx=8, pady=(0, 8))
        tk.Label(left, text="Keys: Q override · E shut up · Z poke · C summon\nWASD / arrows: stick",
                 bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), justify="left").pack(anchor="w", padx=12)

        # --- middle: the controller ------------------------------------------------------------------------
        mid = tk.Frame(body, bg=p["BG"])
        mid.pack(side="left", fill="y", padx=14)
        self.cv = tk.Canvas(mid, width=W, height=H, bg=p["BG"], highlightthickness=0)
        self.cv.pack()
        self._draw_board()

        # --- right: menu + what the robot said -----------------------------------------------------------
        right = tk.Frame(body, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="MENU", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(
            anchor="w", padx=12, pady=(10, 2))
        tk.Label(right, text="Double-click an action to send it (the real controller's stick menu)",
                 bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).pack(anchor="w", padx=12)
        lists = tk.Frame(right, bg=p["PANEL"])
        lists.pack(fill="both", expand=True, padx=12, pady=6)
        lb = dict(bg=p["PANEL_HI"], fg=p["TEXT"], selectbackground=p["GREEN"], selectforeground=p["BG"],
                  relief="flat", highlightthickness=0, font=("Segoe UI", 9), activestyle="none", exportselection=False)
        self.folders = tk.Listbox(lists, width=14, **lb)
        self.folders.pack(side="left", fill="y")
        self.entries = tk.Listbox(lists, **lb)
        self.entries.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.menu = [(name, items) for name, items in cl.MENU]
        chat_i = next(i for i, (n, _) in enumerate(self.menu) if n == "Chat")
        self.menu[chat_i] = ("Chat", [(s, "Chat", i) for i, s in enumerate(self.chat_say)])
        for name, _ in self.menu:
            self.folders.insert("end", name)
        self.folders.bind("<<ListboxSelect>>", lambda e: self._fill_entries())
        self.entries.bind("<Double-Button-1>", lambda e: self._send_entry())
        self.entries.bind("<Return>", lambda e: self._send_entry())
        self.folders.selection_set(0)
        self._fill_entries()
        btns = tk.Frame(right, bg=p["PANEL"])
        btns.pack(fill="x", padx=12)
        app.button(btns, "Send", self._send_entry, small=True).pack(side="left")
        app.button(btns, "Robot status", self._ask_status, small=True).pack(side="left", padx=6)

        tk.Label(right, text="FROM THE ROBOT", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).pack(
            anchor="w", padx=12, pady=(10, 2))
        self.log = tk.Text(right, height=8, bg="#040705", fg="#7dffa0", relief="flat", font=("Cascadia Mono", 8),
                           state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.refresh_ports()
        self._show(["NOT CONNECTED", "", "PICK USB OR", "BLUETOOTH AND", "HIT CONNECT"], title="BUTTERBOT")
        root = self.winfo_toplevel()
        root.bind("<KeyPress>", self._key_down, add="+")
        root.bind("<KeyRelease>", self._key_up, add="+")
        self.bind("<Map>", lambda e: self._check_ble())
        self.after(self.DRIVE_EVERY_MS, self._drive_tick)
        self._mode_changed(save=False)

    # --- small helpers ------------------------------------------------------------------------------------
    def _rb(self) -> dict:
        p = self.pal
        return dict(bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"], activebackground=p["PANEL"],
                    activeforeground=p["GREEN"], highlightthickness=0, font=("Segoe UI", 9),
                    disabledforeground=p["EDGE"])

    @staticmethod
    def _u(x: float, y: float) -> tuple[float, float]:
        return x * UNIT, y * UNIT

    def _oval(self, cx, cy, r, **kw):
        x, y = self._u(cx, cy)
        rr = r * UNIT
        return self.cv.create_oval(x - rr, y - rr, x + rr, y + rr, **kw)

    def _rect(self, x0, y0, x1, y1, **kw):
        return self.cv.create_rectangle(*self._u(x0, y0), *self._u(x1, y1), **kw)

    # --- the board -----------------------------------------------------------------------------------------
    def _draw_board(self):
        cv = self.cv
        # outline: narrower top around the module, shoulders, chamfered corners (like the real PCB)
        pts = [(13, 1.5), (37, 1.5), (39, 4), (39, 15), (43.5, 17), (46, 20), (46, 85), (43, 89), (7, 89), (4, 85),
               (4, 20), (6.5, 17), (11, 15), (11, 4)]
        cv.create_polygon([c for x, y in pts for c in self._u(x, y)], fill=PCB, outline=PCB_EDGE, width=3)
        for x, y in ((14.5, 5.5), (35.5, 5.5), (8, 84.5), (42, 84.5)):
            self._oval(x, y, 2.0, fill=GOLD, outline="")
            self._oval(x, y, 1.0, fill=INK, outline="")
        for i in range(4):  # silkscreen hatching down both edges, below the buttons
            y = 79.5 + i * 1.8
            cv.create_line(*self._u(5, y + 2), *self._u(8, y), fill=SILK, width=2)
            cv.create_line(*self._u(42, y), *self._u(45, y + 2), fill=SILK, width=2)
        # ESP32 module + green LED
        self._rect(16, 1, 30, 13, fill="#b0b6bc", outline="#6e747a", width=2)
        self._rect(17.5, 4.5, 28.5, 12, fill="#c8cdd2", outline="")
        for i in range(3):
            cv.create_line(*self._u(19, 6.5 + i * 1.8), *self._u(27, 6.5 + i * 1.8), fill="#8c9298")
        self.led_big = self._oval(34, 8, 2.4, fill="#2d5a36", outline="#0c4a22")
        # status LEDs left of the screen: link, on, charge
        self.led_link = self._oval(8, 26, 0.8, fill="#3a3a3a", outline="")
        self._oval(8, 29, 0.8, fill="#7dff8a", outline="")
        cv.create_text(*self._u(8, 23.5), text="📶", fill=SILK, font=("Segoe UI", 7))
        # screen
        self._rect(11, 18, 39, 46, fill="#0c0c0e", outline="#2c3034", width=2)
        self.screen = (12.5, 19.5, 37.5, 44.5)
        self._rect(*self.screen, fill=SCREEN_BG, outline="")
        self.screen_items: list[int] = []
        # toggle switch
        self._rect(21.5, 48.5, 28.5, 53, fill="#969ca2", outline="#50565c", width=2)
        cv.create_line(*self._u(25, 51), *self._u(26.8, 47.6), fill="#dce0e4", width=4, capstyle="round")
        cv.create_text(*self._u(19.5, 47.3), text="ON", fill=SILK, font=("Segoe Print", 7))
        cv.create_text(*self._u(30.5, 47.3), text="OFF", fill=SILK, font=("Segoe Print", 7))
        # buttons
        self.btn_items = {}
        for key, ((x, y), col, label, side, k) in BUTTONS.items():
            self._oval(x, y, 5.1, fill="#141618", outline="#30343a")
            cap = self._oval(x, y, 3.9, fill=col, outline="#000000", width=1)
            shine = self._oval(x - 1.2, y - 1.7, 1.1, fill="#ffffff", outline="", stipple="gray50")
            self.btn_items[key] = (cap, col)
            for item in (cap, shine):
                cv.tag_bind(item, "<ButtonPress-1>", lambda e, b=key: self._press(b))
                cv.tag_bind(item, "<ButtonRelease-1>", lambda e, b=key: self._release(b))
            # label centred over the button, kept on the board
            # top row: label above the button; bottom row: below it (the stick sits between them)
            lx = min(max(x, 12.5), 37.5)
            ly = y - 6.4 if y < 60 else y + 6.4
            cv.create_text(*self._u(lx, ly), text=f"{label} [{k}]", fill=SILK, font=("Segoe Print", 7, "bold"))
        # joystick (shared with the Diagnostics tab)
        self.stick_w = Stick(cv, *self._u(*STICK_C), STICK_R * UNIT, KNOB_R * UNIT, self._stick_moved)
        # USB-C
        self._rect(21, 87, 29, 90.5, fill="#babec4", outline="#6e747a", width=2)

    # --- the screen ---------------------------------------------------------------------------------------
    def _show(self, lines: list[str], title: str = "BUTTERBOT"):
        cv = self.cv
        for i in self.screen_items:
            cv.delete(i)
        x0, y0, x1, y1 = self.screen
        self.screen_items = [self._rect(x0, y0, x1, y0 + 3, fill=LIME, outline="")]
        self.screen_items.append(cv.create_text(*self._u(x0 + 1, y0 + 1.5), text=title[:24], anchor="w",
                                                fill=SCREEN_BG, font=("Consolas", 8, "bold")))
        y = y0 + 5.5
        for line in lines[:9]:
            self.screen_items.append(cv.create_text(*self._u((x0 + x1) / 2, y), text=line[:24], fill=LIME,
                                                    font=("Consolas", 8, "bold")))
            y += 2.4

    def _home(self):
        self._show(HOME_HINTS)

    # --- connection ---------------------------------------------------------------------------------------
    def refresh_ports(self):
        self.port_box.configure(values=[pt.label() for pt in core.list_ports()])
        if not self.port_var.get():
            known = self.app.known_ports.get("bot") if hasattr(self.app, "known_ports") else None
            if known:
                core.select_port_in(self.port_box, self.port_var, known)

    def use_port(self, kind: str, device: str):
        if kind == "bot" and self.link is None:
            core.select_port_in(self.port_box, self.port_var, device)

    def _check_ble(self):
        if self.ble_ok is not None:
            return
        self.ble_ok = False

        def work():
            ok, why = cl.bluetooth_available()
            self.app.ui(lambda: self._ble_result(ok, why))
        threading.Thread(target=work, daemon=True).start()

    def _ble_result(self, ok: bool, why: str):
        self.ble_ok = ok
        self.ble_rb.configure(state="normal" if ok else "disabled")
        self.ble_note.configure(text="Bluetooth found." if ok else why,
                                fg=self.pal["DIM"] if ok else self.pal["WARN"])
        if not ok and self.mode.get() == "ble":
            self.mode.set("usb")
            self._mode_changed()

    def _mode_changed(self, save: bool = True):
        usb = self.mode.get() == "usb"
        self.port_box.configure(state="readonly" if usb else "disabled")
        if save:
            self.app.cfg["emu_mode"] = self.mode.get()
            core.save_config(self.app.cfg)

    def _state(self, text: str, ok: bool):
        def ui():
            self.state_lbl.configure(text=text, fg=self.pal["GREEN"] if ok else self.pal["DIM"])
            self.cv.itemconfigure(self.led_link, fill="#5ad7ff" if ok else "#3a3a3a")
            if ok:
                tooltip.relabel(self.connect_btn, "Disconnect")
                self._home()
            elif self.link is not None and not self.link.connected and text not in ("Connecting…",) \
                    and not text.startswith(("Looking", "Connecting")):
                self._ended()
        self.app.ui(ui)

    def _ended(self):
        if self.link_port:
            self.app.monitor_ports.discard(self.link_port)
            self.link_port = ""
        self.link = None
        tooltip.relabel(self.connect_btn, "Connect")
        self._show(["NOT CONNECTED"])

    def toggle(self):
        if self.link is not None:
            self._stop_driving()
            self.link.close()
            if isinstance(self.link, cl.BleLink):
                return  # its thread reports "Disconnected" and cleans up
            self._ended()
            self.state_lbl.configure(text="Disconnected", fg=self.pal["DIM"])
            return
        if self.mode.get() == "usb":
            port = self.port_var.get().split(" ", 1)[0]
            if not port:
                self.state_lbl.configure(text="Pick the robot's port (or hit Scan).", fg=self.pal["WARN"])
                return
            if self.app.port_in_use(port) or self.app.busy:
                return
            self.link = cl.UsbLink(port, self._message, self._state)
            self.link_port = port
            self.app.monitor_ports.add(port)  # other tabs leave it alone while it's ours
            self.state_lbl.configure(text=f"Connecting over USB ({port})…", fg=self.pal["DIM"])
            self.link.start()
            self.after(4000, self._usb_check)
        else:
            self.link = cl.BleLink(self._message, self._state)
            self.link.start()
        tooltip.relabel(self.connect_btn, "Disconnect")

    def _usb_check(self):
        if isinstance(self.link, cl.UsbLink) and not self.link.connected:
            self.state_lbl.configure(text="No answer on that port. Is it the robot, switched on, with this "
                                          "release's firmware?", fg=self.pal["WARN"])

    def _send(self, data: bytes):
        if self.link is not None and self.link.connected:
            self.link.send(data)
            return True
        self.state_lbl.configure(text="Connect first.", fg=self.pal["WARN"])
        return False

    def shutdown(self):
        if self.link is not None:
            self._stop_driving()
            self.link.close()

    # --- buttons ------------------------------------------------------------------------------------------
    def _flash(self, key: str, down: bool):
        cap, col = self.btn_items[key]
        self.cv.itemconfigure(cap, fill="#ffffff" if down else col)

    def _press(self, key: str):
        if key in self.pressed:
            return
        self.pressed[key] = time.monotonic()
        self._flash(key, True)
        if key == "shutup":
            self._send(cl.command("ShutUp"))
        elif key == "override":
            self._show(["RC MODE NEEDS THE", "HARDWARE CONTROLLER", "(IT MAKES THE", "CAMERA HOTSPOT)", "",
                        "USE THE STICK", "TO DRIVE"], title="OVERRIDE")
        elif key == "summon":
            self.after(self.SUMMON_HOLD_MS, lambda t=self.pressed[key]: self._summon_held(t))

    def _summon_held(self, t: float):
        if self.pressed.get("summon") == t:
            self.pressed["summon"] = -1.0  # handled: the release does nothing
            if self._send(cl.command("Summon")):
                self._show(["COMING..."], title="SUMMON")

    def _release(self, key: str):
        t = self.pressed.pop(key, None)
        self._flash(key, False)
        if t is None:
            return
        if key == "poke":
            self._send(cl.command("Poke"))
        elif key == "summon" and t >= 0 and (time.monotonic() - t) * 1000 < self.SUMMON_TAP_MS * 4:
            # a tap: listen (the hardware's 250 ms is tight for a mouse click, so allow a little longer)
            self._send(cl.command("Listen"))

    KEYMAP = {"q": "override", "e": "shutup", "z": "poke", "c": "summon"}

    def _visible(self) -> bool:
        try:
            return bool(self.winfo_ismapped())
        except tk.TclError:
            return False

    def _key_down(self, e):
        if not self._visible() or isinstance(e.widget, (tk.Entry, ttk.Combobox, tk.Text)):
            return
        k = e.keysym.lower()
        if k in self.KEYMAP:
            self._press(self.KEYMAP[k])
        else:
            self.stick_w.key(k, True)

    def _key_up(self, e):
        k = e.keysym.lower()
        if k in self.KEYMAP:
            self._release(self.KEYMAP[k])
        else:
            self.stick_w.key(k, False)

    # --- stick --------------------------------------------------------------------------------------------
    def _stick_moved(self, x: int, y: int):
        was = self.stick
        self.stick = (x, y)
        if (x, y) == (0, 0) and was != (0, 0):
            self._stop_driving()

    def _stop_driving(self):
        if self.link is not None and self.link.connected:
            self.link.send(cl.drive(0, 0))

    def _drive_tick(self):
        # Re-send while the stick is pushed: the robot stops by itself if the messages stop
        if self.stick != (0, 0) and self.link is not None and self.link.connected:
            self.link.send(cl.drive(*self.stick))
        self.after(self.DRIVE_EVERY_MS, self._drive_tick)

    # --- menu ---------------------------------------------------------------------------------------------
    def _fill_entries(self):
        sel = self.folders.curselection()
        if not sel:
            return
        self.entries.delete(0, "end")
        for label, _, _ in self.menu[sel[0]][1]:
            self.entries.insert("end", label)

    def _send_entry(self):
        f, e = self.folders.curselection(), self.entries.curselection()
        if not f or not e:
            return
        label, name, data = self.menu[f[0]][1][e[0]]
        if self._send(cl.scenario(name, data)):
            self._log(f"→ {label}")

    def _ask_status(self):
        if self._send(cl.command("Status")):
            self._show(["ASKING THE ROBOT..."], title="ROBOT STATUS")

    # --- what the robot sends -----------------------------------------------------------------------------
    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _message(self, m: cl.Message):
        self.app.ui(lambda: self._handle(m))

    def _handle(self, m: cl.Message):
        self.cv.itemconfigure(self.led_big, fill="#78ff8c")
        self.after(150, lambda: self.cv.itemconfigure(self.led_big, fill="#2d5a36"))
        d = m.data
        if m.state == "Listen":
            lines = LISTEN.get(d[0] if d else 0, ["LISTENING..."])
            self._show(lines, title="LISTEN")
            self._log("listen: " + " ".join(lines).lower())
            return
        if m.state == "Scenario" and m.action == "Subtitle":
            s = m.strings() + ["", ""]
            heard, say = s[0], s[1]
            self._show((["YOU: " + heard[:18]] if heard else []) + _wrap(say), title="AI")
            self._log(f"you: {heard}  ·  bot: {say}")
            return
        if m.state == "Scenario" and m.action == "Chat":
            i = d[0] if d else -1
            reply = self.chat_reply[i] if 0 <= i < len(self.chat_reply) else ""
            self._show(_wrap(reply) or ["..."], title="CHAT")
            self._log(f"chat: {reply}")
            return
        if m.state == "Idle" and m.action == "RobotStatus":
            self._robot_status(d)
            return
        if m.state == "Idle" and m.action == "None":
            self._home()
            return
        if m.state == "Idle" and m.action == "BatteryLevel":
            self.battery = d[0] if d else None  # sent on connect and when it changes
            self._log(f"battery level {self.battery}")
            return
        if m.state == "Idle" and m.action == "ShutUp":
            muted = bool(d[:1] == b"")
            self._show(["MUTED"] if muted else HOME_HINTS, title="SHUT UP" if muted else "BUTTERBOT")
            self._log("muted" if muted else "not muted")
            return
        if m.state == "Idle" and m.action == "CantMove":
            why = "CHARGING" if (d[:1] == b"\x00") else "NOT STABLE"
            self._show(["CAN'T MOVE:", why], title="NOPE")
            self._log(f"can't move ({why.lower()})")
            return
        title = _words(m.action) if m.action not in ("None",) else m.state.upper()
        self._show([title], title=m.state.upper())
        self._log(f"{m.state.lower()}: {title.lower()}")

    def _robot_status(self, d: bytes):
        if len(d) < 9:
            return
        batt, charge, ram, ram_min, wifi, ai, brain, up_lo, up_hi = d[:9]
        m = cl.Message("Idle", "RobotStatus", d[9:])
        fw, network, problem, heard, said = (m.strings() + [""] * 5)[:5]
        up = up_lo | up_hi << 8
        lines = [f"BATTERY {batt}%" + (" CHG" if charge == 1 else (" FULL" if charge == 2 else "")),
                 f"MEMORY {ram} KB (LOW {ram_min})",
                 "WIFI " + ((network or "SET UP").upper()[:16] if wifi else "NOT SET UP"),
                 "AI " + ("ON" if ai else "OFF"),
                 f"AWAKE {up // 60}H {up % 60}M" if up >= 60 else f"AWAKE {up}M",
                 fw.upper()[:22]]
        self._show(lines, title="ROBOT STATUS")
        self._log("status: " + ", ".join(line.lower() for line in lines))
