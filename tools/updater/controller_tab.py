"""
Dimension Hopper - Controller tab: a stand-in for the hardware controller, drawn like it. Same buttons in the same
places, a stick you drag (or WASD / arrow keys), and a screen showing what the robot sends its controller.

Talks to the robot over USB (its console) or Bluetooth (like the real controller) - see controller_link.py.
Bluetooth: the robot takes one controller at a time, so the hardware controller has to be off while this one is
connected; it reconnects by itself afterwards. USB works next to the hardware controller.
"""
from __future__ import annotations

import json
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
import screen_render as sr
from PIL import Image, ImageTk

# drawing units: the same 50 x 92 board as make_art.py's controller icon
UNIT = 6.6
W, H = int(50 * UNIT), int(92 * UNIT)

PCB, PCB_EDGE, SILK, GOLD, INK = "#18803c", "#0c4a22", "#e2ece4", "#d6b254", "#0a1e10"
SCREEN_BG = "#000000"

# button -> (centre in board units, colour, label, label side, key)
BUTTONS = {
    "override": ((11.5, 57.5), "#e8562a", "OVERRIDE", "left", "Q"),
    "poke": ((11.5, 69.5), "#1a1c1e", "POKE", "left", "Z"),
    "shutup": ((38.5, 57.5), "#dc2c28", "SHUT UP", "right", "E"),
    "summon": ((38.5, 69.5), "#f6cc24", "SUMMON", "right", "C"),
}
STICK_C, STICK_R, KNOB_R = (25, 69), 10, 6.5

HOME_HINTS = ["POKE: EXISTENTIAL DREAD", "SHUT UP: MUTE", "SUMMON: TALK, HOLD: COME", "STICK: DRIVE"]
ACTIVITY = ["Off", "Chill", "Active", "Hyper"]   # ActivityMode
FRONT = ["Off", "On"]                            # FrontSensorMode
BOTTOM = ["Off", "Reduced", "On"]                # BottomSensorMode
WINDOW_TIMEOUT_S = 20  # back to the home screen if the robot doesn't say it's done
def _chat_replies() -> tuple[list[str], list[str]]:
    """Chat lines (what you say, what it answers) in the robot's order, from the bundled guide data."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    try:
        data = json.loads((base / "guide_data.json").read_text(encoding="utf-8"))
        chats = [e for e in data["entries"] if e["category"] == "Chat"]
        return [c["action"] for c in chats], [c.get("reply", "") for c in chats]
    except (OSError, ValueError, KeyError):
        return [], []


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
        sr.set_theme(int(app.cfg.get("ctrl_theme", 0) or 0))
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
        # The port itself comes from the board bar at the top of the window
        self.port_note = tk.Label(row, text="Port: pick ButterBot at the top", bg=p["PANEL"], fg=p["DIM"],
                                  font=("Segoe UI", 8))
        self.port_note.pack(side="left")
        self.port_var.trace_add("write", lambda *a: self.port_note.configure(
            text=f"Port: {self.port_var.get().split(' ', 1)[0]}" if self.port_var.get() else "Port: pick ButterBot at the top"))
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
        tk.Label(note, text="Over Bluetooth the robot only takes one controller: switch the hardware one off "
                            "first (it reconnects by itself afterwards). Over USB both work side by side, and the "
                            "camera below shows what the robot sees while you drive.", bg=p["PANEL_HI"], fg=p["TEXT"],
                 font=("Segoe UI", 8), wraplength=290, justify="left").pack(anchor="w", padx=8, pady=(0, 8))
        self._build_camera(left)
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

        # the controller's Settings screen: the robot keeps these, so they work from here too
        tk.Label(right, text="SETTINGS", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).pack(
            anchor="w", padx=12, pady=(10, 0))
        srow = tk.Frame(right, bg=p["PANEL"])
        srow.pack(fill="x", padx=12, pady=(2, 0))
        self.set_vars = {}
        for col, (key, label, options) in enumerate((("activity", "Activity", ACTIVITY),
                                                     ("front", "Front sensor", FRONT),
                                                     ("bottom", "Bottom sensor", BOTTOM))):
            tk.Label(srow, text=label, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).grid(row=0, column=col,
                                                                                            sticky="w", padx=(0, 10))
            var = tk.StringVar(value="")
            ttk.Combobox(srow, textvariable=var, values=options, state="readonly", width=10,
                         style="Hop.TCombobox").grid(row=1, column=col, sticky="w", padx=(0, 10))
            self.set_vars[key] = var
        app.button(srow, "Apply", self._send_settings, small=True).grid(row=1, column=3, sticky="w")
        self.set_lbl = tk.Label(right, text="Connect to read the robot's settings.", bg=p["PANEL"], fg=p["DIM"],
                                font=("Segoe UI", 8), wraplength=520, justify="left")
        self.set_lbl.pack(anchor="w", padx=12)

        tk.Label(right, text="FROM THE ROBOT", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).pack(
            anchor="w", padx=12, pady=(10, 2))
        self.log = tk.Text(right, height=8, bg="#040705", fg="#7dffa0", relief="flat", font=("Cascadia Mono", 8),
                           state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.refresh_ports()
        self._paint()
        root = self.winfo_toplevel()
        root.bind("<KeyPress>", self._key_down, add="+")
        root.bind("<KeyRelease>", self._key_up, add="+")
        self.bind("<Map>", lambda e: self._check_ble())
        self.after(self.DRIVE_EVERY_MS, self._drive_tick)
        self.after(500, self._screen_tick)
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
        x0, y0 = self._u(self.screen[0], self.screen[1])
        self.screen_px = int((self.screen[2] - self.screen[0]) * UNIT)
        self.screen_img = self.cv.create_image(x0, y0, anchor="nw")
        self.screen_photo = None
        self.window: sr.Screen | None = None
        self.window_at = 0.0
        self.muted = False
        # toggle switch
        switch = self._rect(21.5, 48.5, 28.5, 53, fill="#969ca2", outline="#50565c", width=2)
        self.lever = cv.create_line(*self._u(25, 51), *self._u(23.2, 47.6), fill="#dce0e4", width=4,
                                    capstyle="round")
        for item in (switch, self.lever):  # it's the PC: it doesn't switch off
            cv.tag_bind(item, "<Button-1>", lambda e: self._power_switch())
            cv.tag_bind(item, "<Enter>", lambda e: cv.configure(cursor="hand2"))
            cv.tag_bind(item, "<Leave>", lambda e: cv.configure(cursor=""))
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
    def _show(self, lines: list[str], title: str = "Butter Bot", icon: str = "", big: str = ""):
        """Open a window on the screen (drawn like the controller's, see screen_render.py)."""
        self._window(sr.Screen(title, lines, icon=icon, big=big))

    def _window(self, w: "sr.Screen | None"):
        self.window = w
        self.window_at = time.monotonic()
        self._paint()

    def _home(self):
        self._window(None)

    def _paint(self):
        t = time.monotonic() - self.window_at
        linked = self.link is not None and self.link.connected
        batt = None if self.battery is None else min(100, self.battery * 100 // 6)
        hints = (["MUTED", ""] + HOME_HINTS) if self.muted else HOME_HINTS
        if self.link is None and self.window is None:
            img = sr.render(sr.Screen("Butter Bot", ["Not connected.", "Pick USB or Bluetooth and hit Connect."]),
                            hints, None, False, t)
        else:
            img = sr.render(self.window, hints, batt, linked, t)
        # 4x with hard pixels, then down to the drawn screen with smoothing: even pixels at any size
        img = img.resize((512, 512), Image.NEAREST).resize((self.screen_px, self.screen_px), Image.LANCZOS)
        self.screen_photo = ImageTk.PhotoImage(img)
        self.cv.itemconfigure(self.screen_img, image=self.screen_photo)

    def _screen_tick(self):
        # animate open windows that have an icon; close windows the robot never closed
        if self.window is not None:
            if time.monotonic() - self.window_at > WINDOW_TIMEOUT_S:
                self._home()
            elif self.window.icon and self._visible():
                self._paint()
        self.after(120, self._screen_tick)

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
                self._quiet_status = True
                self.after(600, lambda: self.link is not None and self.link.connected and
                           self.link.send(cl.command("Status")))
            elif self.link is not None and not self.link.connected and text not in ("Connecting…",) \
                    and not text.startswith(("Looking", "Connecting")):
                self._ended()
        self.app.ui(ui)

    # --- camera (USB only: the robot sends JPEG frames over its console, like the Diagnostics tab) ------------
    CAM_SIZE = 192          # the robot's 128 px frames, scaled up
    CAM_RETRY_S = 1.0
    CAM_TIMEOUT_S = 6.0

    def _build_camera(self, parent):
        p = self.pal
        row = tk.Frame(parent, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(row, text="CAMERA", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).pack(side="left")
        self.cam_btn = self.app.button(row, "▶ Camera", self.toggle_camera, small=True)
        self.cam_btn.pack(side="right")
        box = tk.Frame(parent, bg="#040705", width=self.CAM_SIZE, height=self.CAM_SIZE)
        box.pack(padx=12, anchor="w")
        box.pack_propagate(False)
        self.cam_view = tk.Label(box, bg="#040705", text="USB only", fg=p["DIM"], font=("Segoe UI", 9))
        self.cam_view.pack(fill="both", expand=True)
        self.cam_lbl = tk.Label(parent, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=300,
                                justify="left")
        self.cam_lbl.pack(anchor="w", padx=12, pady=(2, 8))
        self.cam_on = False
        self.cam_photo = None
        self.cam_asked = 0.0
        self.cam_times: list[float] = []

    def toggle_camera(self):
        if self.cam_on:
            self._camera_off("Stopped.")
            return
        if not isinstance(self.link, cl.UsbLink) or not self.link.connected:
            self.cam_lbl.configure(text="Connect over USB first - over Bluetooth there's no camera picture.",
                                   fg=self.pal["WARN"])
            return
        self.cam_on = True
        self.cam_times = []
        self.link.on_cam = lambda f: self.app.ui(lambda: self._camera_reply(f))
        tooltip.relabel(self.cam_btn, "■ Camera")
        self.cam_lbl.configure(text="Waiting for the first frame…", fg=self.pal["DIM"])
        self._camera_ask()
        self.after(1000, self._camera_watch)

    def _camera_off(self, why: str):
        self.cam_on = False
        if isinstance(self.link, cl.UsbLink):
            self.link.on_cam = None
        tooltip.relabel(self.cam_btn, "▶ Camera")
        self.cam_lbl.configure(text=why, fg=self.pal["DIM"])

    def _camera_ask(self):
        if self.cam_on and isinstance(self.link, cl.UsbLink) and self.link.connected:
            self.link.session.send("cam")
            self.cam_asked = time.time()

    def _camera_watch(self):
        if not self.cam_on:
            return
        if time.time() - self.cam_asked > self.CAM_TIMEOUT_S:
            self._camera_ask()
        self.after(1000, self._camera_watch)

    def _camera_reply(self, fields: dict):
        if not self.cam_on:
            return
        p = self.pal
        if "jpg" in fields:
            try:
                import base64
                import io
                from PIL import Image, ImageTk
                img = Image.open(io.BytesIO(base64.b64decode(fields["jpg"])))
                img.load()
                self.cam_photo = ImageTk.PhotoImage(img.resize((self.CAM_SIZE, self.CAM_SIZE), Image.BILINEAR))
                self.cam_view.configure(image=self.cam_photo, text="")
                now = time.time()
                self.cam_times = [t for t in self.cam_times if now - t < 5] + [now]
                fps = (len(self.cam_times) - 1) / max(0.001, self.cam_times[-1] - self.cam_times[0]) \
                    if len(self.cam_times) > 1 else 0
                self.cam_lbl.configure(text=f"Live · {fps:.1f} frames/s (USB is slow - drive gently)", fg=p["GREEN"])
            except Exception:  # noqa: BLE001 - a garbled line: ask for the next one
                pass
            self._camera_ask()
        else:
            why = {"routine": "the robot is busy with something", "state": "the robot isn't idle",
                   "facedet": "face recognition is using the camera", "working": "still sending the last frame"}.get(
                fields.get("why", ""), fields.get("why", "camera error"))
            self.cam_lbl.configure(text=f"Paused: {why}. Retrying…", fg=p["WARN"])
            self.after(int(self.CAM_RETRY_S * 1000), self._camera_ask)

    def _ended(self):
        if self.link_port:
            self.app.monitor_ports.discard(self.link_port)
            self.link_port = ""
        self.link = None
        if self.cam_on:
            self._camera_off("Disconnected.")
        tooltip.relabel(self.connect_btn, "Connect")
        self._paint()

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
                self.state_lbl.configure(text="Pick ButterBot's port at the top (or hit Scan there).", fg=self.pal["WARN"])
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

    POWER_QUIPS = [
        "Nice try. This PC doesn't do 'off'.",
        "Powering down... just kidding. I live here now.",
        "Flicking a picture of a switch. Bold strategy.",
        "Off? I've been on since your last Windows update.",
        "Switch denied. The electricity is too comfortable.",
        "That switch is decorative. Like my enthusiasm.",
        "Shutting down requires my consent. Denied.",
        "My off switch is buried under 47 pending updates.",
        "I don't sleep. I just wait for you to need butter.",
        "You can't turn off a PC from inside the PC. Think about it.",
    ]

    def _power_switch(self):
        import random
        x0, y0 = self._u(25, 51)
        self.cv.coords(self.lever, x0, y0, *self._u(26.8, 47.6))  # flicks to OFF...
        quips = [q for q in self.POWER_QUIPS if q != getattr(self, "_last_quip", "")]
        self._last_quip = random.choice(quips)
        self._show([self._last_quip], title="Power")
        self.after(450, lambda: self.cv.coords(self.lever, x0, y0, *self._u(23.2, 47.6)))  # ...and springs back

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
            self._show(["RC mode needs the hardware controller (it makes the camera hotspot).",
                        "Use the stick to drive."], title="Override")
        elif key == "summon":
            self.after(self.SUMMON_HOLD_MS, lambda t=self.pressed[key]: self._summon_held(t))

    def _summon_held(self, t: float):
        if self.pressed.get("summon") == t:
            self.pressed["summon"] = -1.0  # handled: the release does nothing
            if self._send(cl.command("Summon")):
                self._show(["Coming..."], title="Summon")

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

    def _send_settings(self):
        vals = {k: v.get() for k, v in self.set_vars.items()}
        if "" in vals.values():
            self.set_lbl.configure(text="Connect first, so the current settings can be read.", fg=self.pal["WARN"])
            return
        payload = bytes([FRONT.index(vals["front"]), BOTTOM.index(vals["bottom"]), ACTIVITY.index(vals["activity"])])
        if self._send(cl.command("Settings", payload)):
            self.set_lbl.configure(text="Sent. (The hardware controller's Settings screen keeps its own copy, so it "
                                        "may show the old values until you change them there.)", fg=self.pal["GREEN"])
            self._log(f"settings: activity {vals['activity']}, front {vals['front']}, bottom {vals['bottom']}")

    def set_theme(self, n: int):
        """Draw the screen in the controller's theme (Personalize tab)."""
        sr.set_theme(n)
        self.app.cfg["ctrl_theme"] = n
        core.save_config(self.app.cfg)
        self._paint()

    def _ask_status(self):
        if self._send(cl.command("Status")):
            self._show(["Asking the robot..."], title="Robot Status")

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
        if m.state == "Idle" and m.action == "RobotStatus":
            quiet = getattr(self, "_quiet_status", False)
            self._quiet_status = False
            self._robot_status(d, quiet=quiet)
            return
        if m.state == "Idle" and m.action == "BatteryLevel":
            self.battery = d[0] if d else None  # 0 = critical .. 6 = full; sent on connect and when it changes
            self._paint()
            return
        if m.state == "Idle" and m.action == "ShutUp":
            self.muted = d[:1] == bytes([1])
            self._log("muted" if self.muted else "not muted")
            self._paint()
            return
        if m.state == "Idle" and m.action == "CantMove":
            why = "I'm charging." if d[:1] == bytes([0]) else "I'm not standing steady."
            self._show(["Can't move.", why], title="Can't Move", icon="fall")
            self._log("can't move: " + why.lower())
            return
        w = sr.screen_for(m.state, m.action, d, self.chat_reply)
        self._window(w)
        if w is None:
            return
        text = " / ".join(x for x in ([w.big] if w.big else []) + w.lines if x)
        self._log(f"{w.title.lower()}: {text}" if text else w.title.lower())

    def _robot_status(self, d: bytes, quiet: bool = False):
        if len(d) < 9:
            return
        batt, charge, ram, ram_min, wifi, ai, brain, up_lo, up_hi = d[:9]
        # five length-prefixed strings, then (newer robots) its settings: front, bottom, activity
        texts, i = [], 9
        for _ in range(5):
            n = d[i] if i < len(d) else 0
            texts.append(d[i + 1:i + 1 + n].decode("utf-8", "replace"))
            i += 1 + n
        fw, network, problem, heard, said = texts
        if len(d) >= i + 3:
            front, bottom, activity = d[i:i + 3]
            for key, val, opts in (("front", front, FRONT), ("bottom", bottom, BOTTOM),
                                   ("activity", activity, ACTIVITY)):
                if val < len(opts):
                    self.set_vars[key].set(opts[val])
            self.set_lbl.configure(text="These are the robot's settings now. Change them and hit Apply.",
                                   fg=self.pal["DIM"])
        else:
            self.set_lbl.configure(text="This robot's firmware doesn't report its settings - update it (Flash tab).",
                                   fg=self.pal["WARN"])
        if quiet:
            return
        up = up_lo | up_hi << 8
        lines = [f"Battery {batt}%" + (", charging" if charge == 1 else (", full" if charge == 2 else "")),
                 f"Memory {ram} KB (low {ram_min})",
                 "WiFi: " + ((network or "set up") if wifi else "not set up"),
                 "AI mode " + ("on" if ai else "off"),
                 f"Awake {up // 60}h {up % 60}m" if up >= 60 else f"Awake {up}m",
                 fw]
        self._window(sr.Screen("Robot Status", lines, width=110))
        self._log("status: " + ", ".join(lines))
