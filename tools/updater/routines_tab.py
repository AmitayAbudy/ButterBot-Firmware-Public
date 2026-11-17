"""
Dimension Hopper - Routines tab: the owner's own routines, voice commands, schedules, quiet hours and the robot's clock.

Everything lives on the robot (main/src/Services/UserData.h) so it works without the PC; this tab edits it over USB:
  routines        "rout", "rout get <i>", "rout begin <i>" + "rout put <hex>"... + "rout save", "rout run <i>"
  voice commands  "cmds", "cmds set <i> <routine> <hex phrase> <hex phonemes>", "cmds del <i>"
  schedules       "sched", "sched set <i> <days> <minute> <routine> <on>", "sched del <i>", "quiet <on> <start> <end>"
  clock           "clock", "clock set <seconds>" (local wall-clock time, counted as if it were UTC)
The voice commands are also handed to the PC's AI brain as actions, so they work in AI conversations too.
"""
from __future__ import annotations

import calendar
import threading
import time
import tkinter as tk
from tkinter import ttk

import controller_link
import hopper_core as core
import phonemes
from console_tab import FUZZY_AMB_RE, FUZZY_NO_RE, FUZZY_OK_RE, TIMEOUT_RE, TOP_RE
import mac_compat
import tooltip

MAX_ROUTINES = 12
MAX_STEPS = 30
MAX_TEXT = 1500
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")   # shown Monday first
DAY_BIT = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}   # the robot's bits (tm_wday)
KINDS = (("S", "💬 Say a line"), ("A", "⭐ Built-in action"), ("M", "🚗 Drive"), ("W", "⏳ Wait"), ("D", "💃 Dance"),
         ("L", "💡 LED module"))
# "A" steps: the robot's own actions a routine can run (the firmware's UserData::BuiltinActions)
ACTIONS = (("time", "🕒 Tell the time"), ("joke", "😂 Tell a joke"), ("fact", "🧠 Tell a fact"),
           ("butter", "🧈 Pass the butter"), ("portal", "🌀 Open a portal"))
DIRECTIONS = (("forward", 1, 1), ("backward", -1, -1), ("turn left", 0.35, 1), ("turn right", 1, 0.35),
              ("spin left", -1, 1), ("spin right", 1, -1))
LED_MODES = ("on", "off", "strobe", "breathe")
EXAMPLE = "Good morning\nS Good morning! Rise and shine, it's another day of passing butter.\nD 6\nS Right. Coffee time."


def parse_steps(text: str) -> tuple[str, list[tuple]]:
    """A routine's text -> (name, steps). Steps: ("S", text) ("M", left, right, ms) ("W", ms) ("L", mode) ("D", s)."""
    lines = text.split("\n")
    steps = []
    for line in lines[1:]:
        if len(line) < 3:
            continue
        k, arg = line[0], line[2:]
        try:
            if k == "S":
                steps.append(("S", arg))
            elif k == "M":
                l, r, ms = (int(x) for x in arg.split())
                steps.append(("M", l, r, ms))
            elif k in "WD":
                steps.append((k, int(arg)))
            elif k in "LA":
                steps.append((k, arg))
        except ValueError:
            pass
    return lines[0], steps


def build_text(name: str, steps: list[tuple]) -> str:
    out = [" ".join(name.split())[:24]]
    for s in steps:
        out.append(s[0] + " " + " ".join(str(x) for x in s[1:]))
    return "\n".join(out)


def direction_of(l: int, r: int) -> tuple[str, int] | None:
    """(direction name, speed %) for motor speeds that match one of the presets."""
    for name, a, b in DIRECTIONS:
        s = max(abs(l), abs(r))
        if s and round(a * s) == l and round(b * s) == r:
            return name, s
    return None


def describe(s: tuple) -> str:
    """One step as a line for the step list, with its kind's icon (an action has its own)."""
    text = _describe(s)
    icon = "" if s[0] == "A" else next((n.split(" ", 1)[0] for k, n in KINDS if k == s[0]), "")
    return f"{icon} {text}" if icon else text


def _describe(s: tuple) -> str:
    if s[0] == "S":
        return f'Say "{s[1]}"'
    if s[0] == "M":
        d = direction_of(s[1], s[2])
        what = f"Drive {d[0]} at {d[1]}%" if d else f"Drive (left {s[1]}, right {s[2]})"
        return f"{what} for {s[3] / 1000:g} s"
    if s[0] == "W":
        return f"Wait {s[1] / 1000:g} s"
    if s[0] == "D":
        return f"Dance for {s[1]} s"
    if s[0] == "L":
        return f"LED module: {s[1]}"
    if s[0] == "A":
        return dict(ACTIONS).get(s[1], f"Action: {s[1]}")
    return str(s)


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def days_text(bits: int) -> str:
    if bits & 0x7f == 0x7f:
        return "Every day"
    if bits == 0b0111110:
        return "Weekdays"
    if bits == 0b1000001:
        return "Weekends"
    return " ".join(d for d in DAYS if bits & (1 << DAY_BIT[d]))


def pc_now() -> int:
    """This PC's local wall-clock time, as the robot counts it (seconds, read as UTC)."""
    return calendar.timegm(time.localtime())


class RoutinesTab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        p = pal
        self.working = False
        self.robot_ok = False
        self.routines: dict[int, str] = {}          # slot -> text, as on the robot
        self.sel: int | None = None                 # the routine in the editor
        self.steps: list[tuple] = []
        self.commands: list[tuple[int, str, str]] = []   # (routine, phrase, phonemes)
        self.schedules: list[tuple[int, int, int, bool]] = []   # (days, minute, routine, on)
        self.clock_offset: int | None = None        # robot clock - PC clock, seconds
        self.custom_time = False                     # set to something other than the PC's time this session
        self.taught: dict[str, str] = {}             # phrase -> phonemes the robot heard (Teach by voice)
        self.clock_edited = False

        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(top, text="ROUTINES", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="your own routines, voice commands and schedules - saved on the robot, they work without "
                           "the PC", bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 9)).pack(side="left", padx=10,
                                                                                         pady=(4, 0))
        ports = tk.Frame(self, bg=p["BG"])
        ports.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(ports, text="Robot", bg=p["BG"], fg=p["DIM"], width=9, anchor="w").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(ports, textvariable=self.port_var, state="readonly", style="Hop.TCombobox",
                                     width=40)
        # (not shown: the board bar at the top of the window picks the port)
        self.port_lbl = tk.Label(ports, text="Plug the robot in by USB and switch it on.", bg=p["BG"], fg=p["DIM"],
                                 font=("Segoe UI", 8))
        self.port_lbl.grid(row=0, column=1, sticky="w")

        # One notice for the whole tab (not one per card) while the robot hasn't answered
        self.need_lbl = tk.Label(self, text="🔒  Everything here lives on the robot:"
                                 " plug it in by USB, switch it on and hit Scan at the top.",
                                 bg=p["PANEL_HI"], fg=p["WARN"], font=("Segoe UI Semibold", 9),
                                 anchor="w", padx=10, pady=5)
        body = self.body = tk.Frame(self, bg=p["BG"])
        body.pack(fill="both", expand=True, padx=(18, 4), pady=(0, 10))
        canvas = tk.Canvas(body, bg=p["BG"], highlightthickness=0)
        sb = ttk.Scrollbar(body, command=canvas.yview, style="Hop.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        cols = tk.Frame(canvas, bg=p["BG"])
        win = canvas.create_window(0, 0, window=cols, anchor="nw")
        cols.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width - 10))

        def wheel(e):
            if canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(mac_compat.wheel_steps(e), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        self.left = tk.Frame(cols, bg=p["BG"])
        self.left.pack(side="left", fill="both", expand=True)
        self.right = tk.Frame(cols, bg=p["BG"])
        self.right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        self._build_routines()
        self._build_commands()
        self._build_schedule()
        self._build_quiet()
        self._build_clock()
        self.cards = (self.rout_card, self.cmd_card, self.sched_card, self.quiet_card, self.clock_card)
        self._gate_all()
        self.refresh_ports()
        self._looked = False
        self.bind("<Map>", lambda e: self._first_look())
        self.after(1000, self._clock_tick)

    # --- shared bits --------------------------------------------------------------------------------------
    def _card(self, parent, title: str, hint: str):
        p = self.pal
        card = tk.Frame(parent, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        card.pack(fill="x", pady=(0, 10))
        tk.Label(card, text=title, bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(
            anchor="w", padx=12, pady=(8, 0))
        tk.Label(card, text=hint, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=500,
                 justify="left").pack(anchor="w", padx=12, pady=(0, 6))
        return card

    def _entry(self, parent, var, width=22):
        p = self.pal
        return tk.Entry(parent, textvariable=var, width=width, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                        insertbackground=p["GREEN"], highlightthickness=1, highlightbackground=p["EDGE"],
                        highlightcolor=p["GREEN"], disabledbackground=p["PANEL_HI"], disabledforeground=p["DIM"])

    def _list(self, parent, height: int, width: int = 60) -> tk.Listbox:
        p = self.pal
        lb = tk.Listbox(parent, height=height, width=width, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                        selectbackground=p["GREEN"], selectforeground=p["BG"], highlightthickness=0,
                        activestyle="none", font=("Segoe UI", 9), exportselection=False)
        return lb

    def _spin(self, parent, var, lo, hi, width=5, inc=1.0, fmt=None):
        p = self.pal
        kw = dict(format=fmt) if fmt else {}
        return tk.Spinbox(parent, from_=lo, to=hi, increment=inc, textvariable=var, width=width, bg=p["PANEL_HI"],
                          fg=p["TEXT"], relief="flat", buttonbackground=p["PANEL_HI"], insertbackground=p["GREEN"],
                          highlightthickness=0, disabledbackground=p["PANEL_HI"], **kw)

    def _combo(self, parent, var, values, width=14):
        return ttk.Combobox(parent, textvariable=var, values=values, state="readonly", style="Hop.TCombobox",
                            width=width)

    def _label(self, parent, text, dim=True, **kw):
        p = self.pal
        return tk.Label(parent, text=text, bg=p["PANEL"], fg=p["DIM"] if dim else p["TEXT"],
                        font=("Segoe UI", 8 if dim else 9), **kw)

    @staticmethod
    def _hex(text: str) -> str:
        return text.encode("utf-8").hex() or "-"

    @staticmethod
    def _unhex(h: str) -> str:
        try:
            return bytes.fromhex(h).decode("utf-8", "replace") if h and h != "-" else ""
        except ValueError:
            return ""

    def _gate(self, card, on: bool):
        p = self.pal

        def walk(w):
            for c in w.winfo_children():
                if c is getattr(card, "_gate", None):
                    continue
                if isinstance(c, (tk.Button, tk.Radiobutton, tk.Checkbutton, tk.Entry, tk.Scale, tk.Spinbox,
                                  tk.Listbox)):
                    c.configure(state="normal" if on else "disabled")
                elif isinstance(c, ttk.Combobox):
                    c.configure(state="readonly" if on else "disabled")
                walk(c)
        walk(card)
        gate = getattr(card, "_gate", None)
        if gate is not None:
            if on:
                gate.pack_forget()
            elif not gate.winfo_ismapped():
                gate.pack(anchor="w", padx=12, pady=(0, 8), before=card.winfo_children()[1])

    def _gate_all(self):
        for c in self.cards:
            self._gate(c, self.robot_ok)
        if self.robot_ok:
            self.need_lbl.pack_forget()
        elif not self.need_lbl.winfo_ismapped():
            self.need_lbl.pack(fill="x", padx=18, pady=(0, 8), before=self.body)
        if self.robot_ok:
            self._kind_changed()

    def _robot_answered(self, ok: bool):
        if ok != self.robot_ok:
            self.robot_ok = ok
            self._gate_all()

    # --- ports --------------------------------------------------------------------------------------------
    def refresh_ports(self):
        self.port_box.configure(values=[pt.label() for pt in core.list_ports()])

    def use_port(self, kind: str, device: str):
        if kind != "bot":
            return
        changed = self.port_var.get().split(" ", 1)[0] != device
        if core.select_port_in(self.port_box, self.port_var, device) and changed and self.winfo_ismapped():
            self.read_all()

    def _first_look(self):
        if self._looked:
            return
        if self.app.busy:
            self.after(800, self._first_look)
            return
        self._looked = True
        if not self.port_var.get():
            self.find_robot(probe_silent=False)
        else:
            self.read_all()

    def find_robot(self, probe_silent: bool = True):
        if self.app.busy:
            self.port_lbl.configure(text="Busy - try again when the current job is done.", fg=self.pal["WARN"])
            return
        self.port_lbl.configure(text="Looking for the robot (a few seconds)…", fg=self.pal["DIM"])

        def found(f: dict):
            if f.get("bot"):
                self.read_all()
            else:
                self.port_lbl.configure(text="No robot answered. Switch it on (hold the button ~1 s) and try again.",
                                        fg=self.pal["WARN"])
        self.app.run_scan(probe_silent=probe_silent, on_done=found)

    def _port(self) -> str | None:
        port = self.port_var.get().split(" ", 1)[0]
        if not port:
            self.port_lbl.configure(text="Pick the robot's port above, or hit Scan.", fg=self.pal["WARN"])
            return None
        if self.app.port_in_use(port) or self.app.busy:
            self.port_lbl.configure(text=f"{port} is in use (Diagnostics, Console or a flash) - free it first.",
                                    fg=self.pal["WARN"])
            return None
        return port

    def _cmd(self, cmds: list[tuple[str, str]], done, status: tk.Label | None = None):
        """Runs console commands in order on a worker thread; done(list of replies) on the UI thread."""
        if self.working:  # still reading or saving: run this once that's done instead of dropping it
            self.after(300, lambda: self._cmd(cmds, done, status))
            return
        port = self._port()
        if not port:
            return
        self.working = True

        def work():
            out = []
            for c, tag in cmds:
                r = core.console_command(port, c, tag, timeout_s=5)
                problem = core.port_problem(r, port)
                if problem:
                    def fail():
                        self.working = False
                        (status or self.port_lbl).configure(text=problem, fg=self.pal["WARN"])
                        self._robot_answered(False)
                    self.app.ui(fail)
                    return
                out.append(r)

            def ok():
                self.working = False
                done(out)
            self.app.ui(ok)
        threading.Thread(target=work, daemon=True).start()

    # --- reading everything --------------------------------------------------------------------------------
    def read_all(self):
        """Asks the robot for its routines, commands, schedules and clock (then each routine's steps)."""
        if self.app.busy:
            self.after(800, self.read_all)
            return

        def done(r):
            rout, cmds, sched, clock = (r + [None] * 4)[:4]
            if not rout or "max" not in rout:
                self._robot_answered(False)
                self.port_lbl.configure(text="No answer - the robot needs this release's firmware (Flash tab), or "
                                             "it's switched off.", fg=self.pal["WARN"])
                return
            self._robot_answered(True)
            self.port_lbl.configure(text=f"Robot found on {self.port_var.get().split(' ', 1)[0]}.",
                                    fg=self.pal["GREEN"])
            slots = [int(x.split(":", 1)[0]) for x in rout.get("d", "-").split(",") if ":" in x]
            self._commands_reply(cmds)
            self._schedules_reply(sched)
            self._read_routines(slots, clock)
        self._cmd([("rout", "BBROUT"), ("cmds", "BBCMDS"), ("sched", "BBSCHED"), ("clock", "BBCLOCK")], done)

    def _read_routines(self, slots: list[int], clock=None):
        def done(r):
            self.routines = {}
            for i, reply in zip(slots, r):
                text = self._unhex(reply.get("d", "")) if reply else ""
                if text:
                    self.routines[i] = text
            self._fill_routines()
            self._fill_commands()
            self._fill_schedules()
            self._sync_brain()
            self._clock_reply(clock, auto=True)  # may set the clock: after the reads, which it would block
        if slots:
            self._cmd([(f"rout get {i}", "BBROUT") for i in slots], done)
        else:
            done([])

    def _names(self) -> list[str]:
        return [f"{i + 1}. {parse_steps(t)[0]}" for i, t in sorted(self.routines.items())]

    def _routine_label(self, i: int) -> str:
        return f"{i + 1}. {parse_steps(self.routines[i])[0]}" if i in self.routines else f"{i + 1}. (deleted)"

    @staticmethod
    def _slot_of(label: str) -> int | None:
        try:
            return int(label.split(".", 1)[0]) - 1
        except ValueError:
            return None

    # --- routines -----------------------------------------------------------------------------------------
    def _build_routines(self):
        p = self.pal
        card = self.rout_card = self._card(self.left, "MY ROUTINES",
                                           "A routine is a list of steps the robot runs in order: lines to say, "
                                           "its own actions (tell the time, a joke…), driving, pauses, a dance, the LED add-on. Run it from here, with your "
                                           "own voice command or on a schedule. Up to 12, 30 steps each.")
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12)
        left = tk.Frame(row, bg=p["PANEL"])
        left.pack(side="left", fill="y")
        self.rout_list = self._list(left, 12, 24)
        self.rout_list.pack()
        self.rout_list.bind("<<ListboxSelect>>", lambda e: self._pick_routine())
        b = tk.Frame(left, bg=p["PANEL"])
        b.pack(fill="x", pady=(6, 0))
        self.app.button(b, "New routine", self.new_routine, small=True).pack(side="left")
        self.app.button(b, "Delete routine", self.delete_routine, small=True).pack(side="left", padx=6)

        ed = tk.Frame(row, bg=p["PANEL"])
        ed.pack(side="left", fill="both", expand=True, padx=(12, 0))
        nr = tk.Frame(ed, bg=p["PANEL"])
        nr.pack(fill="x")
        self._label(nr, "Name", dim=False).pack(side="left")
        self.name_var = tk.StringVar()
        self._entry(nr, self.name_var, 26).pack(side="left", padx=6)
        self.step_list = self._list(ed, 9, 46)
        self.step_list.pack(fill="x", pady=(6, 0))
        self.step_list.bind("<<ListboxSelect>>", lambda e: self._pick_step())

        # the step editor: a kind, then that kind's settings
        se = tk.Frame(ed, bg=p["PANEL"])
        se.pack(fill="x", pady=(6, 0))
        self.kind_var = tk.StringVar(value=KINDS[0][1])
        kb = self._combo(se, self.kind_var, [k[1] for k in KINDS], 16)
        kb.pack(side="left")
        kb.bind("<<ComboboxSelected>>", lambda e: self._kind_changed())
        self.param = tk.Frame(se, bg=p["PANEL"])
        self.param.pack(side="left", padx=6, fill="x", expand=True)
        self.say_var = tk.StringVar()
        self.dir_var = tk.StringVar(value=DIRECTIONS[0][0])
        self.speed_var = tk.IntVar(value=60)
        self.secs_var = tk.DoubleVar(value=1.0)
        self.led_var = tk.StringVar(value=LED_MODES[0])
        self.action_var = tk.StringVar(value=ACTIONS[0][1])
        sb = tk.Frame(ed, bg=p["PANEL"])
        sb.pack(fill="x", pady=(6, 0))
        for text, cmd in (("Add step", self.add_step), ("Replace step", self.replace_step), ("▲", lambda: self.move_step(-1)),
                          ("▼", lambda: self.move_step(1)), ("Remove step", self.remove_step)):
            self.app.button(sb, text, cmd, small=True).pack(side="left", padx=(0, 5))
        ab = tk.Frame(card, bg=p["PANEL"])
        ab.pack(fill="x", padx=12, pady=(8, 10))
        self.app.button(ab, "Save to robot", self.save_routine, small=True).pack(side="left")
        self.app.button(ab, "▶ Run on robot", self.run_routine, small=True).pack(side="left", padx=6)
        self.rout_lbl = self._label(ab, "")
        self.rout_lbl.pack(side="left", padx=6)
        self._kind_changed()

    def _kind_changed(self):
        for w in self.param.winfo_children():
            w.destroy()
        k = self._kind()
        if k == "S":
            e = self._entry(self.param, self.say_var, 40)
            e.pack(side="left", fill="x", expand=True)
            tooltip.Tooltip(e, "What the robot says (up to 100 characters).")
        elif k == "M":
            self._combo(self.param, self.dir_var, [d[0] for d in DIRECTIONS], 10).pack(side="left")
            self._label(self.param, "speed %").pack(side="left", padx=(6, 2))
            self._spin(self.param, self.speed_var, 20, 100, 4, 10).pack(side="left")
            self._label(self.param, "seconds").pack(side="left", padx=(6, 2))
            self._spin(self.param, self.secs_var, 0.1, 5.0, 4, 0.1, "%.1f").pack(side="left")
        elif k == "W":
            self._label(self.param, "seconds").pack(side="left", padx=(0, 2))
            self._spin(self.param, self.secs_var, 0.1, 10.0, 5, 0.1, "%.1f").pack(side="left")
        elif k == "D":
            self._label(self.param, "seconds").pack(side="left", padx=(0, 2))
            self._spin(self.param, self.secs_var, 2, 30, 4, 1, "%.0f").pack(side="left")
        elif k == "L":
            self._combo(self.param, self.led_var, LED_MODES, 9).pack(side="left")
            self._label(self.param, "(needs the LED add-on module)").pack(side="left", padx=6)
        elif k == "A":
            self._combo(self.param, self.action_var, [a[1] for a in ACTIONS], 19).pack(side="left")
            self._label(self.param, "(needs the newest robot firmware)").pack(side="left", padx=6)
        if not self.robot_ok:
            self._gate(self.rout_card, False)

    def _kind(self) -> str:
        return next(k for k, name in KINDS if name == self.kind_var.get())

    def _step_from_editor(self) -> tuple | None:
        k = self._kind()
        try:
            secs = float(self.secs_var.get())
        except (tk.TclError, ValueError):
            secs = 1.0
        if k == "S":
            text = " ".join(self.say_var.get().split())[:100]
            if not text:
                self.rout_lbl.configure(text="Type what the robot should say.", fg=self.pal["WARN"])
                return None
            return ("S", text)
        if k == "M":
            _, a, b = next(d for d in DIRECTIONS if d[0] == self.dir_var.get())
            s = max(20, min(100, int(self.speed_var.get() or 60)))
            return ("M", round(a * s), round(b * s), int(max(0.1, min(5.0, secs)) * 1000))
        if k == "W":
            return ("W", int(max(0.1, min(10.0, secs)) * 1000))
        if k == "D":
            return ("D", int(max(2, min(30, round(secs)))))
        if k == "A":
            return ("A", next(a for a, name in ACTIONS if name == self.action_var.get()))
        return ("L", self.led_var.get())

    def _fill_steps(self, select: int | None = None):
        self.step_list.delete(0, "end")
        for i, s in enumerate(self.steps):
            self.step_list.insert("end", f"{i + 1:>2}. {describe(s)}")
        if select is not None and 0 <= select < len(self.steps):
            self.step_list.selection_set(select)
            self.step_list.see(select)

    def _pick_step(self):
        sel = self.step_list.curselection()
        if not sel:
            return
        s = self.steps[sel[0]]
        self.kind_var.set(next(name for k, name in KINDS if k == s[0]))
        if s[0] == "S":
            self.say_var.set(s[1])
        elif s[0] == "M":
            d = direction_of(s[1], s[2]) or ("forward", max(abs(s[1]), abs(s[2])))
            self.dir_var.set(d[0])
            self.speed_var.set(d[1])
            self.secs_var.set(s[3] / 1000)
        elif s[0] == "W":
            self.secs_var.set(s[1] / 1000)
        elif s[0] == "D":
            self.secs_var.set(s[1])
        elif s[0] == "L":
            self.led_var.set(s[1])
        elif s[0] == "A":
            self.action_var.set(dict(ACTIONS).get(s[1], ACTIONS[0][1]))
        self._kind_changed()

    def add_step(self):
        if len(self.steps) >= MAX_STEPS:
            self.rout_lbl.configure(text=f"A routine can have up to {MAX_STEPS} steps.", fg=self.pal["WARN"])
            return
        s = self._step_from_editor()
        if s:
            sel = self.step_list.curselection()
            at = sel[0] + 1 if sel else len(self.steps)
            self.steps.insert(at, s)
            self._fill_steps(at)
            self.rout_lbl.configure(text="Not saved yet.", fg=self.pal["DIM"])

    def replace_step(self):
        sel = self.step_list.curselection()
        s = self._step_from_editor()
        if sel and s:
            self.steps[sel[0]] = s
            self._fill_steps(sel[0])
            self.rout_lbl.configure(text="Not saved yet.", fg=self.pal["DIM"])

    def move_step(self, d: int):
        sel = self.step_list.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + d
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self._fill_steps(j)
            self.rout_lbl.configure(text="Not saved yet.", fg=self.pal["DIM"])

    def remove_step(self):
        sel = self.step_list.curselection()
        if sel:
            del self.steps[sel[0]]
            self._fill_steps(min(sel[0], len(self.steps) - 1))
            self.rout_lbl.configure(text="Not saved yet.", fg=self.pal["DIM"])

    def _fill_routines(self):
        self.rout_list.delete(0, "end")
        for i in range(MAX_ROUTINES):
            self.rout_list.insert("end", self._routine_label(i) if i in self.routines else f"{i + 1}.  —")
        if self.sel is not None:
            self.rout_list.selection_set(self.sel)
        names = self._names()
        self.cmd_rout_box.configure(values=names)
        self.sched_rout_box.configure(values=names)

    def _pick_routine(self):
        sel = self.rout_list.curselection()
        if not sel:
            return
        self.sel = sel[0]
        if self.sel in self.routines:
            name, self.steps = parse_steps(self.routines[self.sel])
        else:
            name, self.steps = "", []
        self.name_var.set(name)
        self._fill_steps()
        self.rout_lbl.configure(text="" if self.sel in self.routines else "Empty slot - add steps, then Save.",
                                fg=self.pal["DIM"])

    def new_routine(self):
        free = next((i for i in range(MAX_ROUTINES) if i not in self.routines), None)
        if free is None:
            self.rout_lbl.configure(text="All 12 slots are used - delete one first.", fg=self.pal["WARN"])
            return
        self.sel = free
        self.rout_list.selection_clear(0, "end")
        self.rout_list.selection_set(free)
        name, self.steps = parse_steps(EXAMPLE) if not self.routines else ("", [])
        self.name_var.set(name or f"Routine {free + 1}")
        self._fill_steps()
        self.rout_lbl.configure(text="New routine (an example to start from) - change it, then Save to robot."
                                if not self.routines else "New routine - add steps, then Save to robot.",
                                fg=self.pal["DIM"])

    def save_routine(self):
        if self.sel is None:
            self.rout_lbl.configure(text="Pick a slot on the left (or New routine) first.", fg=self.pal["WARN"])
            return
        name = " ".join(self.name_var.get().split())[:24]
        if not name or not self.steps:
            self.rout_lbl.configure(text="Give it a name and at least one step.", fg=self.pal["WARN"])
            return
        text = build_text(name, self.steps)
        if len(text.encode()) > MAX_TEXT:
            self.rout_lbl.configure(text="Too long for the robot - shorten the lines or use fewer steps.",
                                    fg=self.pal["WARN"])
            return
        i = self.sel
        hexed = text.encode().hex()
        cmds = [(f"rout begin {i}", "BBROUT")] + [("rout put " + hexed[j:j + 180], "BBROUT")
                                                  for j in range(0, len(hexed), 180)] + [("rout save", "BBROUT")]
        self.rout_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])

        def done(r):
            last = r[-1] if r else None
            if not last or last.get("result") != "saved":
                why = (last or {}).get("why", "no answer")
                if why == "space":
                    why = "its settings storage is full - shorten or delete a routine"
                self.rout_lbl.configure(text=f"The robot didn't save it ({why}).", fg=self.pal["WARN"])
                return
            self.routines[i] = text
            self._fill_routines()
            self._fill_commands()
            self._fill_schedules()
            self._sync_brain()
            self.rout_lbl.configure(text=f"Saved: {last.get('steps')} steps.", fg=self.pal["GREEN"])
        self._cmd(cmds, done)

    def delete_routine(self):
        i = self.sel
        if i is None or i not in self.routines:
            return
        name = parse_steps(self.routines[i])[0]
        users = [c for c in self.commands if c[0] == i]
        timed = [s for s in self.schedules if s[2] == i]
        extra = (f" Its {len(users)} voice command{'s' if len(users) != 1 else ''} and {len(timed)} "
                 f"schedule{'s' if len(timed) != 1 else ''} go too.") if users or timed else ""
        if not self.app.confirm("Delete routine?", f"Delete \"{name}\" from the robot?{extra}", ok="Delete",
                                kind="warning"):
            return
        cmds = [(f"rout begin {i}", "BBROUT"), ("rout save", "BBROUT")]
        # Voice commands / schedules that ran it: deleted from the end, so the indexes stay right
        cmds += [(f"cmds del {k}", "BBCMDS") for k in reversed(range(len(self.commands))) if self.commands[k][0] == i]
        cmds += [(f"sched del {k}", "BBSCHED") for k in reversed(range(len(self.schedules))) if self.schedules[k][2] == i]

        def done(r):
            self.routines.pop(i, None)
            self.steps = []
            self.name_var.set("")
            self.sel = None
            self.read_all()
            self.rout_lbl.configure(text=f"Deleted \"{name}\".", fg=self.pal["GREEN"])
        self._cmd(cmds, done)

    def run_routine(self):
        i = self.sel
        if i is None or i not in self.routines:
            self.rout_lbl.configure(text="Save the routine to the robot first.", fg=self.pal["WARN"])
            return
        if build_text(" ".join(self.name_var.get().split())[:24], self.steps) != self.routines[i]:
            self.rout_lbl.configure(text="Running the saved version - Save to robot to run your changes.",
                                    fg=self.pal["WARN"])

        def done(r):
            res = r[0] or {}
            if res.get("result") == "queued":
                self.rout_lbl.configure(text="Running on the robot (press its button to stop it).",
                                        fg=self.pal["GREEN"])
            elif res.get("result") == "busy":
                self.rout_lbl.configure(text=f"The robot is busy ({res.get('state', '?')}) - try again when it's "
                                             "idle.", fg=self.pal["WARN"])
            else:
                self.rout_lbl.configure(text="The robot didn't start it.", fg=self.pal["WARN"])
        self._cmd([(f"rout run {i}", "BBROUT")], done)

    # --- voice commands -----------------------------------------------------------------------------------
    def _build_commands(self):
        p = self.pal
        card = self.cmd_card = self._card(self.right, "VOICE COMMANDS",
                                          "Your own phrases that run a routine. Say them like the built-in "
                                          "commands - press the robot's button (or the controller's) and talk. "
                                          "They work in AI conversations too. Up to 12.")
        self.cmd_list = self._list(card, 5, 62)
        self.cmd_list.pack(fill="x", padx=12)
        self.cmd_list.bind("<<ListboxSelect>>", lambda e: self._pick_command())
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(6, 0))
        self._label(row, "When I say", dim=False).pack(side="left")
        self.phrase_var = tk.StringVar()
        e = self._entry(row, self.phrase_var, 24)
        e.pack(side="left", padx=6)
        self.phrase_var.trace_add("write", lambda *_: self._phrase_preview())
        self._label(row, "run", dim=False).pack(side="left")
        self.cmd_rout_var = tk.StringVar()
        self.cmd_rout_box = self._combo(row, self.cmd_rout_var, [], 18)
        self.cmd_rout_box.pack(side="left", padx=6)
        self.phon_lbl = self._label(card, "", anchor="w", justify="left", wraplength=500)
        self.phon_lbl.pack(fill="x", padx=12, pady=(4, 0))
        b = tk.Frame(card, bg=p["PANEL"])
        b.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(b, "Add command", self.add_command, small=True).pack(side="left")
        self.app.button(b, "Update command", self.update_command, small=True).pack(side="left", padx=6)
        self.app.button(b, "Delete command", self.delete_command, small=True).pack(side="left")
        self.app.button(b, "🎤 Teach by voice", self.teach_by_voice, small=True).pack(side="left", padx=6)
        self.cmd_lbl = self._label(b, "")
        self.cmd_lbl.pack(side="left", padx=8)

    def _phrase_preview(self) -> str | None:
        text = self.phrase_var.get()
        if not text.strip():
            self.phon_lbl.configure(text="")
            return None
        taught = self.taught.get(" ".join(text.lower().split()))
        if taught:
            self.phon_lbl.configure(text=f"The robot listens for: {taught}   ·   taught by voice (your own way of "
                                         "saying it)", fg=self.pal["GREEN"])
            return taught
        try:
            ph, guessed = phonemes.to_phonemes(text)
        except ValueError as e:
            self.phon_lbl.configure(text=f"Can't use this: {e}.", fg=self.pal["WARN"])
            return None
        note = f"The robot listens for: {ph}"
        if guessed:
            note += (f"   ·   guessed from the spelling: {', '.join(guessed)} - if it's not recognised, try "
                     "🎤 Teach by voice")
        self.phon_lbl.configure(text=note, fg=self.pal["WARN"] if guessed else self.pal["DIM"])
        return ph

    def _commands_reply(self, r):
        if not r or "max" not in r:
            return
        self.commands = []
        for item in r.get("d", "-").split(","):
            parts = item.split(":")
            if len(parts) == 3:
                self.commands.append((int(parts[0]), self._unhex(parts[1]), self._unhex(parts[2])))
        self._fill_commands()

    def _fill_commands(self):
        self.cmd_list.delete(0, "end")
        for routine, phrase, _ in self.commands:
            self.cmd_list.insert("end", f'"{phrase}"  →  {self._routine_label(routine)}')
        if not self.commands:
            self.cmd_list.insert("end", "(none yet - type a phrase below, pick a routine to run, then Add command)")

    def _pick_command(self):
        sel = self.cmd_list.curselection()
        if not sel or sel[0] >= len(self.commands):
            return
        routine, phrase, _ = self.commands[sel[0]]
        self.phrase_var.set(phrase)
        if routine in self.routines:
            self.cmd_rout_var.set(self._routine_label(routine))

    def _command_args(self) -> tuple[int, str, str] | None:
        phrase = " ".join(self.phrase_var.get().lower().split())
        ph = self._phrase_preview()
        routine = self._slot_of(self.cmd_rout_var.get())
        if not phrase or ph is None:
            self.cmd_lbl.configure(text="Type a phrase (a few words).", fg=self.pal["WARN"])
            return None
        if len(phrase) > 40:
            self.cmd_lbl.configure(text="Keep the phrase under 40 characters.", fg=self.pal["WARN"])
            return None
        if routine is None or routine not in self.routines:
            self.cmd_lbl.configure(text="Pick the routine it runs.", fg=self.pal["WARN"])
            return None
        return routine, phrase, ph

    def _save_command(self, index: int, args):
        routine, phrase, ph = args

        def done(r):
            reply = r[-1] or {}
            self._commands_reply(reply)
            self._sync_brain()
            ok = any(c[1] == phrase for c in self.commands)
            self.cmd_lbl.configure(text="Saved on the robot." if ok else "The robot didn't take it.",
                                   fg=self.pal["GREEN"] if ok else self.pal["WARN"])
        self._cmd([(f"cmds set {index} {routine} {self._hex(phrase)} {self._hex(ph)}", "BBCMDS")], done)

    def add_command(self):
        args = self._command_args()
        if not args:
            return
        if any(c[1] == args[1] for c in self.commands):
            self.cmd_lbl.configure(text="You already have that phrase - select it and Update.", fg=self.pal["WARN"])
            return
        if len(self.commands) >= 12:
            self.cmd_lbl.configure(text="12 commands is the most - delete one first.", fg=self.pal["WARN"])
            return
        self._save_command(len(self.commands), args)

    def update_command(self):
        sel = self.cmd_list.curselection()
        args = self._command_args()
        if not sel or sel[0] >= len(self.commands):
            self.cmd_lbl.configure(text="Select the command to change in the list.", fg=self.pal["WARN"])
            return
        if args:
            self._save_command(sel[0], args)

    def delete_command(self):
        sel = self.cmd_list.curselection()
        if not sel or sel[0] >= len(self.commands):
            return

        def done(r):
            self._commands_reply(r[-1])
            self._sync_brain()
            self.cmd_lbl.configure(text="Deleted.", fg=self.pal["GREEN"])
        self._cmd([(f"cmds del {sel[0]}", "BBCMDS")], done)

    # --- teaching a phrase by voice ---------------------------------------------------------------------------
    TEACH_TRIES = 3

    def teach_by_voice(self):
        """The robot listens a few times while the owner says the phrase; what its recogniser heard (the most
        consistent take) replaces the spelling-based guess. Good for names and words the dictionary doesn't know."""
        phrase = " ".join(self.phrase_var.get().lower().split())
        if not phrase:
            self.cmd_lbl.configure(text="Type the phrase first, then teach it.", fg=self.pal["WARN"])
            return
        port = self._port()
        if not port or self.working:
            return
        self.working = True
        say = lambda text, color="DIM": self.app.ui(lambda: self.cmd_lbl.configure(text=text, fg=self.pal[color]))
        listen = "ctrl " + controller_link.command("Listen").hex()

        def heard_in(lines: list[str]) -> tuple[str, str]:
            """(transcript, what it matched) from one listen's lines."""
            transcript, matched = "", ""
            for line in lines:
                if m := TIMEOUT_RE.search(line):
                    transcript = transcript or m.group(1)
                elif m := (FUZZY_OK_RE.search(line) or FUZZY_NO_RE.search(line)):
                    transcript = m.group(1)
                elif m := TOP_RE.search(line):
                    matched = m.group(2).strip()
            return transcript.strip(), matched

        def work():
            takes, matched = [], []
            wifi = core.console_command(port, "wifi", "BBWIFI", timeout_s=4) or {}
            ai_was_on = wifi.get("ai") == "on"
            if ai_was_on:  # the robot's own recogniser has to do the listening, not the AI
                core.console_command(port, "ai off", "BBWIFI", timeout_s=4)
            try:
                for i in range(self.TEACH_TRIES):
                    say(f'Listening {i + 1}/{self.TEACH_TRIES}: say "{phrase}" now…', "LIME")
                    lines = core.console_watch(port, listen, lambda l: bool(TIMEOUT_RE.search(l) or TOP_RE.search(l)
                                                                             or FUZZY_AMB_RE.search(l)), 12)
                    if lines is None:
                        say(f"{port} went away or is busy.", "WARN")
                        return
                    t, m = heard_in(lines)
                    if t:
                        takes.append(t)
                    elif m:
                        matched.append(m)
                    time.sleep(2.5)  # the robot answers / goes back to idle
            finally:
                if ai_was_on:
                    core.console_command(port, "ai on", "BBWIFI", timeout_s=4)

                def end():
                    self.working = False
                    self._taught(phrase, takes, matched)
                self.app.ui(end)
        threading.Thread(target=work, daemon=True).start()

    def _taught(self, phrase: str, takes: list[str], matched: list[str]):
        if not takes:
            if matched:
                self.cmd_lbl.configure(text=f'That sounds like "{matched[0]}", which the robot already knows - pick a '
                                            "more different phrase.", fg=self.pal["WARN"])
            else:
                self.cmd_lbl.configure(text="The robot didn't hear anything. Speak right after its listening sound, "
                                            "close to it.", fg=self.pal["WARN"])
            return
        # The take closest to all the others: one odd mishearing doesn't win
        best = min(takes, key=lambda t: sum(edit_distance(t, o) for o in takes))
        self.taught[phrase] = best[:phonemes.MAX_LEN]
        self._phrase_preview()
        self.cmd_lbl.configure(text=f"Learned from {len(takes)} take{'s' if len(takes) > 1 else ''} - now Add (or "
                                    "Update) the command to save it.", fg=self.pal["GREEN"])

    def _sync_brain(self):
        """The AI brain offers the voice commands as actions: keep its list (in the settings) in step."""
        cmds = [{"phrase": phrase, "what": f'runs the owner\'s own routine "{parse_steps(self.routines[r])[0]}" '
                                           "(their own lines and moves)"}
                for r, phrase, _ in self.commands if r in self.routines]
        if cmds != self.app.cfg.get("voice_commands", []):
            self.app.cfg["voice_commands"] = cmds
            core.save_config(self.app.cfg)
            self.app.ai_tab.personality_changed()

    # --- schedule -----------------------------------------------------------------------------------------
    def _build_schedule(self):
        p = self.pal
        card = self.sched_card = self._card(self.right, "SCHEDULE",
                                            "Run a routine at a time of day - \"every weekday at 7:30, Good "
                                            "morning\". The robot needs to be switched on, and its clock set "
                                            "(below). Up to 12.")
        self.sched_list = self._list(card, 5, 62)
        self.sched_list.pack(fill="x", padx=12)
        self.sched_list.bind("<<ListboxSelect>>", lambda e: self._pick_schedule())
        days = tk.Frame(card, bg=p["PANEL"])
        days.pack(fill="x", padx=12, pady=(6, 0))
        self.day_vars = {d: tk.BooleanVar(value=d not in ("Sat", "Sun")) for d in DAYS}
        for d in DAYS:
            tk.Checkbutton(days, text=d, variable=self.day_vars[d], bg=p["PANEL"], fg=p["TEXT"],
                           selectcolor=p["PANEL_HI"], activebackground=p["PANEL"], activeforeground=p["GREEN"],
                           highlightthickness=0, font=("Segoe UI", 8)).pack(side="left")
        for text, bits in (("Every day", 0x7f), ("Weekdays", 0b0111110), ("Weekends", 0b1000001)):
            self.app.button(days, text, lambda b=bits: self._set_days(b), small=True).pack(side="left", padx=(4, 0))
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(6, 0))
        self._label(row, "at", dim=False).pack(side="left")
        self.hour_var = tk.StringVar(value="07")
        self.min_var = tk.StringVar(value="30")
        self._spin(row, self.hour_var, 0, 23, 3, 1, "%02.0f").pack(side="left", padx=(6, 0))
        self._label(row, ":", dim=False).pack(side="left")
        self._spin(row, self.min_var, 0, 59, 3, 5, "%02.0f").pack(side="left")
        self._label(row, "run", dim=False).pack(side="left", padx=(10, 0))
        self.sched_rout_var = tk.StringVar()
        self.sched_rout_box = self._combo(row, self.sched_rout_var, [], 18)
        self.sched_rout_box.pack(side="left", padx=6)
        self.sched_on_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="On", variable=self.sched_on_var, bg=p["PANEL"], fg=p["TEXT"],
                       selectcolor=p["PANEL_HI"], activebackground=p["PANEL"], activeforeground=p["GREEN"],
                       highlightthickness=0, font=("Segoe UI", 9)).pack(side="left")
        b = tk.Frame(card, bg=p["PANEL"])
        b.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(b, "Add schedule", self.add_schedule, small=True).pack(side="left")
        self.app.button(b, "Update schedule", self.update_schedule, small=True).pack(side="left", padx=6)
        self.app.button(b, "Delete schedule", self.delete_schedule, small=True).pack(side="left")
        self.sched_lbl = self._label(b, "")
        self.sched_lbl.pack(side="left", padx=8)

    def _set_days(self, bits: int):
        for d in DAYS:
            self.day_vars[d].set(bool(bits & (1 << DAY_BIT[d])))

    def _schedules_reply(self, r):
        if not r or "max" not in r:
            return
        self.schedules = []
        for item in r.get("d", "-").split(","):
            parts = item.split(":")
            if len(parts) == 4:
                self.schedules.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3] == "1"))
        q = r.get("quiet", "0:1320:420").split(":")
        if len(q) == 3:
            self.quiet_var.set(q[0] == "1")
            for var, minutes in ((self.q_from_h, q[1]), (self.q_to_h, q[2])):
                var.set(f"{int(minutes) // 60:02d}")
            self.q_from_m.set(f"{int(q[1]) % 60:02d}")
            self.q_to_m.set(f"{int(q[2]) % 60:02d}")
        self._fill_schedules()

    def _fill_schedules(self):
        self.sched_list.delete(0, "end")
        for days, minute, routine, on in self.schedules:
            self.sched_list.insert("end", f"{days_text(days)}  {minute // 60:02d}:{minute % 60:02d}  →  "
                                          f"{self._routine_label(routine)}" + ("" if on else "   (off)"))
        if not self.schedules:
            self.sched_list.insert("end", "(none yet - pick days, a time and a routine below, then Add schedule)")

    def _pick_schedule(self):
        sel = self.sched_list.curselection()
        if not sel or sel[0] >= len(self.schedules):
            return
        days, minute, routine, on = self.schedules[sel[0]]
        self._set_days(days)
        self.hour_var.set(f"{minute // 60:02d}")
        self.min_var.set(f"{minute % 60:02d}")
        self.sched_on_var.set(on)
        if routine in self.routines:
            self.sched_rout_var.set(self._routine_label(routine))

    def _schedule_args(self) -> tuple[int, int, int, int] | None:
        bits = sum(1 << DAY_BIT[d] for d in DAYS if self.day_vars[d].get())
        try:
            minute = int(self.hour_var.get()) * 60 + int(self.min_var.get())
        except ValueError:
            minute = -1
        routine = self._slot_of(self.sched_rout_var.get())
        if not bits:
            self.sched_lbl.configure(text="Tick at least one day.", fg=self.pal["WARN"])
            return None
        if not 0 <= minute < 24 * 60:
            self.sched_lbl.configure(text="Check the time.", fg=self.pal["WARN"])
            return None
        if routine is None or routine not in self.routines:
            self.sched_lbl.configure(text="Pick the routine it runs.", fg=self.pal["WARN"])
            return None
        return bits, minute, routine, 1 if self.sched_on_var.get() else 0

    def _save_schedule(self, index: int):
        args = self._schedule_args()
        if not args:
            return

        def done(r):
            self._schedules_reply(r[-1])
            bad = (r[-1] or {}).get("result") == "error"
            self.sched_lbl.configure(text="The robot didn't take it." if bad else self._clock_warning() or "Saved on the robot.",
                                     fg=self.pal["WARN"] if bad or self._clock_warning() else self.pal["GREEN"])
        self._cmd([(f"sched set {index} {' '.join(str(a) for a in args)}", "BBSCHED")], done)

    def add_schedule(self):
        if len(self.schedules) >= 12:
            self.sched_lbl.configure(text="12 schedules is the most - delete one first.", fg=self.pal["WARN"])
            return
        self._save_schedule(len(self.schedules))

    def update_schedule(self):
        sel = self.sched_list.curselection()
        if not sel or sel[0] >= len(self.schedules):
            self.sched_lbl.configure(text="Select the schedule to change in the list.", fg=self.pal["WARN"])
            return
        self._save_schedule(sel[0])

    def delete_schedule(self):
        sel = self.sched_list.curselection()
        if not sel or sel[0] >= len(self.schedules):
            return

        def done(r):
            self._schedules_reply(r[-1])
            self.sched_lbl.configure(text="Deleted.", fg=self.pal["GREEN"])
        self._cmd([(f"sched del {sel[0]}", "BBSCHED")], done)

    # --- quiet hours --------------------------------------------------------------------------------------
    def _build_quiet(self):
        p = self.pal
        card = self.quiet_card = self._card(self.right, "QUIET HOURS",
                                            "No idle muttering, wandering or looking around in this window (it can "
                                            "cross midnight). It still answers you, and schedules still run.")
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(0, 10))
        self.quiet_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="Quiet from", variable=self.quiet_var, bg=p["PANEL"], fg=p["TEXT"],
                       selectcolor=p["PANEL_HI"], activebackground=p["PANEL"], activeforeground=p["GREEN"],
                       highlightthickness=0, font=("Segoe UI", 9)).pack(side="left")
        self.q_from_h, self.q_from_m = tk.StringVar(value="22"), tk.StringVar(value="00")
        self.q_to_h, self.q_to_m = tk.StringVar(value="07"), tk.StringVar(value="00")
        for i, (h, m) in enumerate(((self.q_from_h, self.q_from_m), (self.q_to_h, self.q_to_m))):
            if i:
                self._label(row, "to", dim=False).pack(side="left", padx=6)
            self._spin(row, h, 0, 23, 3, 1, "%02.0f").pack(side="left", padx=(4, 0))
            self._label(row, ":", dim=False).pack(side="left")
            self._spin(row, m, 0, 59, 3, 15, "%02.0f").pack(side="left")
        self.app.button(row, "Save quiet hours", self.save_quiet, small=True).pack(side="left", padx=10)
        self.quiet_lbl = self._label(row, "")
        self.quiet_lbl.pack(side="left")

    def save_quiet(self):
        try:
            start = int(self.q_from_h.get()) * 60 + int(self.q_from_m.get())
            end = int(self.q_to_h.get()) * 60 + int(self.q_to_m.get())
        except ValueError:
            self.quiet_lbl.configure(text="Check the times.", fg=self.pal["WARN"])
            return
        if not (0 <= start < 1440 and 0 <= end < 1440):
            self.quiet_lbl.configure(text="Check the times.", fg=self.pal["WARN"])
            return

        def done(r):
            self._schedules_reply(r[-1])
            warn = self._clock_warning()
            self.quiet_lbl.configure(text=warn or "Saved.", fg=self.pal["WARN"] if warn else self.pal["GREEN"])
        self._cmd([(f"quiet {1 if self.quiet_var.get() else 0} {start} {end}", "BBSCHED")], done)

    # --- clock --------------------------------------------------------------------------------------------
    def _build_clock(self):
        p = self.pal
        card = self.clock_card = self._card(self.left, "ROBOT CLOCK",
                                            "Schedules, quiet hours and \"what time is it?\" use the robot's clock. "
                                            "It keeps time while switched off, but drifts - Dimension Hopper sets "
                                            "it from this PC whenever it finds the robot (untick to stop that). "
                                            "For testing, set any date and time you like.")
        g = tk.Frame(card, bg=p["PANEL"])
        g.pack(fill="x", padx=12)
        self._label(g, "Robot", dim=False, width=7, anchor="w").grid(row=0, column=0, sticky="w")
        self.robot_time_lbl = tk.Label(g, text="unknown", bg=p["PANEL"], fg=p["TEXT"], font=("Cascadia Mono", 10))
        self.robot_time_lbl.grid(row=0, column=1, sticky="w")
        self._label(g, "This PC", dim=False, width=7, anchor="w").grid(row=1, column=0, sticky="w")
        self.pc_time_lbl = tk.Label(g, text="", bg=p["PANEL"], fg=p["DIM"], font=("Cascadia Mono", 10))
        self.pc_time_lbl.grid(row=1, column=1, sticky="w")
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(8, 0))
        self._label(row, "Set to", dim=False).pack(side="left")
        self.set_var = tk.StringVar(value=time.strftime("%Y-%m-%d %H:%M:%S"))
        e = self._entry(row, self.set_var, 20)
        e.pack(side="left", padx=6)
        e.bind("<Key>", lambda ev: setattr(self, "clock_edited", True))
        tooltip.Tooltip(e, "Year-month-day hours:minutes:seconds. It follows this PC's time until you type in it.")
        self.app.button(row, "Set this time", self.set_custom_time, small=True).pack(side="left")
        self.app.button(row, "Use PC time", self.use_pc_time, small=True).pack(side="left", padx=6)
        row2 = tk.Frame(card, bg=p["PANEL"])
        row2.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(row2, "Sync to PC time", self.sync_clock, small=True).pack(side="left")
        self.auto_var = tk.BooleanVar(value=bool(self.app.cfg.get("clock_autosync", True)))
        tk.Checkbutton(row2, text="Keep it in sync automatically", variable=self.auto_var, command=self._save_auto,
                       bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"], activebackground=p["PANEL"],
                       activeforeground=p["GREEN"], highlightthickness=0, font=("Segoe UI", 9)).pack(side="left",
                                                                                                    padx=10)
        self.clock_lbl = self._label(card, "", anchor="w")
        self.clock_lbl.pack(fill="x", padx=12, pady=(0, 10))

    def _save_auto(self):
        self.app.cfg["clock_autosync"] = self.auto_var.get()
        core.save_config(self.app.cfg)

    def _clock_tick(self):
        now = pc_now()
        self.pc_time_lbl.configure(text=time.strftime("%a %d %b %Y  %H:%M:%S"))
        if self.clock_offset is not None:
            self.robot_time_lbl.configure(text=time.strftime("%a %d %b %Y  %H:%M:%S",
                                                             time.gmtime(now + self.clock_offset)))
        if not self.clock_edited:
            self.set_var.set(time.strftime("%Y-%m-%d %H:%M:%S"))
        self.after(1000, self._clock_tick)

    def _clock_warning(self) -> str:
        if self.clock_offset is None:
            return "Note: the robot's clock isn't set - sync it below, or this won't run on time."
        return ""

    def _clock_reply(self, r, auto: bool = False):
        if not r or "t" not in r:
            return
        if r.get("set") != "1":
            self.clock_offset = None
            self.robot_time_lbl.configure(text="not set", fg=self.pal["WARN"])
        else:
            self.clock_offset = int(r["t"]) - pc_now()
            self.robot_time_lbl.configure(fg=self.pal["TEXT"])
        off = self.clock_offset
        if auto and self.auto_var.get() and not self.custom_time and (off is None or abs(off) > 30):
            self.sync_clock(quiet=True)
            return
        if off is None:
            self.clock_lbl.configure(text="The clock isn't set yet.", fg=self.pal["WARN"])
        elif abs(off) <= 2:
            self.clock_lbl.configure(text="Matches this PC.", fg=self.pal["GREEN"])
        else:
            self.clock_lbl.configure(text=f"{abs(off)} s {'ahead of' if off > 0 else 'behind'} this PC.",
                                     fg=self.pal["DIM"])

    def _set_clock(self, t: int, what: str):
        def done(r):
            reply = r[0] or {}
            if "t" not in reply:
                self.clock_lbl.configure(text="The robot didn't take the time.", fg=self.pal["WARN"])
                return
            self._clock_reply(reply)
            self.clock_lbl.configure(text=what, fg=self.pal["GREEN"])
        if self.working:  # e.g. during the first read: try again in a moment
            self.after(500, lambda: self._set_clock(t, what))
            return
        self._cmd([(f"clock set {t}", "BBCLOCK")], done)

    def sync_clock(self, quiet: bool = False):
        self.custom_time = False
        self.clock_edited = False
        self._set_clock(pc_now(), "Synced to this PC's time." if not quiet else
                        "Synced to this PC's time (it was off - this happens now and then).")

    def use_pc_time(self):
        self.clock_edited = False
        self.set_var.set(time.strftime("%Y-%m-%d %H:%M:%S"))

    def set_custom_time(self):
        try:
            t = calendar.timegm(time.strptime(self.set_var.get().strip(), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            self.clock_lbl.configure(text="Type it as 2026-11-15 08:30:00.", fg=self.pal["WARN"])
            return
        if not 1704067200 <= t <= 4102444800:
            self.clock_lbl.configure(text="Pick a date between 2024 and 2100.", fg=self.pal["WARN"])
            return
        self.custom_time = abs(t - pc_now()) > 5
        self._set_clock(t, "Set. Automatic sync is paused until you press Sync to PC time (or reopen the app)."
                        if self.custom_time else "Set.")
