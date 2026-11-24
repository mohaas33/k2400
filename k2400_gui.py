#!/usr/bin/env python3
"""
k2400_gui.py - Front-panel style GUI for the Keithley 2400 SourceMeter over RS-232.

Features
  * Window looks like the 2400 front panel; the keys work like the real ones (KEY MAP below)
  * Source V or I, level and compliance, output on/off, front/rear terminals, 2/4-wire
  * Live readout of V, I (and R = V/I), live plot: "V & I vs time" or "I vs V"
  * CSV logging of every reading, linear I-V sweep
  * Built-in simulator (10 kOhm load) so you can try it without the instrument

Instrument setup (front panel): MENU > COMMUNICATION > RS-232, then set
  baud (e.g. 9600), 8 data bits, parity NONE, terminator <CR>, flow control NONE.
  Use the same baud/terminator in the GUI. Selecting RS-232 disables GPIB.
  If *IDN? times out with a straight DB-9 cable, try a null-modem cable.

Requires: Python 3.8+, pyserial, matplotlib  ->  pip install pyserial matplotlib
Run:      python k2400_gui.py          (add --sim to start in simulator mode)
          The window is sized to the screen; override with --scale 1.5 or --geometry 1600x1000

KEY MAP
  POWER ................. connect / disconnect
  MEAS V / I / Ohm ...... what the big reading shows (FCTN cycles through them)
  SOURCE V / I .......... source function (turns the output off first)
  SOURCE up / down ...... change the digit under the blinking cursor (applies immediately)
  EDIT < / > ............ move the cursor
  EDIT (teal key) ....... toggle editing between source level and compliance
  DISPLAY TOGGLE ........ bottom line: source/compliance <-> other readings
  RANGE up / AUTO / down  measurement range of the measured quantity
  ON/OFF OUTPUT ......... output on/off
  FRONT/REAR ............ select terminals (turns output off first)
  LOCAL ................. return instrument to front-panel control
  REL ................... zero the present reading (toggle)
  FILTER ................ 10-reading repeating average on/off
  DIGITS / SPEED ........ display resolution / integration time (NPLC)
  STORE ................. start/stop CSV logging   RECALL ... show logged count
  TRIG .................. take one reading         SWEEP .... run the I-V sweep
  CONFIG / MENU ......... open the settings tabs   LIMIT .... not used
  ENTER ................. apply level/compliance typed in the side panel
  EXIT .................. abort a running sweep
"""
import argparse
import csv
import datetime
import math
import queue
import random
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:          # simulator still works without pyserial
    serial = None

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.ticker import EngFormatter

# ----------------------------------------------------------------------------- constants
PANEL, KEYPAD, BEZEL, LCD = "#e2e2de", "#d3d4d0", "#1d1f21", "#03070a"
VFD, KEY_DARK, BG = "#46dcff", "#3b3e41", "#b9bcb8"

PREFIX = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}
RANGES = {"VOLT": [0.2, 2.0, 20.0, 200.0],
          "CURR": [1e-6, 10e-6, 100e-6, 1e-3, 10e-3, 100e-3, 1.0]}
SPEEDS = [("FAST", 0.01), ("MED", 0.1), ("NORMAL", 1.0), ("HI ACCURACY", 10.0)]
DIGITS = [(5, "4.5"), (6, "5.5"), (7, "6.5")]
SRC_LIMIT = {"VOLT": 210.0, "CURR": 1.05}
CMPL_LIMIT = {"VOLT": (1e-9, 1.05), "CURR": (1e-3, 210.0)}   # keyed by SOURCE function
SRC_CURSOR = {"VOLT": (-6, 2), "CURR": (-12, 0)}             # allowed digit exponents
CMPL_CURSOR = {"VOLT": (-9, 0), "CURR": (-3, 2)}
UNIT = {"VOLT": "V", "CURR": "A"}
OVERFLOW = 9.0e37
MAX_POINTS = 200_000
UI = 1.0          # UI scale factor, set at start-up to fit the screen (or --scale)
FONT_K = 1.0      # font size multiplier, see init_scaling()
BASE_W, BASE_H = 1092, 864    # window size in pixels at UI = 1


def fs(pt):
    """Font size in points for a design size `pt` (at UI = 1)."""
    return max(1, round(pt * FONT_K))


def fit(c):
    """Scale everything drawn on a canvas (drawn at base size) to the UI scale."""
    if UI != 1.0:
        c.scale("all", 0, 0, UI, UI)
    c.config(width=int(float(c["width"]) * UI), height=int(float(c["height"]) * UI))


# ----------------------------------------------------------------------------- helpers
def exp3_of(x):
    if not x or not math.isfinite(x):
        return 0
    return max(-12, min(9, int(math.floor(math.log10(abs(x)) / 3) * 3)))


def eng(value, unit, sig=6):
    """+1.23456 mA style formatting with `sig` significant digits."""
    if value is None:
        return "------ " + unit
    if not math.isfinite(value) or abs(value) >= OVERFLOW:
        return "OVRFLO " + unit
    e = exp3_of(value)
    for _ in range(3):
        m = value / 10 ** e
        intd = max(1, len(str(int(abs(m)))))
        s = f"{m:+.{max(0, sig - intd)}f}"
        if abs(float(s)) >= 1000 and e < 9:
            e += 3
            continue
        break
    return f"{s} {PREFIX[e]}{unit}"


def fmt_edit(value, unit, cursor, signed=True):
    """Fixed 3.3 format used for the editable source/compliance fields.
    Returns (text, index of the char under the cursor)."""
    mag = exp3_of(value) if value else (cursor // 3) * 3
    cands = [e for e in range(cursor - 2, cursor + 4) if e % 3 == 0]
    e = min(cands, key=lambda c: (abs(c - mag), -c))
    s = f"{value / 10 ** e:+08.3f}"
    if not signed:
        s = s[1:]
    dot = s.index(".")
    rel = cursor - e
    idx = dot - 1 - rel if rel >= 0 else dot - rel
    return f"{s} {PREFIX[e]}{unit}", idx


def fmt_range(r, unit):
    e = exp3_of(r)
    return f"{r / 10 ** e:g}{PREFIX[e]}{unit}"


SUFFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6}


def parse_eng(text):
    """Accepts 1.5, 2e-3, 100m, 105u, 105uA, 2 V ..."""
    s = text.strip().replace(" ", "")
    while s and s[-1] in "VvAaΩ":
        s = s[:-1]
    if s and s[-1] in SUFFIX:
        return float(s[:-1]) * SUFFIX[s[-1]]
    return float(s)


def port_key(dev):
    """USB/ACM adapters first, then COMx / ttySx in numeric order."""
    import re
    usb = 0 if re.search(r"USB|ACM|usbserial|usbmodem", dev) else 1
    num = re.search(r"(\d+)$", dev)
    return usb, re.sub(r"\d+$", "", dev), int(num.group(1)) if num else 0


def pick_font(root, names):
    fams = set(tkfont.families(root))
    return next((n for n in names if n in fams), "Courier")


# ----------------------------------------------------------------------------- instrument I/O
class Keithley2400:
    """Thin SCPI-over-RS-232 transport."""
    TERMS = {"CR": "\r", "LF": "\n", "CRLF": "\r\n"}

    def __init__(self, port, baud=9600, term="CR", timeout=2.0):
        if serial is None:
            raise RuntimeError("pyserial is not installed (pip install pyserial)")
        self.term = self.TERMS[term]
        self.lock = threading.RLock()
        self.ser = serial.Serial(port=port, baudrate=int(baud), bytesize=8, parity="N",
                                 stopbits=1, timeout=timeout, xonxoff=False, rtscts=False)
        time.sleep(0.1)
        self.ser.reset_input_buffer()

    def write(self, cmd):
        with self.lock:
            self.ser.write((cmd + self.term).encode("ascii"))

    def query(self, cmd):
        with self.lock:
            self.ser.reset_input_buffer()
            self.write(cmd)
            raw = self.ser.read_until(self.term[-1].encode())
        if not raw.strip():
            raise TimeoutError(f"No reply to {cmd} (check baud, terminator, cable)")
        return raw.decode("ascii", "replace").strip()

    def close(self):
        self.ser.close()


class Simulated2400:
    """Answers the SCPI subset this GUI uses; load is ~10 kOhm."""

    def __init__(self):
        self.lock = threading.RLock()
        self.src, self.out = "VOLT", False
        self.vlev = self.ilev = 0.0
        self.icmp, self.vcmp, self.nplc = 105e-6, 21.0, 1.0

    def write(self, cmd):
        self._handle(cmd)

    def query(self, cmd):
        return self._handle(cmd) or ""

    def close(self):
        pass

    def _handle(self, cmd):
        with self.lock:
            c = cmd.strip().upper()
            val = lambda: float(c.split()[-1])
            if c == "*IDN?":
                return "KEITHLEY INSTRUMENTS INC.,MODEL 2400,SIMULATOR,C32"
            if c == "*OPC?":
                return "1"
            if c.startswith(":SYST:ERR"):
                return '0,"No error"'
            if c.startswith(":SOUR:FUNC"):
                self.src = "VOLT" if "VOLT" in c else "CURR"
            elif c.startswith(":SOUR:VOLT:LEV"):
                self.vlev = val()
            elif c.startswith(":SOUR:CURR:LEV"):
                self.ilev = val()
            elif c.startswith(":SENS:CURR:PROT"):
                self.icmp = val()
            elif c.startswith(":SENS:VOLT:PROT"):
                self.vcmp = val()
            elif c.startswith(":SENS:CURR:NPLC"):
                self.nplc = val()
            elif c.startswith(":OUTP"):
                if c.endswith("?"):
                    return "1" if self.out else "0"
                self.out = c.endswith("ON") or c.endswith(" 1")
            elif c == ":READ?":
                return self._read()
        return None

    def _read(self):
        time.sleep(0.02 * min(self.nplc, 2) + 0.01)
        if not self.out:
            return "+9.910000E+37,+9.910000E+37,+0.000000E+00"
        r = 10e3 * (1 + 0.003 * math.sin(time.time() / 4))
        cmpl = False
        if self.src == "VOLT":
            v, i = self.vlev, self.vlev / r
            if abs(i) > self.icmp:
                i, cmpl = math.copysign(self.icmp, i), True
                v = i * r
        else:
            i, v = self.ilev, self.ilev * r
            if abs(v) > self.vcmp:
                v, cmpl = math.copysign(self.vcmp, v), True
                i = v / r
        v += random.gauss(0, 20e-6)
        i = i * (1 + random.gauss(0, 2e-4)) + random.gauss(0, 50e-12)
        return f"{v:+.6E},{i:+.6E},{8 if cmpl else 0:+.6E}"


# ----------------------------------------------------------------------------- key widgets
def _clickable(c, command):
    def press(_):
        c.move("all", 1, 1)

    def release(e):
        c.move("all", -1, -1)
        if 0 <= e.x <= int(c["width"]) and 0 <= e.y <= int(c["height"]) and command:
            command(e)
    c.bind("<ButtonPress-1>", press)
    c.bind("<ButtonRelease-1>", release)


def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command, d=50, fill=KEY_DARK, fg="white",
                 font=None, bg=KEYPAD):
        font = font or ("Arial", fs(14))
        super().__init__(parent, width=d + 6, height=d + 6, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.create_oval(4, 5, d + 4, d + 5, fill="#8e8f8b", outline="")
        self.oval = self.create_oval(2, 2, d + 2, d + 2, fill=fill, outline="#1c1e20")
        self.create_arc(7, 7, d - 3, d - 3, start=50, extent=80, style="arc",
                        outline="#9a9ea2" if fill == KEY_DARK else "#ffffff", width=2)
        self.create_text(d / 2 + 2, d / 2 + 2, text=text, fill=fg, font=font, justify="center")
        fit(self)
        _clickable(self, lambda e: command())

    def set_fill(self, color):
        self.itemconfig(self.oval, fill=color)


class PillPair(tk.Canvas):
    """Two keys in one rounded pill (LOCAL|REL etc.). right=None gives a single key."""

    def __init__(self, parent, left, right, lcmd, rcmd, top=("", ""), top_center=None,
                 w=128, h=32, bg=KEYPAD, font=None):
        font = font or ("Arial", fs(10))
        y0 = 16
        super().__init__(parent, width=w + 6, height=y0 + h + 6, bg=bg,
                         highlightthickness=0, cursor="hand2")
        round_rect(self, 3, y0 + 3, w + 3, y0 + h + 3, h / 2, fill="#8e8f8b", outline="")
        round_rect(self, 1, y0 + 1, w + 1, y0 + h + 1, h / 2, fill="#f3f3f1", outline="#7a7b78")
        lab = ("Arial", fs(9), "bold")
        if right is None:
            self.create_text(w / 2 + 1, y0 + h / 2 + 1, text=left, font=font, fill="#333")
        else:
            self.create_line(w / 2 + 1, y0 + 5, w / 2 + 1, y0 + h - 3, fill="#b9bab6")
            self.create_text(w / 4 + 1, y0 + h / 2 + 1, text=left, font=font, fill="#333")
            self.create_text(3 * w / 4 + 1, y0 + h / 2 + 1, text=right, font=font, fill="#333")
            self.create_text(w / 4, 8, text=top[0], font=lab, fill="#333")
            self.create_text(3 * w / 4, 8, text=top[1], font=lab, fill="#333")
        if top_center:
            self.create_line(6, 14, 6, 8, w / 2 - 20, 8, fill="#333")
            self.create_line(w / 2 + 20, 8, w - 4, 8, w - 4, 14, fill="#333")
            self.create_text(w / 2, 8, text=top_center, font=lab, fill="#333")
        fit(self)
        half = int(self["width"]) / 2
        _clickable(self, lambda e: (lcmd if right is None or e.x < half else rcmd)())


class TriButton(tk.Canvas):
    def __init__(self, parent, direction, command, w=46, h=32, bg=KEYPAD):
        super().__init__(parent, width=w + 3, height=h + 3, bg=bg,
                         highlightthickness=0, cursor="hand2")
        pts = [w / 2, 2, w - 2, h - 2, 2, h - 2] if direction == "up" else \
              [2, 2, w - 2, 2, w / 2, h - 2]
        self.create_polygon([p + 2 for p in pts], fill="#8e8f8b")
        self.create_polygon(pts, fill="#f4f4f2", outline="#7a7b78")
        fit(self)
        _clickable(self, lambda e: command())


def bracket(parent, text, width, bg=KEYPAD):
    c = tk.Canvas(parent, width=width, height=18, bg=bg, highlightthickness=0)
    tw = len(text) * 9 + 12
    c.create_line(4, 17, 4, 9, width / 2 - tw / 2, 9, fill="#333")
    c.create_line(width / 2 + tw / 2, 9, width - 4, 9, width - 4, 17, fill="#333")
    c.create_text(width / 2, 9, text=text, font=("Arial", fs(11)), fill="#222")
    fit(c)
    return c


# ----------------------------------------------------------------------------- application
class App:
    def __init__(self, root, args):
        self.root = root
        root.title("Keithley 2400 SourceMeter - RS-232 control")
        root.configure(bg=BG)

        self.inst = None
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.sweep_abort = threading.Event()
        self.sweeping = False
        self.ramping, self.ramp_down, self.ramp_now = False, False, 0.0
        self.ramp_abort = threading.Event()
        self.ramp_rate = 0.1

        # instrument / front-panel state
        self.src = "VOLT"
        self.level = {"VOLT": 0.0, "CURR": 0.0}
        self.cmpl = {"VOLT": 105e-6, "CURR": 21.0}     # compliance used when sourcing that func
        self.cursor = {"VOLT": -3, "CURR": -6}
        self.ccursor = {"VOLT": -6, "CURR": -3}
        self.edit_target = "SRC"
        self.meas = "I"
        self.output_on = False
        self.auto_range = True
        self.range_idx = {"CURR": 3, "VOLT": 2}
        self.speed_idx, self.digit_idx = 2, 1
        self.filter_on, self.rear, self.toggle_view = False, False, False
        self.rel = {}
        self.last, self.in_cmpl = None, False
        self.flash_msg, self.flash_until, self.blink = "", 0.0, True
        self.interval = 0.5

        # data / logging
        self.t0, self.ts, self.vs, self.is_ = None, [], [], []
        self.plot_dirty = False
        self.log_fh = self.log_writer = None
        self.log_count, self.log_t0 = 0, 0.0

        self._build_ui()
        self.refresh_ports()
        self.sync_entries()
        self.flash("KEITHLEY 2400 - PRESS POWER TO CONNECT", 4)

        threading.Thread(target=self._poll_loop, daemon=True).start()
        root.after(50, self._process_queue)
        root.after(500, self._blink_tick)
        root.after(400, self._plot_tick)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if args.sim:
            self.sim_var.set(True)
            root.after(200, self.connect)

    # ======================================================================= UI construction
    def _build_ui(self):
        fam = pick_font(self.root, ["Consolas", "DejaVu Sans Mono", "Liberation Mono",
                                    "Menlo", "Courier New"])
        self.f_big = tkfont.Font(family=fam, size=fs(34), weight="bold")
        self.f_mid = tkfont.Font(family=fam, size=fs(19), weight="bold")
        self.f_small = tkfont.Font(family=fam, size=fs(10), weight="bold")
        lbl = ("Arial", fs(9), "bold")

        front = tk.Frame(self.root, bg=PANEL, bd=2, relief="raised")
        front.pack(side="top", fill="x", padx=8, pady=(8, 4))

        # --- display
        bez = tk.Frame(front, bg=BEZEL)
        bez.grid(row=0, column=0, padx=(10, 6), pady=10, sticky="nsew")
        self.DW, self.DH = 800, 190
        self.disp = tk.Canvas(bez, width=int(self.DW * UI), height=int(self.DH * UI), bg=LCD,
                              highlightthickness=0)
        self.disp.pack(padx=8, pady=8)

        term = tk.Canvas(front, width=230, height=215, bg=PANEL, highlightthickness=0)
        term.grid(row=0, column=1, padx=(0, 10), pady=4)
        self._draw_terminals(term)
        fit(term)

        # --- keypad
        kp = tk.Frame(front, bg=KEYPAD, bd=1, relief="solid")
        kp.grid(row=1, column=0, padx=(10, 6), pady=(0, 10), sticky="nsew")

        lc = tk.Frame(kp, bg=KEYPAD)
        lc.pack(side="left", padx=(12, 6), pady=10, anchor="n")
        r = tk.Frame(lc, bg=KEYPAD)
        r.pack(anchor="e")
        tk.Label(r, text="EDIT", bg=KEYPAD, font=lbl).pack(side="left")
        RoundButton(r, "", self.key_edit, d=24, fill="#6cb8ad").pack(side="left", padx=4)
        tk.Label(lc, text="DISPLAY", bg=KEYPAD, font=("Arial", fs(10), "bold")).pack(anchor="w", pady=(8, 0))
        r = tk.Frame(lc, bg=KEYPAD)
        r.pack(anchor="e")
        tk.Label(r, text="TOGGLE", bg=KEYPAD, font=lbl).pack(side="left")
        RoundButton(r, "", self.key_toggle, d=24).pack(side="left", padx=4)
        tk.Label(lc, text="POWER", bg=KEYPAD, font=("Arial", fs(12), "bold")).pack(anchor="w", pady=(10, 0))
        self.pwr_btn = RoundButton(lc, "⏻", self.key_power, d=34, fill="#f2f2ef", fg="#333")
        self.pwr_btn.pack(anchor="e")

        f_key = ("Arial", fs(15))
        mg = tk.Frame(kp, bg=KEYPAD)
        mg.pack(side="left", padx=8, pady=6, anchor="n")
        bracket(mg, "MEAS", 272).pack()
        r = tk.Frame(mg, bg=KEYPAD)
        r.pack()
        for t, cmd, f in [("V", lambda: self.key_meas("V"), f_key),
                          ("I", lambda: self.key_meas("I"), f_key),
                          ("Ω", lambda: self.key_meas("R"), f_key),
                          ("FCTN", self.key_fctn, ("Arial", fs(10)))]:
            RoundButton(r, t, cmd, font=f).pack(side="left", padx=5)
        r = tk.Frame(mg, bg=KEYPAD)
        r.pack()
        PillPair(r, "LOCAL", "REL", self.key_local, self.key_rel, ("0", "1")).pack(side="left", padx=2)
        PillPair(r, "FILTER", "LIMIT", self.key_filter, self.key_limit, ("2", "3")).pack(side="left", padx=2)
        r = tk.Frame(mg, bg=KEYPAD)
        r.pack()
        PillPair(r, "DIGITS", "SPEED", self.key_digits, self.key_speed, ("6", "7")).pack(side="left", padx=2)
        PillPair(r, "STORE", "RECALL", self.key_store, self.key_recall, ("8", "9")).pack(side="left", padx=2)

        sg = tk.Frame(kp, bg=KEYPAD)
        sg.pack(side="left", padx=8, pady=6, anchor="n")
        bracket(sg, "SOURCE", 272).pack()
        r = tk.Frame(sg, bg=KEYPAD)
        r.pack()
        for t, cmd in [("V", lambda: self.key_source("VOLT")), ("I", lambda: self.key_source("CURR")),
                       ("▲", lambda: self.key_updown(+1)), ("▼", lambda: self.key_updown(-1))]:
            RoundButton(r, t, cmd, font=f_key).pack(side="left", padx=5)
        r = tk.Frame(sg, bg=KEYPAD)
        r.pack()
        PillPair(r, "TRIG", "SWEEP", self.key_trig, self.start_sweep, ("4", "5")).pack(side="left", padx=2)
        PillPair(r, "◀", "▶", lambda: self.key_cursor(+1), lambda: self.key_cursor(-1),
                 top_center="EDIT").pack(side="left", padx=2)
        r = tk.Frame(sg, bg=KEYPAD)
        r.pack()
        PillPair(r, "CONFIG", "MENU", self.key_config, self.key_menu, ("+/-", "")).pack(side="left", padx=2)
        PillPair(r, "EXIT", "ENTER", self.key_exit, self.apply_entries).pack(side="left", padx=2)

        rg = tk.Frame(kp, bg=KEYPAD)
        rg.pack(side="left", padx=(8, 14), pady=6, anchor="n")
        TriButton(rg, "up", lambda: self.key_range(+1)).pack(pady=(16, 0))
        tk.Label(rg, text="RANGE", bg=KEYPAD, font=("Arial", fs(10))).pack()
        PillPair(rg, "AUTO", None, self.key_auto, None, w=92).pack()
        tk.Label(rg, text="RANGE", bg=KEYPAD, font=("Arial", fs(10))).pack()
        TriButton(rg, "down", lambda: self.key_range(-1)).pack()

        # --- output + terminals keys
        right = tk.Frame(front, bg=PANEL)
        right.grid(row=1, column=1, padx=(0, 10), pady=(0, 10), sticky="nsew")
        ob = tk.Frame(right, bg=PANEL, bd=2, relief="groove")
        ob.pack(side="left", padx=6, pady=6, anchor="n")
        self.out_btn = RoundButton(ob, "", self.key_output, d=46, bg=PANEL)
        self.out_btn.pack(pady=(10, 4), padx=18)
        tk.Label(ob, text="ON/OFF", bg="#2f3235", fg="white", font=("Arial", fs(12), "bold"),
                 padx=8, pady=4).pack(padx=8)
        tk.Label(ob, text="OUTPUT", bg=PANEL, font=lbl).pack(pady=(2, 8))
        tf = tk.Frame(right, bg=PANEL)
        tf.pack(side="left", padx=6, pady=(40, 0), anchor="n")
        tk.Label(tf, text="TERMINALS", bg=PANEL, font=lbl).pack()
        RoundButton(tf, "FRONT/\nREAR", self.key_terminals, d=44, fill="#6a6d70",
                    font=("Arial", fs(7), "bold"), bg=PANEL).pack()

        # --- bottom: settings + plot
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.nb = ttk.Notebook(bottom, width=int(340 * UI))
        self.nb.pack(side="left", fill="y")
        self._build_tabs()

        # fixed request size: otherwise the matplotlib canvas keeps growing the window on HiDPI
        plotf = tk.Frame(bottom, width=int(650 * UI), height=int(390 * UI))
        plotf.pack_propagate(False)
        plotf.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.fig = Figure(figsize=(6.5, 3.4), dpi=self._plot_dpi(), constrained_layout=True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plotf)
        tb = NavigationToolbar2Tk(self.canvas, plotf, pack_toolbar=False)
        tb.update()
        # matplotlib fixes the toolbar at 50 px, too low for its own icons on a large UI scale
        tb.configure(height=max(50, max(w.winfo_reqheight() for w in tb.winfo_children()
                                            if w.winfo_class() == "Button") + 4))
        tb.pack(side="bottom", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.build_axes()

        self.status = tk.Label(self.root, text="Not connected", anchor="w", bg=BG,
                               font=("Arial", fs(9)))
        self.status.pack(side="bottom", fill="x", padx=10, pady=(0, 4))

    def _plot_dpi(self):
        """Matplotlib applies its own HiDPI ratio on Windows/Linux; add only what is missing."""
        if sys.platform == "win32":
            mpl_ratio = UI
        elif sys.platform == "linux":
            mpl_ratio = self.root.winfo_fpixels("1i") / 96
        else:
            mpl_ratio = 1.0
        return 100 * UI / max(mpl_ratio, 0.5)

    def _build_tabs(self):
        def row(parent, r, text, widget, **kw):
            ttk.Label(parent, text=text).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="we", pady=2, **kw)
            return widget

        # Connection
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="Connection")
        self.port_var = tk.StringVar()
        self.port_cb = row(t, 0, "Port", ttk.Combobox(t, textvariable=self.port_var, width=20))
        ttk.Button(t, text="↻", width=3, command=self.refresh_ports).grid(row=0, column=2, padx=2)
        self.baud_var = tk.StringVar(value="9600")
        row(t, 1, "Baud", ttk.Combobox(t, textvariable=self.baud_var, state="readonly", width=20,
            values=["57600", "38400", "19200", "9600", "4800", "2400", "1200", "600", "300"]))
        self.term_var = tk.StringVar(value="CR")
        row(t, 2, "Terminator", ttk.Combobox(t, textvariable=self.term_var, state="readonly",
                                             width=20, values=["CR", "LF", "CRLF"]))
        self.sim_var = tk.BooleanVar(value=serial is None)
        ttk.Checkbutton(t, text="Simulator (no instrument)", variable=self.sim_var).grid(
            row=3, column=0, columnspan=3, sticky="w")
        self.reset_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(t, text="Reset instrument (*RST) on connect", variable=self.reset_var).grid(
            row=4, column=0, columnspan=3, sticky="w")
        self.conn_btn = ttk.Button(t, text="Connect", command=self.key_power)
        self.conn_btn.grid(row=5, column=0, columnspan=3, sticky="we", pady=(6, 2))
        ttk.Button(t, text="Check errors (SYST:ERR?)", command=self.check_errors).grid(
            row=6, column=0, columnspan=3, sticky="we")
        self.idn_lbl = ttk.Label(t, text="Not connected", wraplength=int(310 * UI), foreground="#555")
        self.idn_lbl.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        t.columnconfigure(1, weight=1)

        # Source / Measure
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="Source/Measure")
        self.src_var = tk.StringVar(value="VOLT")
        f = ttk.Frame(t)
        ttk.Radiobutton(f, text="Voltage", value="VOLT", variable=self.src_var,
                        command=lambda: self.key_source(self.src_var.get())).pack(side="left")
        ttk.Radiobutton(f, text="Current", value="CURR", variable=self.src_var,
                        command=lambda: self.key_source(self.src_var.get())).pack(side="left", padx=8)
        row(t, 0, "Source", f)
        self.level_var, self.cmpl_var = tk.StringVar(), tk.StringVar()
        self.level_lbl = ttk.Label(t, text="Level (V)")
        self.level_lbl.grid(row=1, column=0, sticky="w")
        e1 = ttk.Entry(t, textvariable=self.level_var, width=14)
        e1.grid(row=1, column=1, sticky="we", pady=2)
        self.cmpl_lbl = ttk.Label(t, text="Compliance (A)")
        self.cmpl_lbl.grid(row=2, column=0, sticky="w")
        e2 = ttk.Entry(t, textvariable=self.cmpl_var, width=14)
        e2.grid(row=2, column=1, sticky="we", pady=2)
        for e in (e1, e2):
            e.bind("<Return>", lambda _e: self.apply_entries())
        ttk.Label(t, text="e.g. 1.5   100m   105u   2e-3", foreground="#666").grid(
            row=3, column=1, sticky="w")
        ttk.Button(t, text="Apply  (ENTER)", command=self.apply_entries).grid(
            row=4, column=0, columnspan=2, sticky="we", pady=4)
        self.fourw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(t, text="4-wire (remote sense)", variable=self.fourw_var,
                        command=self.on_fourw).grid(row=5, column=0, columnspan=2, sticky="w")
        self.speed_var = tk.StringVar(value=SPEEDS[self.speed_idx][0])
        cb = row(t, 6, "Speed (NPLC)", ttk.Combobox(t, textvariable=self.speed_var, state="readonly",
                 values=[f"{n}" for n, _ in SPEEDS], width=14))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.set_speed(
            [n for n, _ in SPEEDS].index(self.speed_var.get())))
        self.interval_var = tk.StringVar(value="0.5")
        row(t, 7, "Poll interval (s)", ttk.Entry(t, textvariable=self.interval_var, width=14))
        self.interval_var.trace_add("write", self._on_interval)
        ttk.Separator(t).grid(row=8, column=0, columnspan=2, sticky="we", pady=6)
        self.ramp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(t, text="Ramp the source (ON, OFF and level changes)",
                        variable=self.ramp_var).grid(row=9, column=0, columnspan=2, sticky="w")
        self.rate_var = tk.StringVar(value="0.1")
        self.rate_lbl = ttk.Label(t, text="Ramp rate (V/s)")
        self.rate_lbl.grid(row=10, column=0, sticky="w")
        ttk.Entry(t, textvariable=self.rate_var, width=14).grid(row=10, column=1, sticky="we", pady=2)
        self.rate_var.trace_add("write", self._on_rate)
        t.columnconfigure(1, weight=1)

        # Sweep
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="Sweep")
        self.sw_vars = {}
        for k, (name, default) in enumerate([("Start", "0"), ("Stop", "1"),
                                             ("Points", "21"), ("Delay (s)", "0.05")]):
            v = tk.StringVar(value=default)
            self.sw_vars[name] = v
            row(t, k, name, ttk.Entry(t, textvariable=v, width=14))
        ttk.Label(t, text="Sweeps the selected source function (V or A).\n"
                          "Compliance applies. Output turns OFF at the end.\n"
                          "Clears the plot and shows I vs V.",
                  foreground="#555").grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(t, text="Run sweep  (SWEEP)", command=self.start_sweep).grid(
            row=5, column=0, columnspan=2, sticky="we")
        ttk.Button(t, text="Abort  (EXIT)", command=self.key_exit).grid(
            row=6, column=0, columnspan=2, sticky="we", pady=2)
        t.columnconfigure(1, weight=1)

        # Log & Plot
        t = ttk.Frame(self.nb, padding=8)
        self.nb.add(t, text="Log & Plot")
        self.logfile_var = tk.StringVar()
        row(t, 0, "Log file", ttk.Entry(t, textvariable=self.logfile_var, width=22))
        ttk.Button(t, text="…", width=3, command=self.browse_log).grid(row=0, column=2, padx=2)
        self.log_btn = ttk.Button(t, text="Start logging  (STORE)", command=self.key_store)
        self.log_btn.grid(row=1, column=0, columnspan=3, sticky="we", pady=2)
        self.log_lbl = ttk.Label(t, text="Not logging", foreground="#555")
        self.log_lbl.grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Separator(t).grid(row=3, column=0, columnspan=3, sticky="we", pady=6)
        self.plotmode_var = tk.StringVar(value="V & I vs time")
        cb = row(t, 4, "Plot", ttk.Combobox(t, textvariable=self.plotmode_var, state="readonly",
                 values=["V & I vs time", "I vs V"], width=20))
        cb.bind("<<ComboboxSelected>>", lambda _e: (self.build_axes(), self.refresh_plot()))
        ttk.Button(t, text="Clear plot", command=self.clear_plot).grid(
            row=5, column=0, columnspan=3, sticky="we", pady=2)
        ttk.Button(t, text="Save plot as PNG", command=self.save_png).grid(
            row=6, column=0, columnspan=3, sticky="we", pady=2)
        ttk.Button(t, text="Save plotted data as CSV", command=self.save_buffer).grid(
            row=7, column=0, columnspan=3, sticky="we", pady=2)
        t.columnconfigure(1, weight=1)

    def _draw_terminals(self, c):
        lbl = ("Arial", fs(10), "bold")

        def jack(x, y, color, inner):
            c.create_oval(x - 31, y - 31, x + 31, y + 31, fill="#cfcfcb", outline="#999")
            c.create_oval(x - 25, y - 25, x + 25, y + 25, fill=color, outline="#222")
            c.create_oval(x - 13, y - 13, x + 13, y + 13, fill=inner, outline="#111")
            c.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#111")
        c.create_text(60, 20, text="4-WIRE\nSENSE", font=lbl, justify="center")
        c.create_text(170, 20, text="INPUT/\nOUTPUT", font=lbl, justify="center")
        jack(60, 82, "#d0202a", "#8a1018")
        jack(170, 82, "#d0202a", "#8a1018")
        jack(60, 168, "#1e1e1e", "#000")
        jack(170, 168, "#1e1e1e", "#000")
        c.create_text(115, 64, text="HI", font=lbl)
        c.create_text(115, 192, text="LO", font=lbl)
        c.create_text(115, 125, text="5V\nPEAK", font=("Arial", fs(7)), justify="center")
        c.create_text(18, 125, text="250V\nPEAK", font=("Arial", fs(7)), justify="center")

    # ======================================================================= display
    def redraw(self):
        c, W = self.disp, self.DW
        c.delete("all")
        c.create_rectangle(14, 12, 128, 40, fill="#c8102e", outline="#e04050")
        c.create_text(71, 26, text="KEITHLEY", fill="white", font=("Arial", fs(13)))
        ann = []
        if self.inst:
            ann.append("REM")
        if self.rear:
            ann.append("REAR")
        if self.fourw_var.get():
            ann.append("4W")
        if self.filter_on:
            ann.append("FILT")
        if self.meas in self.rel:
            ann.append("REL")
        if self.log_writer:
            ann.append("LOG")
        if self.sweeping:
            ann.append("SWP")
        if self.ramping:
            ann.append("RAMP")
        if self.edit_target == "CMPL":
            ann.append("EDIT:CMPL")
        c.create_text(150, 18, anchor="w", text="  ".join(ann), fill=VFD, font=self.f_small)
        m = self.measfunc()
        rtxt = "AUTO" if self.auto_range else fmt_range(RANGES[m][self.range_idx[m]], UNIT[m])
        c.create_text(W * 0.60, 44, text=rtxt, fill=VFD, font=self.f_small)

        c.create_text(W * 0.61, 90, anchor="e", text=self._main_text(), fill=VFD, font=self.f_big)
        if not (self.output_on or self.sweeping):
            state = "OFF"
        else:
            state = "CMPL" if (self.in_cmpl and self.blink) else ""
        c.create_text(W * 0.72, 90, anchor="w", text=state, fill=VFD, font=self.f_big)
        self._draw_sub(c, 140)
        c.create_text(W - 60, 172, anchor="e", text="2400  SourceMeter",
                      fill="white", font=("Arial", fs(13), "bold"))
        if UI != 1.0:
            c.scale("all", 0, 0, UI, UI)

    def _draw_sub(self, c, y):
        if self.flash_msg and time.time() < self.flash_until:
            c.create_text(self.DW / 2, y, text=self.flash_msg, fill=VFD, font=self.f_mid)
            return
        if self.toggle_view:
            if self.last and (self.output_on or self.sweeping):
                v, i = self.last
                r = v / i if i else float("inf")
                txt = f"V:{eng(v, 'V', 6)}  I:{eng(i, 'A', 6)}"
                if self.meas != "R":
                    txt += f"  R:{eng(r, 'Ω', 4)}"
            else:
                txt = "V: ------    I: ------"
            c.create_text(self.DW / 2, y, text=txt, fill=VFD, font=self.f_small if len(txt) > 40 else self.f_mid)
            return
        s = self.src
        shown = self.ramp_now if self.ramping else self.level[s]
        stxt, sidx = fmt_edit(shown, UNIT[s], self.cursor[s], signed=True)
        mtxt, midx = fmt_edit(self.cmpl[s], UNIT[self.measfunc()], self.ccursor[s], signed=False)
        head = "Vsrc:" if s == "VOLT" else "Isrc:"
        mid = "  Cmpl:"
        line = head + stxt + mid + mtxt
        hl = len(head) + sidx if self.edit_target == "SRC" else len(head + stxt + mid) + midx
        cw = self.f_mid.measure("0") / UI      # measure() is in real pixels
        x0 = (self.DW - len(line) * cw) / 2
        for k, ch in enumerate(line):
            cx = x0 + k * cw
            col = VFD
            if k == hl and self.blink:
                c.create_rectangle(cx, y - 14, cx + cw, y + 14, fill=VFD, outline="")
                col = LCD
            c.create_text(cx + cw / 2, y, text=ch, fill=col, font=self.f_mid)

    def _main_text(self):
        unit = {"V": "V", "I": "A", "R": "Ω"}[self.meas]
        sig = DIGITS[self.digit_idx][0]
        if not (self.output_on or self.sweeping) or self.last is None:
            ref = None
            if self.meas == "I":
                ref = self.cmpl["VOLT"] if self.src == "VOLT" else self.level["CURR"]
            elif self.meas == "V":
                ref = self.cmpl["CURR"] if self.src == "CURR" else self.level["VOLT"]
            return "-." + "-" * sig + " " + PREFIX[exp3_of(ref)] + unit
        x = self._raw_reading()
        if self.meas in self.rel and math.isfinite(x):
            x -= self.rel[self.meas]
        return eng(x, unit, sig)

    def _raw_reading(self):
        v, i = self.last
        if self.meas == "V":
            return v
        if self.meas == "I":
            return i
        return v / i if i else float("inf")

    def flash(self, msg, secs=2.0):
        self.flash_msg, self.flash_until = msg, time.time() + secs
        self.redraw()

    def _blink_tick(self):
        self.blink = not self.blink
        self.redraw()
        self.root.after(500, self._blink_tick)

    # ======================================================================= comms helpers
    def measfunc(self):
        return "CURR" if self.src == "VOLT" else "VOLT"

    def send(self, *cmds):
        if self.inst is None:
            return True
        try:
            for c in cmds:
                self.inst.write(c)
            return True
        except Exception as e:
            self.comm_error(e)
            return False

    def comm_error(self, e):
        self.status.config(text=f"Communication error: {e}")
        self.flash("COMM ERROR", 3)

    def cmd_source_config(self):
        s, m = self.src, self.measfunc()
        return [f":SOUR:FUNC {s}", f":SOUR:{s}:MODE FIX", f":SOUR:{s}:RANG:AUTO ON",
                f":SOUR:{s}:LEV {self.level[s]:.6E}", f":SENS:{m}:PROT {self.cmpl[s]:.6E}"] \
            + self.cmd_range()

    def cmd_range(self):
        m = self.measfunc()
        if self.auto_range:
            return [f":SENS:{m}:RANG:AUTO ON"]
        return [f":SENS:{m}:RANG {RANGES[m][self.range_idx[m]]:.6E}"]

    # ======================================================================= connection
    def refresh_ports(self):
        ports = sorted((p.device for p in serial.tools.list_ports.comports()), key=port_key) \
            if serial else []
        self.port_cb["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def key_power(self):
        if self.inst:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        try:
            if self.sim_var.get():
                inst = Simulated2400()
            else:
                if not self.port_var.get():
                    messagebox.showerror("Connect", "Choose a serial port first.")
                    return
                inst = Keithley2400(self.port_var.get(), self.baud_var.get(), self.term_var.get())
            idn = inst.query("*IDN?")
        except Exception as e:
            try:
                inst.close()
            except Exception:
                pass
            if isinstance(e, PermissionError) or "Permission denied" in str(e):
                messagebox.showerror("Connect failed", f"{e}\n\nYour user may not open serial "
                                     "ports. In a terminal run:\n\n  sudo usermod -aG dialout $USER"
                                     "\n\nthen log out and back in (or reboot).")
                return
            messagebox.showerror("Connect failed",
                                 f"{e}\n\nCheck: RS-232 selected in MENU > COMMUNICATION, "
                                 "same baud and terminator, flow control NONE, "
                                 "null-modem vs straight cable.")
            return
        self.inst = inst
        cmds = ["*RST"] if self.reset_var.get() else []
        cmds += ["*CLS",
                 f":ROUT:TERM {'REAR' if self.rear else 'FRON'}",
                 f":SYST:RSEN {'ON' if self.fourw_var.get() else 'OFF'}",
                 ":SENS:FUNC:CONC ON", ':SENS:FUNC "VOLT","CURR"',
                 ":FORM:ELEM VOLT,CURR,STAT",
                 f":SENS:CURR:NPLC {SPEEDS[self.speed_idx][1]}",
                 ":SENS:AVER:TCON REP", ":SENS:AVER:COUN 10",
                 f":SENS:AVER {'ON' if self.filter_on else 'OFF'}"]
        cmds += self.cmd_source_config() + [":OUTP OFF"]
        if not self.send(*cmds):
            return
        try:
            self.inst.query("*OPC?")
        except Exception as e:
            self.comm_error(e)
        self.output_on = False
        self.out_btn.set_fill(KEY_DARK)
        self.pwr_btn.set_fill("#bfe8c4")
        self.idn_lbl.config(text=idn)
        self.conn_btn.config(text="Disconnect")
        where = "simulator" if self.sim_var.get() else f"{self.port_var.get()} @ {self.baud_var.get()}"
        self.status.config(text=f"Connected ({where}): {idn}")
        self.flash("CONNECTED")

    def disconnect(self):
        self.sweep_abort.set()
        self.ramp_abort.set()
        inst, self.inst = self.inst, None
        if inst:
            try:
                with inst.lock:
                    inst.write(":OUTP OFF")
                    inst.write(":SYST:LOC")
            except Exception:
                pass
            try:
                inst.close()
            except Exception:
                pass
        self.output_on = False
        self.out_btn.set_fill(KEY_DARK)
        self.pwr_btn.set_fill("#f2f2ef")
        self.conn_btn.config(text="Connect")
        self.idn_lbl.config(text="Not connected")
        self.status.config(text="Disconnected (output turned off, instrument in LOCAL)")
        self.flash("DISCONNECTED")

    def check_errors(self):
        if not self.inst:
            return
        errs = []
        try:
            for _ in range(20):
                r = self.inst.query(":SYST:ERR?")
                if r.lstrip("+").startswith("0,"):
                    break
                errs.append(r)
        except Exception as e:
            errs.append(f"(query failed: {e})")
        messagebox.showinfo("Instrument error queue", "\n".join(errs) or "No errors")

    # ======================================================================= keys
    def key_meas(self, which):
        self.meas = which
        self.flash({"V": "MEAS: VOLTS", "I": "MEAS: AMPS", "R": "MEAS: OHMS (V/I)"}[which], 1.2)

    def key_fctn(self):
        self.key_meas({"V": "I", "I": "R", "R": "V"}[self.meas])

    def key_source(self, func):
        self.src_var.set(func)
        if func == self.src or self.sweeping:
            self.src_var.set(self.src)
            return
        if self.output_on:
            self.set_output(False)
        self.src = func
        self.meas = "I" if func == "VOLT" else "V"
        self.send(*self.cmd_source_config())
        self.sync_entries()
        self.flash("SOURCE: VOLTS" if func == "VOLT" else "SOURCE: AMPS", 1.2)

    def key_updown(self, d):
        s = self.src
        if self.edit_target == "SRC":
            lim = SRC_LIMIT[s]
            v = round(max(-lim, min(lim, self.level[s] + d * 10 ** self.cursor[s])), 12)
            self.level[s] = v
            self.set_level_now_or_ramp()
        else:
            lo, hi = CMPL_LIMIT[s]
            v = round(max(lo, min(hi, self.cmpl[s] + d * 10 ** self.ccursor[s])), 12)
            self.cmpl[s] = v
            self.send(f":SENS:{self.measfunc()}:PROT {v:.6E}")
        self.sync_entries()
        self.redraw()

    def key_cursor(self, d):
        s = self.src
        if self.edit_target == "SRC":
            lo, hi = SRC_CURSOR[s]
            self.cursor[s] = max(lo, min(hi, self.cursor[s] + d))
        else:
            lo, hi = CMPL_CURSOR[s]
            self.ccursor[s] = max(lo, min(hi, self.ccursor[s] + d))
        self.blink = True
        self.redraw()

    def key_edit(self):
        self.edit_target = "CMPL" if self.edit_target == "SRC" else "SRC"
        self.flash("EDIT: COMPLIANCE" if self.edit_target == "CMPL" else "EDIT: SOURCE", 1.2)

    def key_toggle(self):
        self.toggle_view = not self.toggle_view
        self.redraw()

    def key_output(self):
        if self.sweeping:
            self.key_exit()
            return
        if self.ramping:
            if self.ramp_down:            # second press while ramping down: switch off now
                self.set_output(False)
            else:                         # pressed during ramp-up: ramp back down and switch off
                self.ramp_down = True
                self.flash("RAMPING DOWN", 1.5)
            return
        if self.ramp_var.get() and self.inst:
            self.start_ramp(down=self.output_on)
            return
        self.set_output(not self.output_on)

    # ---------------------------------------------------------------- ramp
    def set_level_now_or_ramp(self):
        if self.ramp_var.get() and self.output_on and self.inst:
            if not self.ramping:
                self.start_ramp(down=False)   # the worker follows self.level[src]
        elif not self.ramping:
            self.send(f":SOUR:{self.src}:LEV {self.level[self.src]:.6E}")
            self.ramp_now = self.level[self.src]

    def _on_rate(self, *_):
        try:
            r = abs(parse_eng(self.rate_var.get()))
            if r > 0:
                self.ramp_rate = r
        except ValueError:
            pass

    def start_ramp(self, down):
        """Ramp from the present level (0 if output is off) to the set level, or to 0 then OFF."""
        if self.sweeping or self.ramping or not self.inst:
            return
        s, inst = self.src, self.inst
        start = self.ramp_now if self.output_on else 0.0
        if not self.output_on:
            try:
                with inst.lock:
                    inst.write(f":SOUR:{s}:LEV 0")
                    inst.write(":OUTP ON")
                    self.output_on = True
            except Exception as e:
                self.comm_error(e)
                return
            self.out_btn.set_fill("#a32424")
        self.ramp_abort.clear()
        self.ramping, self.ramp_down, self.ramp_now = True, down, start
        self.flash(f"RAMP {self.ramp_rate:g} {UNIT[s]}/s", 1.2)
        threading.Thread(target=self._ramp_worker, args=(inst, s, start), daemon=True).start()

    def _ramp_worker(self, inst, s, cur):
        dt, msg, switched_off = 0.1, "RAMP DONE", False
        try:
            while True:
                if self.ramp_abort.is_set():
                    msg = "RAMP STOPPED"
                    break
                target = 0.0 if self.ramp_down else self.level[s]
                step = self.ramp_rate * dt
                cur = target if abs(target - cur) <= step else cur + math.copysign(step, target - cur)
                inst.write(f":SOUR:{s}:LEV {cur:.6E}")
                self.ramp_now = cur
                if cur == (0.0 if self.ramp_down else self.level[s]):
                    break
                time.sleep(dt)
            if self.ramp_down and not self.ramp_abort.is_set():
                with inst.lock:
                    inst.write(":OUTP OFF")
                    self.output_on = False
                    inst.write(f":SOUR:{s}:LEV {self.level[s]:.6E}")   # keep setpoint for next ON
                switched_off, msg = True, "RAMPED DOWN - OUTPUT OFF"
        except Exception as e:
            msg = f"RAMP ERROR: {e}"
        self.q.put(("ramp_done", msg, switched_off, cur))

    def set_output(self, on):
        if not on and self.ramping:
            self.ramp_abort.set()
        if on and not self.inst:
            self.flash("NOT CONNECTED - PRESS POWER")
            return
        if self.inst:
            try:
                with self.inst.lock:          # never interleave with a :READ?
                    self.inst.write(":OUTP ON" if on else ":OUTP OFF")
                    self.output_on = on
            except Exception as e:
                self.comm_error(e)
                return
        else:
            self.output_on = on
        if not on:
            self.in_cmpl = False
        self.out_btn.set_fill("#a32424" if on else KEY_DARK)
        if on:
            self.ramp_now = self.level[self.src]
        self.redraw()

    def key_terminals(self):
        if self.output_on:
            self.set_output(False)
        self.rear = not self.rear
        self.send(f":ROUT:TERM {'REAR' if self.rear else 'FRON'}")
        self.flash("REAR TERMINALS" if self.rear else "FRONT TERMINALS")

    def key_range(self, d):
        m = self.measfunc()
        if self.auto_range:
            self.auto_range = False
        else:
            self.range_idx[m] = max(0, min(len(RANGES[m]) - 1, self.range_idx[m] + d))
        self.send(*self.cmd_range())
        self.flash("RANGE " + fmt_range(RANGES[m][self.range_idx[m]], UNIT[m]), 1.2)

    def key_auto(self):
        self.auto_range = not self.auto_range
        self.send(*self.cmd_range())
        self.flash("AUTO RANGE ON" if self.auto_range else "AUTO RANGE OFF", 1.2)

    def key_local(self):
        self.send(":SYST:LOC")
        self.flash("LOCAL (next command returns to REMOTE)")

    def key_rel(self):
        if self.meas in self.rel:
            del self.rel[self.meas]
            self.flash("REL OFF", 1.2)
        elif self.last and (self.output_on or self.sweeping):
            x = self._raw_reading()
            if math.isfinite(x):
                self.rel[self.meas] = x
                self.flash("REL ON", 1.2)
        else:
            self.flash("REL: NO READING", 1.2)

    def key_filter(self):
        self.filter_on = not self.filter_on
        self.send(f":SENS:AVER {'ON' if self.filter_on else 'OFF'}")
        self.flash("FILTER ON (REPEAT, 10)" if self.filter_on else "FILTER OFF", 1.5)

    def key_limit(self):
        self.flash("LIMIT: NOT USED - SET Cmpl WITH EDIT")

    def key_digits(self):
        self.digit_idx = (self.digit_idx + 1) % len(DIGITS)
        self.flash(f"DIGITS: {DIGITS[self.digit_idx][1]}", 1.2)

    def key_speed(self):
        self.set_speed((self.speed_idx + 1) % len(SPEEDS))

    def set_speed(self, idx):
        self.speed_idx = idx
        name, nplc = SPEEDS[idx]
        self.speed_var.set(name)
        self.send(f":SENS:CURR:NPLC {nplc}")
        self.flash(f"SPEED: {name} ({nplc:g} PLC)", 1.5)

    def key_store(self):
        if self.log_writer:
            self.stop_log()
        else:
            self.start_log()

    def key_recall(self):
        self.flash(f"{self.log_count} LOGGED / {len(self.ts)} IN PLOT")

    def key_trig(self):
        inst = self.inst
        if not inst or not self.output_on or self.sweeping:
            self.flash("TRIG: OUTPUT MUST BE ON")
            return

        def one():
            try:
                with inst.lock:
                    if self.output_on:
                        self.q.put(("data", time.time()) + self._read_once(inst))
            except Exception as e:
                self.q.put(("err", str(e)))
        threading.Thread(target=one, daemon=True).start()

    def key_config(self):
        self.nb.select(1)
        self.flash("CONFIG: SEE SOURCE/MEASURE TAB", 1.5)

    def key_menu(self):
        self.nb.select(0)
        self.flash("MENU: SEE CONNECTION TAB", 1.5)

    def key_exit(self):
        if self.sweeping:
            self.sweep_abort.set()
        if self.ramping:
            self.ramp_abort.set()
        self.flash_until = 0
        self.redraw()

    # ======================================================================= entries
    def sync_entries(self):
        s = self.src
        self.level_var.set(f"{self.level[s]:.6g}")
        self.cmpl_var.set(f"{self.cmpl[s]:.6g}")
        self.level_lbl.config(text=f"Level ({UNIT[s]})")
        self.cmpl_lbl.config(text=f"Compliance ({UNIT[self.measfunc()]})")
        self.rate_lbl.config(text=f"Ramp rate ({UNIT[s]}/s)")

    def apply_entries(self):
        s = self.src
        try:
            lv = parse_eng(self.level_var.get())
            cm = abs(parse_eng(self.cmpl_var.get()))
        except ValueError:
            messagebox.showerror("Invalid value", "Use numbers like 1.5, 100m, 105u or 2e-3.")
            return
        lo, hi = CMPL_LIMIT[s]
        if abs(lv) > SRC_LIMIT[s] or not lo <= cm <= hi:
            messagebox.showerror("Out of range",
                                 f"Level must be within ±{SRC_LIMIT[s]} {UNIT[s]}; "
                                 f"compliance {lo:g} to {hi:g} {UNIT[self.measfunc()]}.")
            return
        self.level[s], self.cmpl[s] = lv, cm
        self.send(f":SENS:{self.measfunc()}:PROT {cm:.6E}")
        self.set_level_now_or_ramp()
        self.sync_entries()
        self.flash("SETTINGS APPLIED", 1.2)

    def on_fourw(self):
        if self.output_on:
            self.set_output(False)
        self.send(f":SYST:RSEN {'ON' if self.fourw_var.get() else 'OFF'}")
        self.flash("4-WIRE SENSE" if self.fourw_var.get() else "2-WIRE SENSE", 1.2)

    def _on_interval(self, *_):
        try:
            self.interval = max(0.05, float(self.interval_var.get()))
        except ValueError:
            pass

    # ======================================================================= acquisition
    @staticmethod
    def _read_once(inst):
        parts = [float(x) for x in inst.query(":READ?").split(",")]
        return parts[0], parts[1], int(parts[2]) if len(parts) > 2 else 0

    def _poll_loop(self):
        while not self.stop_evt.is_set():
            t_start = time.time()
            inst = self.inst
            if inst is not None and self.output_on and not self.sweeping:
                try:
                    with inst.lock:
                        if self.output_on and self.inst is inst:
                            self.q.put(("data", time.time()) + self._read_once(inst))
                except Exception as e:
                    self.q.put(("err", str(e)))
                    self.stop_evt.wait(1.0)
            self.stop_evt.wait(max(0.02, self.interval - (time.time() - t_start)))

    def start_sweep(self):
        if not self.inst:
            self.flash("NOT CONNECTED - PRESS POWER")
            return
        if self.sweeping or self.ramping:
            return
        if self.output_on:
            self.flash("TURN OUTPUT OFF BEFORE SWEEP")
            return
        s = self.src
        try:
            start = parse_eng(self.sw_vars["Start"].get())
            stop = parse_eng(self.sw_vars["Stop"].get())
            n = int(self.sw_vars["Points"].get())
            delay = float(self.sw_vars["Delay (s)"].get())
            if n < 2 or delay < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Sweep", "Check Start/Stop (numbers), Points (≥2), Delay (≥0).")
            return
        if max(abs(start), abs(stop)) > SRC_LIMIT[s]:
            messagebox.showerror("Sweep", f"Sweep limits must be within ±{SRC_LIMIT[s]} {UNIT[s]}.")
            return
        levels = [start + (stop - start) * k / (n - 1) for k in range(n)]
        self.clear_plot()
        self.plotmode_var.set("I vs V")
        self.build_axes()
        self.sweep_abort.clear()
        self.sweeping = True
        self.out_btn.set_fill("#a32424")
        self.flash(f"SWEEP {n} PTS", 1.2)
        threading.Thread(target=self._sweep_worker, args=(self.inst, levels, delay, s),
                         daemon=True).start()

    def _sweep_worker(self, inst, levels, delay, s):
        msg = "SWEEP DONE"
        try:
            inst.write(f":SOUR:{s}:LEV {levels[0]:.6E}")
            inst.write(":OUTP ON")
            for lv in levels:
                if self.sweep_abort.is_set():
                    msg = "SWEEP ABORTED"
                    break
                inst.write(f":SOUR:{s}:LEV {lv:.6E}")
                time.sleep(delay)
                self.q.put(("data", time.time()) + self._read_once(inst))
        except Exception as e:
            msg = f"SWEEP ERROR: {e}"
        finally:
            try:
                inst.write(":OUTP OFF")
                inst.write(f":SOUR:{s}:LEV {self.level[s]:.6E}")
            except Exception:
                pass
            self.q.put(("sweep_done", msg))

    def _process_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "data":
                    self._on_data(*msg[1:])
                elif msg[0] == "err":
                    self.comm_error(msg[1])
                elif msg[0] == "ramp_done":
                    _, text, off, cur = msg
                    self.ramping = False
                    if off or not self.output_on:
                        self.in_cmpl = False
                        self.out_btn.set_fill(KEY_DARK)
                    elif text == "RAMP STOPPED":
                        self.level[self.src] = cur        # stopped part-way: that is the new setpoint
                        self.sync_entries()
                    self.flash(text, 2)
                elif msg[0] == "sweep_done":
                    self.sweeping = False
                    self.output_on = False
                    self.in_cmpl = False
                    self.out_btn.set_fill(KEY_DARK)
                    self.plot_dirty = True
                    self.flash(msg[1], 3)
        except queue.Empty:
            pass
        self.root.after(50, self._process_queue)

    def _on_data(self, ts, v, i, stat):
        self.last = (v, i)
        self.in_cmpl = bool(stat & 8)
        if self.t0 is None:
            self.t0 = ts
        self.ts.append(ts - self.t0)
        self.vs.append(v)
        self.is_.append(i)
        if len(self.ts) > MAX_POINTS:
            cut = MAX_POINTS // 10
            del self.ts[:cut], self.vs[:cut], self.is_[:cut]
        self.plot_dirty = True
        if self.log_writer:
            r = v / i if i else float("inf")
            self.log_writer.writerow([
                datetime.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
                f"{ts - self.log_t0:.3f}", f"{v:.6E}", f"{i:.6E}", f"{r:.6E}",
                self.src, f"{self.level[self.src]:.6E}", f"{self.cmpl[self.src]:.6E}",
                int(self.in_cmpl), "sweep" if self.sweeping else "ramp" if self.ramping else "monitor"])
            self.log_fh.flush()
            self.log_count += 1
            self.log_lbl.config(text=f"Logging: {self.log_count} readings")
        self.status.config(text=f"V = {eng(v, 'V', 7)}    I = {eng(i, 'A', 7)}"
                                + ("    IN COMPLIANCE" if self.in_cmpl else ""))
        self.redraw()

    # ======================================================================= logging
    def browse_log(self):
        fn = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=datetime.datetime.now().strftime("k2400_%Y%m%d_%H%M%S.csv"))
        if fn:
            self.logfile_var.set(fn)

    def start_log(self):
        if not self.logfile_var.get():
            self.browse_log()
            if not self.logfile_var.get():
                return
        try:
            self.log_fh = open(self.logfile_var.get(), "a", newline="")
        except OSError as e:
            messagebox.showerror("Log file", str(e))
            return
        self.log_writer = csv.writer(self.log_fh)
        if self.log_fh.tell() == 0:
            self.log_writer.writerow(["timestamp", "elapsed_s", "voltage_V", "current_A",
                                      "resistance_ohm", "source_func", "source_level",
                                      "compliance", "in_compliance", "mode"])
        self.log_t0, self.log_count = time.time(), 0
        self.log_btn.config(text="Stop logging  (STORE)")
        self.log_lbl.config(text=f"Logging to {self.logfile_var.get()}")
        self.flash("STORE: LOGGING ON", 1.2)

    def stop_log(self):
        if self.log_fh:
            self.log_fh.close()
        self.log_fh = self.log_writer = None
        self.log_btn.config(text="Start logging  (STORE)")
        self.log_lbl.config(text=f"Stopped - {self.log_count} readings saved")
        self.flash("STORE: LOGGING OFF", 1.2)

    # ======================================================================= plot
    def build_axes(self):
        self.fig.clear()
        if self.plotmode_var.get() == "V & I vs time":
            self.ax1 = self.fig.add_subplot(211)
            self.ax2 = self.fig.add_subplot(212, sharex=self.ax1)
            (self.l1,) = self.ax1.plot([], [], color="#1f77b4", lw=1.2)
            (self.l2,) = self.ax2.plot([], [], color="#d62728", lw=1.2)
            self.ax1.set_ylabel("Voltage")
            self.ax2.set_ylabel("Current")
            self.ax2.set_xlabel("Time (s)")
            self.ax1.yaxis.set_major_formatter(EngFormatter(unit="V"))
            self.ax2.yaxis.set_major_formatter(EngFormatter(unit="A"))
            self.ax1.tick_params(labelbottom=False)
            axes = (self.ax1, self.ax2)
        else:
            self.ax1, self.ax2 = self.fig.add_subplot(111), None
            (self.l1,) = self.ax1.plot([], [], "o-", ms=3, lw=1, color="#d62728")
            self.l2 = None
            self.ax1.set_xlabel("Voltage")
            self.ax1.set_ylabel("Current")
            self.ax1.xaxis.set_major_formatter(EngFormatter(unit="V"))
            self.ax1.yaxis.set_major_formatter(EngFormatter(unit="A"))
            axes = (self.ax1,)
        for ax in axes:
            ax.grid(True, alpha=0.3)

        self.canvas.draw_idle()

    def refresh_plot(self):
        step = max(1, len(self.ts) // 5000)
        t, v, i = self.ts[::step], self.vs[::step], self.is_[::step]
        if self.l2 is not None:
            self.l1.set_data(t, v)
            self.l2.set_data(t, i)
            axes = (self.ax1, self.ax2)
        else:
            self.l1.set_data(v, i)
            axes = (self.ax1,)
        for ax in axes:
            ax.relim()
            ax.autoscale_view()
        self.canvas.draw_idle()

    def _plot_tick(self):
        if self.plot_dirty:
            self.plot_dirty = False
            self.refresh_plot()
        self.root.after(400, self._plot_tick)

    def clear_plot(self):
        keep = []      # handle (log) readings still queued from before the clear, then wipe
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "data":
                    self._on_data(*m[1:])
                else:
                    keep.append(m)
        except queue.Empty:
            pass
        for m in keep:
            self.q.put(m)
        self.t0, self.ts, self.vs, self.is_ = None, [], [], []
        self.refresh_plot()

    def save_png(self):
        fn = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if fn:
            self.fig.savefig(fn, dpi=150)

    def save_buffer(self):
        fn = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not fn:
            return
        with open(fn, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_s", "voltage_V", "current_A"])
            for row in zip(self.ts, self.vs, self.is_):
                w.writerow([f"{row[0]:.3f}", f"{row[1]:.6E}", f"{row[2]:.6E}"])

    # ======================================================================= shutdown
    def on_close(self):
        self.stop_evt.set()
        self.sweep_abort.set()
        if self.inst:
            self.disconnect()
        if self.log_fh:
            self.log_fh.close()
        self.root.destroy()


def xft_dpi(root):
    """DPI that Xft uses for fonts on X11 (Xft.dpi resource), or None elsewhere."""
    if root.tk.call("tk", "windowingsystem") != "x11":
        return None
    try:
        out = subprocess.run(["xrdb", "-query"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            if line.startswith("Xft.dpi:"):
                return float(line.split(":", 1)[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return root.winfo_fpixels("1i")       # Xft falls back to the X screen DPI


def auto_scale(root, size=None):
    """UI factor that makes the window fill ~85 % of the screen (or the given WxH size)."""
    if size:
        w, h = size
        k = 1.0
    else:
        h = root.winfo_screenheight()
        w = min(root.winfo_screenwidth(), h * 16 // 10)   # several monitors report one wide screen
        k = 0.85
    ui = min(k * w / BASE_W, k * h / BASE_H)
    return max(0.6, min(3.0, math.floor(ui * 20) / 20))


def init_scaling(root, ui):
    """Scale fonts by `ui` like the drawings. Tk scaling (pixels per point) does this on
    Windows/macOS, but on X11 Xft renders point sizes at its own Xft.dpi, so we
    correct the sizes with FONT_K instead."""
    global UI, FONT_K
    xdpi = xft_dpi(root)
    UI = ui
    root.tk.call("tk", "scaling", ui * 96 / 72)
    FONT_K = ui * 96 / xdpi if xdpi else 1.0
    for name in tkfont.names(root):         # fonts of the ttk widgets (tabs, buttons, ...)
        f = tkfont.nametofont(name, root=root)
        size = f.cget("size")
        f.configure(size=fs(size if size > 0 else -size * 72 / 96))


def main():
    ap = argparse.ArgumentParser(description="Keithley 2400 RS-232 front-panel GUI")
    ap.add_argument("--sim", action="store_true", help="start connected to the built-in simulator")
    ap.add_argument("--scale", type=float, default=0,
                    help="UI scale factor, e.g. 0.8, 1, 1.5, 2 (default: auto from screen resolution)")
    ap.add_argument("--geometry", metavar="WxH",
                    help="window size in pixels, e.g. 1600x1000 (the UI is scaled to fit it)")
    args = ap.parse_args()
    size = None
    if args.geometry:
        try:
            size = tuple(int(x) for x in args.geometry.lower().split("x"))
            assert len(size) == 2 and min(size) > 0
        except (ValueError, AssertionError):
            ap.error("--geometry must look like 1600x1000")
    root = tk.Tk()
    init_scaling(root, args.scale if args.scale > 0 else auto_scale(root, size))
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root, args)

    def fix_size():
        # set after the window is mapped (some window managers ignore it before) and keep
        # it fixed, so the plot cannot keep growing the window
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = size or (min(root.winfo_reqwidth(), sw), min(root.winfo_reqheight(), sh))
        root.geometry(f"{w}x{h}")
    root.after(100, fix_size)
    root.mainloop()


if __name__ == "__main__":
    main()