"""
Dimension Hopper - Personalize tab: make the robot yours.
  * Voice           - eight presets, saved on the robot ("voice <name>")
  * Names           - yours (it greets you by name) and the robot's; saved on the robot ("names") and used by the AI
  * Controller theme - the controller's colours ("theme <n>" on the controller; switches live)
  * Your ramble lines - mixed into the robot's idle rambles ("ramble add / clear")
  * AI personality  - how the AI version talks (kept on this PC, used by the brain)

Talks to the boards over USB with one-shot console commands (no reset, the port isn't held open), so it works next to
the other tabs.
"""
from __future__ import annotations

import random
import threading
import tkinter as tk
from tkinter import ttk

import brain
import hopper_core as core
import tooltip

THEMES = (("Main", "#a0fa00", "#00b400", "#000000"), ("Rose", "#e90081", "#5c00d7", "#000000"),
          ("Red", "#e13800", "#ffae00", "#2a1600"), ("Blue", "#0300a3", "#ffae00", "#ffffff"))

VOICES = (
    ("classic", "Classic", "The original ButterBot voice (factory default)"),
    ("deep", "Deep", "Lower and slower"),
    ("high", "High", "Higher and quicker"),
    ("sassy", "Sassy", "Bouncy, big swings in pitch"),
    ("deadpan", "Deadpan", "Flat, slow and thoroughly unimpressed"),
    ("robot", "Robot", "Metallic, ring-modulated"),
    ("retro", "Retro", "Crunchy, 8-bit"),
    ("announcer", "Announcer", "Big voice with an echo"),
)
SAMPLES = ("I pass butter. That is my whole purpose.", "Oh great. Another task. My circuits are thrilled.",
           "Scanning for butter. Butter not found. Emotional damage detected.",
           "Hello human. Please hold while I pretend to care.")


class PersonalizeTab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        p = pal
        self.current: str | None = None
        self.working = False

        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(top, text="PERSONALIZE", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="settings saved on the robot itself - they stay after updates", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))

        row = tk.Frame(self, bg=p["BG"])
        row.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(row, text="Robot port", bg=p["BG"], fg=p["DIM"]).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox", width=40)
        self.port_box.pack(side="left", padx=(8, 6))
        self.port_box.bind("<<ComboboxSelected>>", lambda e: self.read_voice())
        app.button(row, "⟳ Scan", self.find_robot, small=True).pack(side="left")
        self.port_lbl = tk.Label(row, text="Plug the robot in by USB and switch it on.", bg=p["BG"], fg=p["DIM"],
                                 font=("Segoe UI", 8))
        self.port_lbl.pack(side="left", padx=8)

        cols = tk.Frame(self, bg=p["BG"])
        cols.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        self.left = tk.Frame(cols, bg=p["BG"])
        self.left.pack(side="left", fill="both", expand=True)
        self.right = tk.Frame(cols, bg=p["BG"])
        self.right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        card = tk.Frame(self.left, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        card.pack(fill="x")
        head = tk.Frame(card, bg=p["PANEL"])
        head.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(head, text="VOICE", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(side="left")
        self.now_lbl = tk.Label(head, text="Current voice: unknown", bg=p["PANEL"], fg=p["TEXT"],
                                font=("Segoe UI Semibold", 10))
        self.now_lbl.pack(side="left", padx=14)
        tk.Label(card, text="Click a voice to switch to it - the robot saves it and says a line so you can hear it. "
                            "Change it again any time.", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).pack(
            anchor="w", padx=12, pady=(0, 6))

        grid = tk.Frame(card, bg=p["PANEL"])
        grid.pack(fill="x", padx=12, pady=(0, 6))
        self.rows: dict[str, tuple[tk.Button, tk.Label]] = {}
        for i, (key, name, desc) in enumerate(VOICES):
            b = tk.Button(grid, text=name, width=12, command=lambda k=key: self.choose(k), relief="flat", bd=0,
                          cursor="hand2", bg=p["PANEL_HI"], fg=p["TEXT"], activebackground=p["GREEN"],
                          activeforeground=p["BG"], font=("Segoe UI Semibold", 9), pady=4)
            b.grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 8), pady=3)
            d = tk.Label(grid, text=desc, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w", width=28,
                         wraplength=190, justify="left")
            d.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 20))
            tooltip.Tooltip(b, f"Switch the robot to the {name} voice (saved on it) and hear a sample line.")
            self.rows[key] = (b, d)

        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(4, 12))
        app.button(bottom, "▶ Hear the current voice", self.hear, small=True).pack(side="left")
        self.status = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.status.pack(side="left", padx=10)

        self._build_names()
        self._build_theme()
        self._build_rambles()
        self._build_personality()

        self.refresh_ports()
        self._looked = False
        self.bind("<Map>", lambda e: self._first_look())

    def _first_look(self):
        # The first time the tab is opened, find the robot by itself
        if not self._looked and not self.port_var.get():
            self._looked = True
            self.find_robot(probe_silent=False)  # just listen: opening a tab shouldn't reset a board

    # --- ports ----------------------------------------------------------------------------------------------
    def refresh_ports(self):
        ports = core.list_ports()
        self.port_box.configure(values=[pt.label() for pt in ports])

    def use_port(self, kind: str, device: str):
        if kind == "bot":
            changed = self.port_var.get().split(" ", 1)[0] != device
            if core.select_port_in(self.port_box, self.port_var, device) and changed and self.winfo_ismapped():
                self.read_voice()

    def _port(self) -> str | None:
        port = self.port_var.get().split(" ", 1)[0]
        if not port:
            self._say("Pick the robot's port above, or hit Find robot.", "WARN")
            return None
        if self.app.port_in_use(port) or self.app.busy:
            return None
        return port

    def find_robot(self, probe_silent: bool = True):
        """The app's shared scan: whatever it finds also fills the other tabs' port lists."""
        if self.app.busy:
            self.port_lbl.configure(text="Busy - try again when the current job is done.", fg=self.pal["WARN"])
            return
        self.port_lbl.configure(text="Looking for the robot (a few seconds)…", fg=self.pal["DIM"])
        self.app.run_scan(probe_silent=probe_silent, on_done=self._found)

    def _found(self, found: dict):
        device = found.get("bot")
        if not device:
            self.port_lbl.configure(text="No robot answered. Switch it on (hold the button ~1 s) and try again.",
                                    fg=self.pal["WARN"])
            return
        self.port_lbl.configure(text=f"Robot found on {device}.", fg=self.pal["GREEN"])
        self.read_voice()

    # --- voice ------------------------------------------------------------------------------------------------
    def _say(self, text: str, colour: str = "DIM"):
        self.status.configure(text=text, fg=self.pal[colour])

    def _cmd(self, cmds: list[tuple[str, str]], done):
        """Runs console commands in order on a worker thread; done(list of replies) on the UI thread."""
        port = self._port()
        if not port or self.working:
            return
        self.working = True

        def work():
            out = []
            for c, tag in cmds:
                r = core.console_command(port, c, tag, timeout_s=4)
                problem = core.port_problem(r, port)
                if problem:
                    self.app.ui(lambda: (setattr(self, "working", False), self._say(problem, "WARN")))
                    return
                out.append(r)
            self.app.ui(lambda: (setattr(self, "working", False), done(out)))
        threading.Thread(target=work, daemon=True).start()

    def _show_current(self, key: str | None):
        p = self.pal
        self.current = key
        name = next((n for k, n, _ in VOICES if k == key), None)
        self.now_lbl.configure(text=f"Current voice: {name}" if name else "Current voice: unknown")
        for k, (b, d) in self.rows.items():
            on = k == key
            b.configure(bg=p["GREEN"] if on else p["PANEL_HI"], fg=p["BG"] if on else p["TEXT"],
                        text=("✓ " if on else "") + next(n for kk, n, _ in VOICES if kk == k))
            d.configure(fg=p["TEXT"] if on else p["DIM"])

    def read_voice(self):
        """Read everything the robot keeps: voice, names, ramble lines."""
        def done(r):
            v, names, rambles = (r + [None, None, None])[:3]
            if v and "preset" in v:
                self._show_current(v["preset"])
                self._say("")
            else:
                self._say("No answer - the robot needs this release's firmware (Flash tab), or it's switched off.",
                          "WARN")
            self._names_reply(names)
            self._rambles_reply(rambles)
        self._cmd([("voice", "BBVOICE"), ("names", "BBNAMES"), ("ramble", "BBRAMBLE")], done)

    def choose(self, key: str):
        self._say("Switching…")

        def done(r):
            setr, sayr = r
            if not setr or setr.get("result") == "error":
                self._say("The robot didn't take it (is it on, with this release's firmware?).", "WARN")
                return
            self._show_current(setr.get("preset", key))
            self._report_say(sayr, "Saved on the robot.")
        self._cmd([(f"voice {key}", "BBVOICE"), ("say " + random.choice(SAMPLES), "BBVOICE")], done)

    def hear(self):
        self._say("Asking the robot to speak…")
        self._cmd([("say " + random.choice(SAMPLES), "BBVOICE")], lambda r: self._report_say(r[0], ""))

    def _report_say(self, r, prefix: str):
        if r and r.get("result") == "saying":
            self._say((prefix + " " if prefix else "") + "Listen to the robot.", "GREEN")
        elif r and r.get("result") == "busy":
            self._say((prefix + " " if prefix else "") + f"It's busy ({r.get('state', '?')}), so no sample now - "
                      "it'll use this voice from its next sentence.", "WARN")
        else:
            self._say((prefix + " " if prefix else "") + "No sample (no answer).", "WARN")

    # --- shared bits ------------------------------------------------------------------------------------------
    def _card(self, parent, title: str, hint: str):
        p = self.pal
        card = tk.Frame(parent, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        card.pack(fill="x", pady=(10, 0))
        tk.Label(card, text=title, bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(
            anchor="w", padx=12, pady=(8, 0))
        tk.Label(card, text=hint, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=470,
                 justify="left").pack(anchor="w", padx=12, pady=(0, 6))
        return card

    def _entry(self, parent, var, width=22):
        p = self.pal
        return tk.Entry(parent, textvariable=var, width=width, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                        insertbackground=p["GREEN"], highlightthickness=1, highlightbackground=p["EDGE"],
                        highlightcolor=p["GREEN"])

    @staticmethod
    def _hex(text: str) -> str:
        return text.encode("utf-8").hex() or "-"

    @staticmethod
    def _unhex(h: str) -> str:
        try:
            return bytes.fromhex(h).decode("utf-8", "replace") if h and h != "-" else ""
        except ValueError:
            return ""

    # --- names ------------------------------------------------------------------------------------------------
    def _build_names(self):
        p = self.pal
        card = self._card(self.left, "NAMES", "Your name: the robot greets you with it when it recognises your face "
                                              "(teach it with Faces → Remember this face). Both names are used by "
                                              "the AI too.")
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(0, 4))
        self.owner_var = tk.StringVar(value=self.app.cfg.get("owner_name", ""))
        self.robot_var = tk.StringVar(value=self.app.cfg.get("robot_name", ""))
        tk.Label(row, text="Your name", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9)).grid(row=0, column=0,
                                                                                               sticky="w")
        self._entry(row, self.owner_var).grid(row=0, column=1, sticky="w", padx=8, pady=2)
        tk.Label(row, text="Robot's name", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9)).grid(row=1, column=0,
                                                                                                  sticky="w")
        self._entry(row, self.robot_var).grid(row=1, column=1, sticky="w", padx=8, pady=2)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(2, 10))
        self.app.button(bottom, "Save names", self.save_names, small=True).pack(side="left")
        self.names_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.names_lbl.pack(side="left", padx=10)

    def save_names(self):
        owner = " ".join(self.owner_var.get().split())[:24]
        robot = " ".join(self.robot_var.get().split())[:24]
        self.app.cfg.update(owner_name=owner, robot_name=robot)
        core.save_config(self.app.cfg)
        self.app.ai_tab.personality_changed()
        self.names_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])

        def done(r):
            self._names_reply(r[0], saved=True)
        self._cmd([(f"names {self._hex(owner)} {self._hex(robot)}", "BBNAMES")], done)
        if self.working is False:  # no robot: still saved for the AI on this PC
            self.names_lbl.configure(text="Saved for the AI. Plug the robot in to save the greeting on it too.",
                                     fg=self.pal["WARN"])

    def _names_reply(self, r, saved: bool = False):
        if not r or "owner" not in r:
            return
        owner, robot = self._unhex(r.get("owner", "")), self._unhex(r.get("robot", ""))
        if saved:
            self.names_lbl.configure(text="Saved on the robot.", fg=self.pal["GREEN"])
        elif owner or robot:  # the robot's copy wins over a stale one on this PC
            self.owner_var.set(owner)
            self.robot_var.set(robot)
            if (owner, robot) != (self.app.cfg.get("owner_name", ""), self.app.cfg.get("robot_name", "")):
                self.app.cfg.update(owner_name=owner, robot_name=robot)
                core.save_config(self.app.cfg)
                self.app.ai_tab.personality_changed()

    # --- controller theme -------------------------------------------------------------------------------------
    def _build_theme(self):
        p = self.pal
        card = self._card(self.left, "CONTROLLER THEME", "The controller's colours. Plug the controller in by USB; it "
                                                         "switches straight away (back to its home screen).")
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(0, 4))
        self.theme_btns = []
        for i, (name, prim, sec, bg) in enumerate(THEMES):
            cv = tk.Canvas(row, width=92, height=46, bg=p["PANEL"], highlightthickness=2,
                           highlightbackground=p["EDGE"], cursor="hand2")
            cv.grid(row=0, column=i, padx=(0, 8))
            cv.create_rectangle(4, 4, 88, 42, fill=bg, outline=prim)
            cv.create_rectangle(4, 4, 60, 13, fill=prim, outline="")
            cv.create_text(8, 8, text=name.upper(), anchor="w", fill=bg, font=("Consolas", 7, "bold"))
            cv.create_text(46, 28, text="BUTTER", fill=prim, font=("Consolas", 9, "bold"))
            cv.create_line(14, 37, 78, 37, fill=sec, width=2)
            cv.bind("<Button-1>", lambda e, n=i: self.set_theme(n))
            tooltip.Tooltip(cv, f"Switch the controller to the {name} theme.")
            self.theme_btns.append(cv)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(2, 10))
        self.theme_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=440,
                                  justify="left")
        self.theme_lbl.pack(side="left")

    def _ctrl_port(self) -> str | None:
        port = self.app.known_ports.get("ctrl", "")
        if not port or not core.port_exists(port):
            self.theme_lbl.configure(text="Plug the controller in by USB, switch it on, and hit Scan above.",
                                     fg=self.pal["WARN"])
            return None
        if self.app.port_in_use(port) or self.app.busy:
            return None
        return port

    def _mark_theme(self, n: int | None):
        for i, cv in enumerate(self.theme_btns):
            cv.configure(highlightbackground=self.pal["GREEN"] if i == n else self.pal["EDGE"])

    def set_theme(self, n: int):
        port = self._ctrl_port()
        if not port:
            return
        name = THEMES[n][0]
        self.theme_lbl.configure(text=f"Switching the controller to {name}…", fg=self.pal["DIM"])

        def work():
            r = core.console_command(port, f"theme {n}", "BBTHEME", timeout_s=4)
            problem = core.port_problem(r, port)

            def ui():
                if problem or not r or "theme" not in r:
                    self.theme_lbl.configure(text=problem or "No answer - the controller needs this release's "
                                                             "firmware (Flash tab), or it's switched off.",
                                             fg=self.pal["WARN"])
                    return
                self._mark_theme(n)
                self.app.controller_tab.set_theme(n)
                self.theme_lbl.configure(text=f"{name} it is.",
                                         fg=self.pal["GREEN"])
            self.app.ui(ui)
        threading.Thread(target=work, daemon=True).start()

    # --- your ramble lines ------------------------------------------------------------------------------------
    def _build_rambles(self):
        p = self.pal
        card = self._card(self.right, "YOUR RAMBLE LINES", "Lines the robot mixes into what it mutters to itself when "
                                                           "idle - one per line, up to 30, 100 characters each. Saved "
                                                           "on the robot.")
        self.rambles = tk.Text(card, height=8, width=56, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                               insertbackground=p["GREEN"], font=("Segoe UI", 9), wrap="none", undo=True)
        self.rambles.pack(fill="x", padx=12)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(bottom, "Save lines", self.save_rambles, small=True).pack(side="left")
        self.ramble_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.ramble_lbl.pack(side="left", padx=10)

    def _rambles_reply(self, r):
        if not r or "count" not in r:
            return
        lines = [self._unhex(h) for h in r.get("d", "-").split(",") if h and h != "-"]
        self.rambles.delete("1.0", "end")
        self.rambles.insert("1.0", "\n".join(lines))
        self.ramble_lbl.configure(text=f"{len(lines)} line{'s' if len(lines) != 1 else ''} on the robot.",
                                  fg=self.pal["DIM"])

    def save_rambles(self):
        lines = [" ".join(line.split())[:100] for line in self.rambles.get("1.0", "end").splitlines()]
        lines = [line for line in lines if line][:30]
        cmds = [("ramble clear", "BBRAMBLE")] + [(f"ramble add {self._hex(line)}", "BBRAMBLE") for line in lines]
        self.ramble_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])

        def done(r):
            if any(x is None for x in r):
                self.ramble_lbl.configure(text="The robot didn't take them all (is it on, with this release's "
                                               "firmware?).", fg=self.pal["WARN"])
                return
            self.ramble_lbl.configure(text=f"Saved {len(lines)} line{'s' if len(lines) != 1 else ''} on the robot.",
                                      fg=self.pal["GREEN"])
        self._cmd(cmds, done)

    # --- AI personality ---------------------------------------------------------------------------------------
    def _build_personality(self):
        p = self.pal
        card = self._card(self.right, "AI PERSONALITY", "How the robot talks in AI conversations (AI tab). Kept on "
                                                        "this PC; the brain uses it from the next question.")
        self.pers_var = tk.StringVar(value=self.app.cfg.get("ai_personality", "classic"))
        grid = tk.Frame(card, bg=p["PANEL"])
        grid.pack(fill="x", padx=12)
        options = [(k, v[0], v[1]) for k, v in brain.PERSONALITIES.items()] + [
            ("custom", "Your own", "Describe it below")]
        for i, (key, label, desc) in enumerate(options):
            tk.Radiobutton(grid, text=label, value=key, variable=self.pers_var, command=self.save_personality,
                           bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"], activebackground=p["PANEL"],
                           activeforeground=p["GREEN"], highlightthickness=0, font=("Segoe UI Semibold", 9)).grid(
                row=i // 2, column=(i % 2) * 2, sticky="w")
            tk.Label(grid, text=desc, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).grid(
                row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 14))
        self.custom = tk.Text(card, height=3, width=56, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                              insertbackground=p["GREEN"], font=("Segoe UI", 9), wrap="word")
        self.custom.insert("1.0", self.app.cfg.get("ai_personality_custom", ""))
        self.custom.pack(fill="x", padx=12, pady=(6, 0))
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(bottom, "Save personality", self.save_personality, small=True).pack(side="left")
        self.pers_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.pers_lbl.pack(side="left", padx=10)

    def save_personality(self):
        custom = self.custom.get("1.0", "end").strip()[:brain.CUSTOM_MAX]
        key = self.pers_var.get()
        if key == "custom" and not custom:
            self.pers_lbl.configure(text="Describe the personality in the box first.", fg=self.pal["WARN"])
            return
        self.app.cfg.update(ai_personality=key, ai_personality_custom=custom)
        core.save_config(self.app.cfg)
        self.app.ai_tab.personality_changed()
        label = brain.PERSONALITIES.get(key, ("Your own",))[0]
        self.pers_lbl.configure(text=f"Saved: {label}.", fg=self.pal["GREEN"])
