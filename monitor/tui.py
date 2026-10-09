"""lmon - terminal dashboard: same real data as the web UI, plus an AI-workload page. No server needed.

    lmon                                  # interactive, runs until Ctrl+C
    lmon --page ai                        # start on the AI page (system | ai | advanced)
    lmon --once --page ai --width 160 --height 45
    lmon --graph block                    # block graphs, for fonts without braille
    lmon --web                            # run the web dashboard server instead

Keys: 1 2 3 / Tab / <- -> pages, wheel or up/down scroll, PgUp/PgDn, click a process to select it,
/ filter, s sort, r reverse, t measure tokens/s, c colour theme, Esc clear. Only Ctrl+C exits.

Drawing uses WGL4 glyphs (present in Courier New / Consolas / Lucida Console, PuTTY's defaults) and,
in the default graph mode, braille - the same glyphs btop uses. `--graph block` avoids braille.
"""
from __future__ import annotations

import argparse
import getpass
import math
import os
import re
import select
import signal
import sys
import termios
import time
import tty

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

from . import userpaths
from .insights import Timeline, analyze, score, verdict
from .sampler import Sampler

FULL, LOWER, UPPER, SQ = "█", "▄", "▀", "■"
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
PAGES = (("pulse", "1"), ("system", "2"), ("ai", "3"), ("advanced", "4"), ("net", "5"))
MOUSE_ON, MOUSE_OFF = "\x1b[?1000h\x1b[?1006h", "\x1b[?1000l\x1b[?1006l"

# Colour themes (24-bit). Section colours are box titles; borders are a darker shade of them.
# LOW replaces any theme on 16-colour terminals, where dark hex shades collapse to black.
THEMES = {
    # lmon's own palette: aqua -> blue -> violet -> rose instead of the usual green -> yellow -> red
    "aurora": {
        "fg": "#d7dcf0", "mut": "#6b7394", "empty": "#232842", "sel": "#0b0d1a", "selbg": "#2b2350",
        "ok": "#3ef0c2", "warn": "#ffb454", "bad": "#ff4f7b", "hi": "#a78bfa",
        "cpu": "#3ef0c2", "mem": "#ffb454", "net": "#7aa2ff", "gpu": "#c3f73a", "ai": "#a78bfa",
        "proc": "#ff79c6", "disk": "#ffcb6b", "adv": "#7aa2ff", "tok": "#f472b6",
        "grad": ("#3ef0c2", "#7aa2ff", "#c084fc", "#ff4f7b"), "tgrad": ("#7aa2ff", "#c084fc", "#ff4f7b"),
        "mgrad": ("#ffb454", "#ff7a59", "#ff4f7b"), "g_avail": ("#1f8f74", "#3ef0c2"), "g_cached": ("#3b5bb5", "#7aa2ff"),
        "rx": ("#2a2f7a", "#7aa2ff", "#c7d7ff"), "tx": ("#5a1a5e", "#c084fc", "#f5c2ff"),
        "ggrad": ("#3a7d1a", "#c3f73a", "#ffe066"), "vgrad": ("#2a2f7a", "#a78bfa", "#e9ddff"),
        "heat": ("#0f1330", "#253a8c", "#3ef0c2", "#c084fc", "#ff4f7b"),
    },
    # btop's default palette: same gradients for cpu / temps / memory / network, red highlights
    "btop": {
        "fg": "#cccccc", "mut": "#707070", "empty": "#404040", "sel": "#ffffff", "selbg": "#6a2f2f",
        "ok": "#50f095", "warn": "#f2e266", "bad": "#fa1e1e", "hi": "#d05050",
        "cpu": "#8fc69a", "mem": "#c4c48a", "net": "#a8a0ff", "gpu": "#9fd58f", "ai": "#8ec7e6",
        "proc": "#e89696", "disk": "#e0b07a", "adv": "#8ec7e6", "tok": "#dcafde",
        "grad": ("#50f095", "#f2e266", "#fa1e1e"), "tgrad": ("#4897d4", "#5474e8", "#ff40b6"),
        "mgrad": ("#d9626d", "#ff4769"), "g_avail": ("#ffd77a", "#ffb814"), "g_cached": ("#74e6fc", "#26c5ff"),
        "rx": ("#231a63", "#4f43a3", "#b0a9de"), "tx": ("#510554", "#7d4180", "#dcafde"),
        "ggrad": ("#50f095", "#f2e266", "#fa1e1e"), "vgrad": ("#0b1a29", "#74e6fc", "#26c5ff"),
        "heat": ("#151a18", "#2f6b46", "#50f095", "#f2e266", "#fa1e1e"),
    },
    "neon": {
        "fg": "#cdd6e0", "mut": "#5d6b7a", "empty": "#1c2630", "sel": "#03070a", "selbg": "#0e3b2e",
        "ok": "#00ff9f", "warn": "#ffe600", "bad": "#ff2a6d", "hi": "#00e8ff",
        "cpu": "#00ff9f", "mem": "#ffe600", "net": "#b65cff", "gpu": "#7cff4f", "ai": "#00e8ff",
        "proc": "#ff2a6d", "disk": "#ff9e00", "adv": "#00e8ff", "tok": "#ff5cf0",
        "grad": ("#00ff9f", "#ffe600", "#ff2a6d"), "tgrad": ("#00b3ff", "#b65cff", "#ff2a6d"),
        "mgrad": ("#ffe600", "#ff9e00", "#ff2a6d"), "g_avail": ("#008c57", "#00ff9f"), "g_cached": ("#007f8c", "#00e8ff"),
        "rx": ("#3a1c80", "#9d4dff", "#e2b8ff"), "tx": ("#6b0f3a", "#ff2a6d", "#ffb3cc"),
        "ggrad": ("#1f6b2a", "#7cff4f", "#ffe600"), "vgrad": ("#123d66", "#00b3ff", "#9df3ff"),
        "heat": ("#0a1016", "#004d40", "#00ff9f", "#ffe600", "#ff2a6d"),
    },
    # the softer palette of the first redesign
    "classic": {
        "fg": "#d8dee9", "mut": "#7b8796", "empty": "#2a313b", "sel": "#0b0e13", "selbg": "#1f3a4a",
        "ok": "#6fdc8c", "warn": "#f2c94c", "bad": "#ff5f6d", "hi": "#5fd7ff",
        "cpu": "#6fdc8c", "mem": "#f2c94c", "net": "#b48eff", "gpu": "#a6e22e", "ai": "#5fd7ff",
        "proc": "#ff7a90", "disk": "#ffa45c", "adv": "#5fd7ff", "tok": "#ff9ff3",
        "grad": ("#6fdc8c", "#f2c94c", "#ff5f6d"), "tgrad": ("#5fb4ff", "#b48eff", "#ff5f6d"),
        "mgrad": ("#e0b83c", "#f2c94c", "#ff8f5f"), "g_avail": ("#3d7a4d", "#6fdc8c"), "g_cached": ("#33778c", "#5fd7ff"),
        "rx": ("#5a3fb8", "#c9a6ff"), "tx": ("#b8406a", "#ff9fb0"),
        "ggrad": ("#3fa86b", "#a6e22e", "#f2c94c"), "vgrad": ("#2a6f99", "#5fd7ff"),
        "heat": ("#161b22", "#2f5d40", "#6fdc8c", "#f2c94c", "#ff5f6d"),
    },
}
LOW = {
    "fg": "white", "mut": "bright_black", "empty": "bright_black", "sel": "black", "selbg": "blue",
    "ok": "green", "warn": "yellow", "bad": "red", "hi": "cyan",
    "cpu": "green", "mem": "yellow", "net": "magenta", "gpu": "green", "ai": "cyan",
    "proc": "red", "disk": "yellow", "adv": "cyan", "tok": "magenta",
    "grad": ("green", "yellow", "red"), "tgrad": ("cyan", "magenta", "red"), "mgrad": ("yellow", "yellow", "red"),
    "g_avail": ("green", "green"), "g_cached": ("cyan", "cyan"),
    "rx": ("magenta", "magenta"), "tx": ("red", "red"), "ggrad": ("green", "green", "yellow"), "vgrad": ("cyan", "cyan"),
    "heat": ("black", "blue", "green", "yellow", "red"),
}
DEFAULT_THEME = "aurora"
P = {"low": False, "theme": DEFAULT_THEME, **THEMES[DEFAULT_THEME]}
GRAPH = {"mode": "braille"}
THEME_FILE = os.path.join(userpaths.home(), ".config/lmon/theme")


def use_palette(color_system, theme=None):
    theme = theme if theme in THEMES else P.get("theme", DEFAULT_THEME)
    low = color_system in ("standard", "windows")
    P.clear()
    P.update({"low": low, "theme": theme, **(LOW if low else THEMES[theme])})


def next_theme():
    names = list(THEMES)
    return names[(names.index(P["theme"]) + 1) % len(names)] if P["theme"] in names else names[0]


def load_theme():
    try:
        with open(THEME_FILE) as f:
            name = f.read().strip()
        return name if name in THEMES else None
    except OSError:
        return None


def save_theme(name):
    try:
        os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
        with open(THEME_FILE, "w") as f:
            f.write(name + "\n")
        userpaths.give_back(THEME_FILE)
    except OSError:
        pass  # remembering the theme is a convenience, never an error


# ---------------------------------------------------------------- formatting helpers
def clean(s) -> str:
    """Strip control chars: journal/host/process text is untrusted and could carry terminal escape codes."""
    return _CTRL.sub("", "" if s is None else str(s))


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def grad(stops, f):
    """Colour at fraction f (0..1) of a gradient; stepped on 16-colour terminals."""
    f = clamp(f, 0.0, 1.0)
    if P["low"] or not stops[0].startswith("#"):
        return stops[min(len(stops) - 1, int(f * len(stops)))]
    x = f * (len(stops) - 1)
    i = min(int(x), len(stops) - 2)
    a, b, t = _rgb(stops[i]), _rgb(stops[i + 1]), x - i
    return "#{:02x}{:02x}{:02x}".format(*(round(p + (q - p) * t) for p, q in zip(a, b)))


def dim(c, f=0.55):
    if P["low"] or not c.startswith("#"):
        return c
    return "#{:02x}{:02x}{:02x}".format(*(round(v * f) for v in _rgb(c)))


def lvl(p):
    return P["mut"] if p is None else grad(P["grad"], p / 100)


def tcol(t):
    return P["fg"] if t is None else P["bad"] if t >= 80 else P["warn"] if t >= 70 else P["fg"]


def n(v, d=0):
    return "—" if v is None else f"{v:,.{d}f}"


def k(v):
    return "—" if v is None else (f"{v / 1e6:,.1f}M" if v >= 1e6 else f"{v / 1000:,.1f}K" if v >= 1000 else f"{v:,.0f}")


def deg(v):
    return "—" if v is None else f"{v:.0f}°C"


def gb(v):
    return "—" if v is None else f"{v:.1f}G" if v < 100 else f"{v:.0f}G"


def size_b(v):
    if v is None:
        return "—"
    for unit in ("B", "K", "M", "G", "T"):
        if v < 1024 or unit == "T":
            return f"{v:.0f}{unit}" if unit in ("B", "K") or v >= 100 else f"{v:.1f}{unit}"
        v /= 1024
    return "—"


def rate(mbps):
    if mbps is None:
        return "—"
    if mbps >= 1000:
        return f"{mbps / 1000:.2f} Gbps"
    return f"{mbps:.1f} Mbps" if mbps >= 1 else f"{mbps * 1000:.0f} Kbps"


def dur(s):
    if s is None:
        return "—"
    if s < 1:
        return f"{s * 1000:.0f}ms" if s >= 0.001 else f"{s * 1e6:.0f}µs"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}m"


def updays(s):
    s = int(s or 0)
    return f"{s // 86400}d {s % 86400 // 3600:02d}:{s % 3600 // 60:02d}"


def fixed(s, width, right=False):
    s = clean(s)
    if width <= 0:
        return ""
    if len(s) > width:
        s = s[:width - 1] + "…"
    return s.rjust(width) if right else s.ljust(width)


def note(s, style=None) -> Text:
    return Text(clean(s), style=style or P["mut"], no_wrap=True, overflow="ellipsis")


def runs(t: Text, cells):
    """Append (char, style) cells to t, merging equal styles into one span."""
    buf, cur = "", None
    for ch, st in cells:
        if st != cur and buf:
            t.append(buf, style=cur)
            buf = ""
        buf, cur = buf + ch, st
    if buf:
        t.append(buf, style=cur)
    return t


def meter(p, w, stops=None) -> Text:
    """btop-style meter: every filled cell takes the gradient colour of its position."""
    stops = stops or P["grad"]
    f = 0 if p is None else round(clamp(p) / 100 * w)
    return runs(Text(no_wrap=True), [(SQ, grad(stops, i / max(1, w - 1)) if i < f else P["empty"]) for i in range(w)])


def kv(rows, label_w=None) -> Table:
    t = Table.grid(expand=True)
    t.add_column(ratio=1, no_wrap=True, overflow="ellipsis", min_width=label_w)
    t.add_column(justify="right", no_wrap=True)
    for label, val, *st in rows:
        t.add_row(Text(clean(label), style=P["mut"]), Text(clean(val), style=st[0] if st else P["fg"]))
    return t


def stack(*parts) -> Layout:
    """Vertical layout; each part is (renderable, size) - size None = share the remaining rows."""
    lay = Layout()
    lay.split_column(*[Layout(r, size=s) if s else Layout(r, ratio=1) for r, s in parts])
    return lay


def row(*parts) -> Layout:
    """Horizontal layout; each part is (renderable, size or ('r', ratio))."""
    lay = Layout()
    lay.split_row(*[Layout(r, ratio=s[1]) if isinstance(s, tuple) else Layout(r, size=s) for r, s in parts])
    return lay


def scrollbar(total, vis, top):
    """(char, style) cells for a vertical scrollbar of `vis` rows."""
    if total <= vis or vis <= 0:
        return [(" ", None)] * max(0, vis)
    thumb = max(1, round(vis * vis / total))
    pos = round(top / max(1, total - vis) * (vis - thumb))
    return [("█", P["hi"]) if pos <= i < pos + thumb else ("│", P["empty"]) for i in range(vis)]


# ---------------------------------------------------------------- braille
_BL, _BR = (0x40, 0x04, 0x02, 0x01), (0x80, 0x20, 0x10, 0x08)  # dots of each column, bottom -> top


def _btable(down):
    left, right = (_BL[::-1], _BR[::-1]) if down else (_BL, _BR)
    return [[chr(0x2800 + sum(left[:a]) + sum(right[:b])) if a or b else " " for b in range(5)] for a in range(5)]


BRAILLE = {False: _btable(False), True: _btable(True)}


class Graph:
    """Area graph, newest sample on the right, coloured by height. Braille: 2 samples x 4 levels per cell;
    block: 1 sample x 2 levels. `down=True` grows from the top edge (mirrored lower half)."""

    def __init__(self, vals, stops, down=False, guides=True):
        self.vals, self.stops, self.down, self.guides = vals or [], stops, down, guides

    def __rich_console__(self, console, options):
        w, h = options.max_width, options.height or 4
        braille = GRAPH["mode"] == "braille"
        per, lv = (2, 4) if braille else (1, 2)
        vals = list(self.vals)[-w * per:]
        units = [0] * (w * per - len(vals)) + [0 if v is None else max(1 if v >= 0.5 else 0, round(clamp(v) / 100 * lv * h))
                                                 for v in vals]
        guides = {round(h * (1 - q)) for q in (.25, .5, .75)} if self.guides and h >= 6 and not self.down else set()
        half = UPPER if self.down else LOWER
        table = BRAILLE[self.down]
        for r in range(h):
            level = r if self.down else h - 1 - r
            base = level * lv
            color = grad(self.stops, level / max(1, h - 1))
            gap = ("·", P["empty"]) if r in guides else (" ", None)
            cells = []
            for c in range(w):
                if braille:
                    ch = table[min(4, max(0, units[2 * c] - base))][min(4, max(0, units[2 * c + 1] - base))]
                else:
                    u = units[c]
                    ch = FULL if u >= base + 2 else half if u == base + 1 else " "
                cells.append(gap if ch == " " else (ch, color))
            yield runs(Text(no_wrap=True), cells)


def spark(vals, w) -> Text:
    """One-row history: braille (2 samples x 4 levels per cell) coloured by load; block mode -> meter."""
    vals = list(vals or [])
    if GRAPH["mode"] != "braille":
        return meter(vals[-1] if vals else None, w)
    vals = vals[-2 * w:]
    vals = [0] * (2 * w - len(vals)) + vals
    cells = []
    for c in range(w):
        a, b = vals[2 * c], vals[2 * c + 1]
        da, db = (0 if v < 0.5 else max(1, round(clamp(v) / 25)) for v in (a, b))
        cells.append((chr(0x2800 + sum(_BL[:da]) + sum(_BR[:db])), lvl(max(a, b))) if da or db else ("⣀", P["empty"]))
    return runs(Text(no_wrap=True), cells)


# ---------------------------------------------------------------- renderables
class Box:
    """┌─┤ TITLE ├──────┤ info ├─┐ ... └──────┤ foot ├─┘ with a border in a darker shade of the section colour."""

    def __init__(self, title, body, color, info=None, foot=None):
        self.title, self.body, self.color, self.info, self.foot = clean(title), body, color, info, foot

    def _edge(self, console, w, bc, top):
        left, right = ("┌", "┐") if top else ("└", "┘")
        t = Text(left + "─", style=bc, no_wrap=True)
        tag = self.info if top else self.foot
        info = Text()
        if tag is not None:
            info = Text("┤ ", style=bc)
            info.append_text(tag if isinstance(tag, Text) else Text(clean(tag), style=P["fg"]))
            info.append(" ├─", style=bc)
        if top and self.title:
            if len(info) + len(self.title) + 10 > w:
                info = Text()
            t.append("┤ ", style=bc)
            t.append(self.title[:max(0, w - 8 - len(info))], style=f"bold {self.color}")
            t.append(" ├", style=bc)
        if len(info) + len(t) + 1 > w:
            info = Text()
        t.append("─" * max(0, w - len(t) - len(info) - 1), style=bc)
        t.append_text(info)
        t.append(right, style=bc)
        t.truncate(w)
        yield from t.render(console)
        yield Segment.line()

    def __rich_console__(self, console, options):
        w, h = options.max_width, options.height
        if w < 4:
            return
        bc = Style.parse(dim(self.color))
        inner = options.update(width=w - 2, height=None if h is None else max(0, h - 2))
        lines = console.render_lines(self.body, inner, pad=True) if h is None or h > 2 else []
        yield from self._edge(console, w, bc, True)
        side = Segment("│", bc)
        for line in lines:
            yield side
            yield from line
            yield side
            yield Segment.line()
        if h is None or h >= 2:
            yield from self._edge(console, w, bc, False)


class MeterLine:
    """label  ■■■■■■■■■■  tail  - the meter takes whatever width is left."""

    def __init__(self, label, pct, tail="", lw=7, stops=None, label_style=None):
        self.label, self.pct, self.tail, self.lw = clean(label), pct, clean(tail), lw
        self.stops, self.label_style = stops, label_style

    def __rich_console__(self, console, options):
        mw = max(2, options.max_width - self.lw - len(self.tail) - 2)
        t = Text(no_wrap=True, overflow="crop")
        t.append(f"{self.label:<{self.lw}.{self.lw}} ", style=self.label_style or P["mut"])
        t.append_text(meter(self.pct, mw, self.stops))
        t.append(f" {self.tail}", style=P["fg"])
        yield t


class Lines:
    """Rows produced by fn(width, height) - for column-aligned tables sized to their box."""

    def __init__(self, fn):
        self.fn = fn

    def __rich_console__(self, console, options):
        yield from self.fn(options.max_width, options.height or 999)


class ScrollList:
    """Scrollable list with an optional header row and a scrollbar; the offset lives in ui.scroll[name]."""

    def __init__(self, name, ui, items, header=None, empty="nothing to show"):
        self.name, self.ui, self.items, self.header, self.empty = name, ui, items, header, empty

    def __rich_console__(self, console, options):
        w, h = options.max_width, options.height or 10
        head = 1 if self.header else 0
        vis = max(0, h - head)
        total = len(self.items)
        top = self.ui.clamp(self.name, total, vis)
        if self.header:
            yield self.header(w)
        if not total:
            yield note(self.empty)
            return
        bar = scrollbar(total, vis, top)
        for i in range(min(vis, total - top)):
            t = self.items[top + i](w - 1)
            t.truncate(w - 1, pad=True)
            t.append(bar[i][0], style=bar[i][1])
            yield t


class Cores:
    """One cell per logical CPU: label, braille history (or meter), usage, core temperature."""

    def __init__(self, c, cols, mw, temps):
        self.c, self.cols, self.mw, self.temps = c, cols, mw, temps

    def __rich_console__(self, console, options):
        c, cols = self.c, self.cols
        count = len(c["per_core"])
        rows = -(-count // cols)
        lw = len(str(count - 1)) + 1
        hist = c.get("core_hist") or [[] for _ in range(count)]
        for r in range(rows):
            line = Text(no_wrap=True, overflow="crop")
            for ci in range(cols):
                i = ci * rows + r
                if i >= count:
                    continue
                v, t = c["per_core"][i], c["core_temps"][i]
                if ci:
                    line.append("  ")
                line.append(f"C{i:<{lw - 1}} ", style=P["mut"])
                line.append_text(spark(hist[i] or [v], self.mw))
                line.append(f"{v:4.0f}%", style=f"bold {lvl(v)}")
                if self.temps:
                    line.append(f" {t:3.0f}°" if t is not None else "     ", style=tcol(t))
            yield line


def core_cell(count, mw, temps):
    return 1 + len(str(count - 1)) + 1 + mw + 5 + (5 if temps else 0) + 2


# ---------------------------------------------------------------- UI state
class UI:
    def __init__(self, page="pulse"):
        self.page = "system" if page == "basic" else page
        self.sort, self.reverse, self.filter, self.typing = "cpu", False, "", False
        self.sel_pid = None
        self.scroll = {"proc": 0, "events": 0, "reqs": 0, "models": 0, "insights": 0, "timeline": 0, "services": 0,
                       "svc": 0, "incoming": 0, "connlog": 0, "programs": 0}
        self.show_all = False  # net page: also list localhost-only and UDP sockets
        self.vis = {}
        self.proc_y0, self.tabs, self.split_x, self.pulse_y, self.pulse_y2 = None, [], 0, 0, 0
        self.msg, self.msg_at = "", 0.0

    def clamp(self, name, total, vis):
        self.vis[name] = vis
        self.scroll[name] = max(0, min(self.scroll[name], total - vis))
        return self.scroll[name]

    def say(self, msg):
        self.msg, self.msg_at = msg, time.time()


SORTS = {"cpu": (lambda p: (p["cpu"], p["mem"]), True), "mem": (lambda p: (p.get("rss") or 0, p["mem"]), True),
         "pid": (lambda p: p["pid"], False), "name": (lambda p: p["name"].lower(), False),
         "threads": (lambda p: p["threads"] or 0, True), "user": (lambda p: p["user"].lower(), False)}


def proc_rows(D, ui):
    procs = (D.get("top") or {}).get("procs") or []
    f = ui.filter.lower()
    if f:
        procs = [p for p in procs if f in p["name"].lower() or f in p["cmd"].lower() or f in p["user"].lower()
                 or str(p["pid"]).startswith(f)]
    key, desc = SORTS[ui.sort]
    return sorted(procs, key=key, reverse=desc != ui.reverse)


# ---------------------------------------------------------------- chrome
def header(D, ui, w):
    live = time.time() - (D.get("ts") or 0) < 5
    left = Text(no_wrap=True, overflow="ellipsis")
    for ch, f in zip("░▒▓", (.35, .6, 1)):
        left.append(ch, style=dim(P["hi"], f) if f < 1 else P["hi"])
    left.append(" LMON ", style=f"bold {P['sel']} on {P['hi']}")
    for ch, f in zip("▓▒░", (1, .6, .35)):
        left.append(ch, style=dim(P["hi"], f) if f < 1 else P["hi"])
    if D.get("demo"):
        left.append(" DEMO DATA ", style=f"bold {P['sel']} on {P['warn']}")
    left.append(f"  {clean(D.get('user') or getpass.getuser())}@{clean(D.get('host'))}", style=f"bold {P['ok']}")
    left.append(":~$ ", style=P["mut"])
    left.append("█" if int(time.time()) % 2 else " ", style=P["ok"] if live else P["bad"])
    ip = next((i.get("ip") for i in (D.get("net") or {}).get("ifaces", []) if i.get("ip")), None)
    for label, val in (("ip", ip), ("up", updays(D.get("uptime_s"))), ("tty", D.get("sessions")), ("kernel", D.get("kernel"))):
        if val is not None:
            left.append(" │ ", style=P["empty"])
            left.append(f"{label} ", style=P["mut"])
            left.append(clean(val), style=P["fg"])
    clock = Text(time.strftime("%H:%M:%S", time.localtime(D.get("ts") or time.time())), style=f"bold {P['hi']}", no_wrap=True)
    tabs, spans = Text(no_wrap=True), []
    for key, num in PAGES:
        on = key == ui.page
        start = len(tabs)
        tabs.append(f" {num} ", style=f"bold {P['sel']} on {P['hi']}" if on else f"bold {P['hi']}")
        tabs.append(f"{key} ", style=f"bold {P['fg']}" if on else P["mut"])
        spans.append((start, len(tabs), key))
        tabs.append(" ")
    # laid out by hand (not a grid) so the tab positions used for mouse clicks are exact
    tabs.rstrip()
    x_tabs = w - len(tabs)
    ui.tabs = [(x_tabs + a, x_tabs + b, key) for a, b, key in spans]
    cx = (w - len(clock)) // 2
    if cx + len(clock) + 2 > x_tabs:
        cx = x_tabs - len(clock) - 2
    room = max(0, cx - 2)
    left.truncate(room, overflow="ellipsis")
    line = Text(no_wrap=True)
    line.append_text(left)
    line.append(" " * max(0, cx - len(line)))
    line.append_text(clock)
    line.append(" " * max(0, x_tabs - len(line)))
    line.append_text(tabs)
    line.truncate(w)
    return line


def footer(D, ui):
    t = Text(" ", no_wrap=True, overflow="ellipsis")
    if ui.typing:
        keys = [("type", "to filter"), ("enter", "done"), ("esc", "clear")]
    else:
        keys = [("^C", "exit"), ("1-5", "page"), ("wheel/↑↓", "scroll"), ("c", f"theme {P['theme']} → {next_theme()}")]
        keys += {"system": [("/", "filter"), ("s", "sort"), ("r", "reverse")], "ai": [("t", "tokens/s")],
                 "net": [("a", "hide local" if ui.show_all else "show local")]}.get(ui.page, [])
    for key, label in keys:
        t.append(key, style=f"bold {P['hi']}")
        t.append(f" {label}  ", style=P["mut"])
    right = Text(no_wrap=True, overflow="ellipsis", justify="right")
    if ui.msg and time.time() - ui.msg_at < 8:
        right.append(clean(ui.msg), style=P["warn"])
    elif D.get("errors"):
        right.append("issues: " + ", ".join(clean(x) for x in D["errors"]), style=P["warn"])
    g = Table.grid(expand=True, padding=(0, 1))
    g.add_column(no_wrap=True)
    g.add_column(ratio=1, no_wrap=True, overflow="ellipsis", justify="right")
    g.add_row(t, right)
    return g


# ---------------------------------------------------------------- pulse page (lmon's own home screen)
class Card:
    """lmon's panel style: a tinted header bar with an accent, no side borders."""

    def __init__(self, title, body, color, info=None):
        self.title, self.body, self.color, self.info = clean(title), body, color, info

    def __rich_console__(self, console, options):
        w, h = options.max_width, options.height
        bg = None if P["low"] else dim(self.color, .2)
        head = Text(no_wrap=True, style=f"on {bg}" if bg else "")
        head.append("▌", style=f"bold {self.color}")
        head.append(f" {self.title} ", style=f"bold {self.color}")
        info = self.info if isinstance(self.info, Text) else Text(clean(self.info or ""), style=P["mut"])
        if len(head) + len(info) + 2 <= w:
            head.append(" " * (w - len(head) - len(info) - 1))
            head.append_text(info)
        head.truncate(w, pad=True)
        yield head
        if h is not None and h <= 1:
            return
        inner = options.update(width=max(1, w - 2), height=None if h is None else h - 1)
        for line in console.render_lines(self.body, inner, pad=True):
            yield Segment(" ")
            yield from line
            yield Segment(" ")
            yield Segment.line()


_DIGITS = {"0": ("███", "█ █", "█ █", "█ █", "███"), "1": (" █ ", "██ ", " █ ", " █ ", "███"),
           "2": ("███", "  █", "███", "█  ", "███"), "3": ("███", "  █", "███", "  █", "███"),
           "4": ("█ █", "█ █", "███", "  █", "  █"), "5": ("███", "█  ", "███", "  █", "███"),
           "6": ("███", "█  ", "███", "█ █", "███"), "7": ("███", "  █", "  █", "  █", "  █"),
           "8": ("███", "█ █", "███", "█ █", "███"), "9": ("███", "█ █", "███", "  █", "███")}


def big_number(num: int, style) -> list[Text]:
    """3-row digits drawn with half blocks (5 pixel rows -> 3 text rows)."""
    px = [" ".join(_DIGITS[d][r] for d in str(num)) for r in range(5)] + [" " * (4 * len(str(num)) - 1)]
    rows = []
    for r in range(0, 6, 2):
        top, bot = px[r], px[r + 1]
        rows.append(Text("".join("█" if a != " " and b != " " else "▀" if a != " " else "▄" if b != " " else " "
                                 for a, b in zip(top, bot)), style=style, no_wrap=True))
    return rows


def vital(label, value, hist, stops, col):
    head = Text(no_wrap=True, overflow="ellipsis")
    head.append(f"{label} ", style=P["mut"])
    head.append(value, style=f"bold {col}")
    return stack((head, 1), (Graph(hist, stops, guides=False), None))


def vitals(D, width):
    c, m, g = D.get("cpu") or {}, D.get("mem") or {}, D.get("gpu") or {}
    nw = D.get("net") or {}
    tk = ((D.get("ai") or {}).get("requests") or {}).get("tokens") or {}
    rx, tx = nw.get("rx_hist") or [], nw.get("tx_hist") or []
    traffic = [a + b for a, b in zip(rx, tx)]
    scale = nice_scale(max([0.0, *traffic]))
    per_min = tk.get("per_min") or []
    top = max(per_min or [0]) or 1
    cells = [vital("CPU", f"{c.get('pct', 0):.0f}%", c.get("hist"), P["grad"], P["cpu"]),
             vital("MEM", f"{m.get('pct', 0):.0f}%", m.get("hist"), P["mgrad"], P["mem"])]
    if g:
        cells += [vital("GPU", f"{n(g.get('util'))}%", g.get("util_hist"), P["ggrad"], P["gpu"]),
                  vital("VRAM", f"{n(g.get('vram_pct'))}%", g.get("vram_hist"), P["vgrad"], P["ai"])]
    cells.append(vital("NET", rate(traffic[-1] if traffic else None), [v / scale * 100 for v in traffic], P["rx"], P["net"]))
    if tk:
        cells.append(vital("TOK/min", k(per_min[-1] if per_min else None), [v / top * 100 for v in per_min], (dim(P["tok"], .5), P["tok"]),
                           P["tok"]))
    cells = cells[:max(1, (width + 2) // 14)]  # drop what does not fit (>= 12 columns per vital)
    parts = []
    for i, cell in enumerate(cells):
        if i:
            parts.append((Text(" "), 2))
        parts.append((cell, ("r", 1)))
    return row(*parts)


def pulse_page(D, ui, w, h):
    findings = D.get("insights")
    if findings is None:
        findings = analyze(D)
    sc = score(findings)
    vd = verdict(findings)
    vcol = {"NOMINAL": P["ok"], "WATCH": P["warn"], "DEGRADED": P["warn"]}.get(vd, P["bad"])
    counts = {lv: sum(f["level"] == lv for f in findings) for lv in ("bad", "warn", "info")}

    # banner: big health score, verdict, live vitals
    digits = big_number(sc, f"bold {vcol}")
    left = Group(*digits, Text("health", style=P["mut"], justify="center"))
    v1 = Text(no_wrap=True, overflow="ellipsis")
    v1.append(f"● {vd}", style=f"bold {vcol}")
    v2 = Text(no_wrap=True, overflow="ellipsis")
    v2.append(f"{counts['bad']} critical", style=f"bold {P['bad']}" if counts["bad"] else P["mut"])
    v2.append(" · ", style=P["mut"])
    v2.append(f"{counts['warn']} warning", style=f"bold {P['warn']}" if counts["warn"] else P["mut"])
    v2.append(" · ", style=P["mut"])
    v2.append(f"{counts['info']} info", style=P["fg"] if counts["info"] else P["mut"])
    v3 = Text(no_wrap=True, overflow="ellipsis")
    v3.append(f"{clean(D.get('host'))}", style=f"bold {P['fg']}")
    v3.append(f" · up {updays(D.get('uptime_s'))}", style=P["mut"])
    dw = 4 * len(str(sc)) + 1
    banner_body = row((left, dw), (Text(" "), 2), (Group(v1, v2, v3), 34), (Text(" "), 2), (vitals(D, w - dw - 40), ("r", 1)))
    banner = Card("PULSE", banner_body, vcol, Text("system health from live data", style=P["mut"]))

    # findings
    items = []
    for f in findings:
        col = {"bad": P["bad"], "warn": P["warn"]}.get(f["level"], P["hi"])

        def title(w, f=f, col=col):
            t = Text(no_wrap=True)
            t.append("■ ", style=col)
            t.append(clean(f["title"]), style=f"bold {P['fg']}" if f["level"] != "info" else P["fg"])
            return t

        def detail(w, f=f):
            return Text("  " + clean(f["detail"]), style=P["mut"], no_wrap=True) if f["detail"] else Text("")
        items += [title, detail] if f["detail"] else [title]
    if not items:
        items = [lambda w: Text("● all clear - nothing needs attention", style=f"bold {P['ok']}")]
    insights_card = Card("INSIGHTS", ScrollList("insights", ui, items), P["hi"], Text(f"{len(findings)} findings", style=P["mut"]))

    # timeline
    events = list(reversed(D.get("timeline") or []))
    today = time.strftime("%m-%d")
    tl = []
    for e in events:
        def ev(w, e=e):
            t = Text(no_wrap=True)
            day = time.strftime("%m-%d", time.localtime(e["ts"]))
            t.append(time.strftime("%H:%M:%S" if day == today else "%m-%d %H:%M", time.localtime(e["ts"])) + "  ", style=P["mut"])
            icon, col = {"bad": ("▲", P["bad"]), "warn": ("▲", P["warn"]), "ok": ("▼", P["ok"]), "ai": ("◆", P["tok"]),
                         "net": ("→", P["net"])}.get(e["level"], ("•", P["hi"]))
            t.append(icon + " ", style=col)
            t.append(clean(e["text"]), style=P["fg"] if e["level"] != "info" else P["mut"])
            return t
        tl.append(ev)
    timeline_card = Card("TIMELINE", ScrollList("timeline", ui, tl, empty="nothing recorded yet - events appear here as they happen"),
                         P["tok"], Text("what happened · kept across restarts", style=P["mut"]))

    # sizes follow content: findings and network take what they need, the two lists share the rest
    rest = h - 5
    hi = max(4, min(len(items) + 1, rest - 8))
    net_card, net_lines = net_summary_card(D, max(1, rest - 10))
    hn = max(4, min(net_lines + 1, rest - 6))
    ui.split_x = w * 5 // 9
    ui.pulse_y, ui.pulse_y2 = 1 + 5 + hi, 1 + 5 + hn
    gap = (Text(" "), 1)
    left = stack((insights_card, hi), (programs_card(D, ui), None))
    right = stack((net_card, hn), (timeline_card, None))
    return stack((banner, 5), (row((left, ("r", 5)), gap, (right, ("r", 4))), None))


def programs_card(D, ui):
    """Processes grouped by program: '42 x postgres' is one row with their summed CPU and memory."""
    procs = (D.get("top") or {}).get("procs") or []
    ncpu = len((D.get("cpu") or {}).get("per_core") or []) or 1
    gpu = {}
    for app in (D.get("gpu") or {}).get("apps") or []:
        gpu[app["name"]] = gpu.get(app["name"], 0) + app["vram_gb"]
    groups = {}
    for p in procs:
        g = groups.setdefault(p["name"] or "?", {"name": p["name"] or "?", "n": 0, "cpu": 0.0, "rss": 0, "threads": 0, "users": set()})
        g["n"] += 1
        g["cpu"] += p["cpu"]
        g["rss"] += p.get("rss") or 0
        g["threads"] += p["threads"] or 0
        g["users"].add(p["user"])
    rows = sorted(groups.values(), key=lambda g: (g["cpu"], g["rss"]), reverse=True)

    def head(w):
        return Text(f"{fixed('PROGRAM', 18)} {fixed('×', 4, True)} {fixed('CPU (whole machine)', 22)} {fixed('MEM', 7, True)} "
                    f"{fixed('THR', 5, True)}" + (f" {fixed('VRAM', 6, True)}" if gpu else "") + "  USER", style=f"bold {P['mut']}", no_wrap=True)
    items = []
    for g in rows:
        def item(w, g=g):
            t = Text(no_wrap=True)
            busy = g["cpu"] >= 1
            t.append(fixed(g["name"], 18) + " ", style=f"bold {P['fg']}" if busy else P["fg"])
            t.append(fixed(f"{g['n']}" if g["n"] > 1 else "", 4, True) + " ", style=P["mut"])
            share = g["cpu"] / ncpu
            t.append_text(meter(share, 10))
            t.append(fixed(f"{g['cpu']:.0f}%" if g["cpu"] >= 1 else f"{g['cpu']:.1f}%", 6, True), style=f"bold {lvl(min(100, share * 4))}" if busy else P["mut"])
            t.append(fixed(f"{share:.1f}%", 6, True) + " ", style=P["mut"])
            t.append(fixed(size_b(g["rss"]), 7, True) + " ", style=grad(P["mgrad"], g["rss"] / (8 * 1024 ** 3)))
            t.append(fixed(n(g["threads"]), 5, True), style=P["mut"])
            if gpu:
                t.append(" " + fixed(f"{gpu[g['name']]:.1f}G" if g["name"] in gpu else "", 6, True), style=P["gpu"])
            t.append("  " + ", ".join(sorted(clean(u) for u in g["users"]))[:24], style=P["mut"])
            return t
        items.append(item)
    info = Text(f"{len(rows)} programs · {len(procs)} processes · CPU% = sum, 100% = one core", style=P["mut"])
    return Card("TOP PROGRAMS", ScrollList("programs", ui, items, header=head, empty="process list not collected"), P["proc"], info)


# ---------------------------------------------------------------- net page
RISKY = ("database", "cache / key-value", "vector database", "admin web UI")
FROM = {"lan": "LAN", "tailscale": "Tailscale", "vpn": "VPN", "docker": "container", "private": "private net", "internet": "INTERNET"}


def svc_name(r):
    if not r["name"]:
        return "unknown (root-owned)"
    return clean(r["name"]) + ("?" if not r["sure"] else "")


def svc_risk(r, fw_on):
    """Colour for a service row: red = data/admin service open to every network with nothing in front."""
    if r["open_to"] == "this host":
        return P["ok"]
    if r["open_to"] == "everyone" and r["what"] in RISKY and not (fw_on and r["firewall"] == "blocked"):
        return P["bad"]
    return P["warn"] if r["open_to"] == "everyone" else P["fg"]


def fw_text(fw):
    if not fw:
        return "firewall status unknown", P["mut"]
    if fw.get("on"):
        which = "UFW" if fw.get("ufw_enabled") else "firewalld" if fw.get("firewalld") else "nftables"
        extra = f" · default incoming: {fw['default_in']}" if fw.get("default_in") else " · rules need root to read (sudo lmon)"
        return f"FIREWALL ON  ({which}{extra})", P["ok"]
    if fw.get("ufw_installed"):
        return "FIREWALL OFF  (UFW is installed but disabled: ENABLED=no in /etc/ufw/ufw.conf)", P["bad"]
    return "FIREWALL OFF  (no ufw / firewalld / nftables active)", P["bad"]


def net_summary_card(D, max_rows=12):
    ns = D.get("netsec") or {}
    rows = [r for r in ns.get("services") or [] if r["proto"] == "tcp"]
    fw = ns.get("firewall") or {}
    text, col = fw_text(fw)
    everyone = [r for r in rows if r["open_to"] == "everyone"]
    risky = [r for r in everyone if r["what"] in RISKY]
    lines = [Text("■ " + text.split("  ")[0], style=f"bold {col}", no_wrap=True)]
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f"{len(everyone)}", style=f"bold {P['warn'] if everyone else P['ok']}")
    t.append(" services open to every network · ", style=P["mut"])
    t.append(f"{len(risky)}", style=f"bold {P['bad'] if risky else P['ok']}")
    t.append(" are databases / admin UIs", style=P["mut"])
    lines.append(t)
    by = ns.get("by_from") or {}
    c = Text(no_wrap=True, overflow="ellipsis")
    c.append("connected now: ", style=P["mut"])
    c.append(" · ".join(f"{v} {FROM.get(k, k)}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])) or "nobody", style=P["fg"])
    lines.append(c)
    lines.append(Text(""))
    listed = (risky + [r for r in everyone if r not in risky])
    for r in listed[:max(1, min(12, max_rows - 6))]:
        x = Text(no_wrap=True, overflow="ellipsis")
        x.append("● ", style=svc_risk(r, fw.get("on")))
        x.append(fixed(svc_name(r), 22) + " ", style=f"bold {P['fg']}")
        x.append(fixed(r["what"] or "—", 18) + " ", style=P["mut"])
        x.append(fixed(f":{r['port']}", 6, True), style=P["fg"])
        if r["nconn"]:
            x.append(f"  {r['nconn']} connected", style=P["hi"])
        lines.append(x)
    more = len(listed) - (len(lines) - 4)
    lines.append(note((f"+{more} more · " if more > 0 else "") + "press 5 for the full network view", P["hi"]))
    return Card("NETWORK ACCESS", Group(*lines), col, Text(f"{len(rows)} tcp services", style=P["mut"])), len(lines)


def net_page(D, ui, w, h):
    ns = D.get("netsec") or {}
    fw = ns.get("firewall") or {}
    text, col = fw_text(fw)
    svcs = ns.get("services") or []
    rows = [r for r in svcs if ui.show_all or (r["proto"] == "tcp" and r["open_to"] != "this host")]
    hidden = len(svcs) - len(rows)
    tcp = [r for r in svcs if r["proto"] == "tcp"]
    everyone = [r for r in tcp if r["open_to"] == "everyone"]
    limited = [r for r in tcp if r["open_to"] not in ("everyone", "this host")]
    local = [r for r in tcp if r["open_to"] == "this host"]

    # banner: firewall, the networks this host is on, exposure in numbers
    b1 = Text(no_wrap=True, overflow="ellipsis")
    b1.append("■ " + text.split("  ")[0], style=f"bold {col}")
    b1.append("   " + text.split("  ", 1)[1] if "  " in text else "", style=P["mut"])
    if fw.get("root") and fw.get("input_policy"):
        b1.append(f"   · iptables INPUT: policy {fw['input_policy']}, {fw.get('input_rules', 0)} rules", style=P["fg"])
    elif not fw.get("root"):
        b1.append("   · sudo lmon also reads firewall rules and names every port", style=P["hi"])
    b2 = Text(no_wrap=True, overflow="ellipsis")
    b2.append("this server is on  ", style=P["mut"])
    for i, (cls, name, addr) in enumerate(ns.get("attached") or []):
        b2.append(("  │  " if i else ""), style=P["empty"])
        b2.append(f"{FROM.get(cls, cls)} ", style=f"bold {P['fg']}")
        b2.append(f"{name} {addr}", style=P["mut"])
    b3 = Text(no_wrap=True, overflow="ellipsis")
    b3.append(f"{len(everyone)} open to EVERY network", style=f"bold {P['warn'] if everyone else P['ok']}")
    b3.append(f"  ·  {len(limited)} limited to one network  ·  {len(local)} this machine only", style=P["mut"])
    if ns.get("docker") and any(r["via"] == "docker" and r["open_to"] == "everyone" for r in tcp):
        n_d = sum(r["via"] == "docker" and r["open_to"] == "everyone" for r in tcp)
        b3.append(f"  ·  {n_d} published by Docker", style=P["mut"])
        if fw.get("ufw_enabled"):
            b3.append(" (Docker bypasses UFW)", style=P["warn"])
    by = ns.get("by_from") or {}
    b4 = Text(no_wrap=True, overflow="ellipsis")
    b4.append("connected right now  ", style=P["mut"])
    if by:
        for i, (k, v) in enumerate(sorted(by.items(), key=lambda kv: -kv[1])):
            b4.append(("  ·  " if i else ""), style=P["mut"])
            b4.append(f"{v} from {FROM.get(k, k)}", style=f"bold {P['bad'] if k == 'internet' else P['fg']}")
    else:
        b4.append("nobody", style=P["fg"])
    banner = Card("FIREWALL & EXPOSURE", Group(b1, b2, b3, b4), col, Text("who can reach this server", style=P["mut"]))

    # services table: risky first, merged per service, unnamed root-owned ports folded into one line
    show_fw = bool(fw.get("on"))  # with no firewall the banner already says so; a column of "NONE" adds nothing
    rank = {P["bad"]: 0, P["warn"]: 1, P["fg"]: 2, P["ok"]: 3}
    merged, unknown = {}, []
    for r in rows:
        if not r["name"] and r["via"] == "root":
            unknown.append(r)
            continue
        key = (r["name"], r["via"], r["open_to"], r["proto"])
        m = merged.setdefault(key, {**r, "ports": [], "conns": {}, "nconn": 0})
        m["ports"].append(r["port"])
        for k, v in r["conns"].items():
            m["conns"][k] = m["conns"].get(k, 0) + v
        m["nconn"] += r["nconn"]
    table = sorted(merged.values(), key=lambda r: (rank.get(svc_risk(r, fw.get("on")), 2), -r["nconn"], r["ports"][0]))

    def head(w):
        what = f"{fixed('WHAT', 17)} " if w >= 92 else ""  # narrow: drop WHAT so CONNECTED stays visible
        return Text(f"  {fixed('SERVICE', 24)} {what}{fixed('PORT', 11, True)}  {fixed('OPEN TO', 14)} "
                    + (f"{fixed('FIREWALL', 9)} " if show_fw else "") + "CONNECTED", style=f"bold {P['mut']}", no_wrap=True)
    items = []
    for r in table:
        def item(w, r=r):
            t = Text(no_wrap=True)
            t.append("● ", style=svc_risk(r, fw.get("on")))
            name = svc_name(r) + (f"  {r['via']}" if r["via"] == "docker" else "")
            t.append(fixed(name, 24) + " ", style=f"bold {P['fg']}")
            if w >= 92:
                t.append(fixed(r["what"] or "—", 17) + " ", style=P["fg"] if r["what"] else P["mut"])
            ports = ",".join(map(str, r["ports"])) + ("/udp" if r["proto"] == "udp" else "")
            t.append(fixed(ports, 11, True) + "  ", style=f"bold {P['fg']}")
            ot = "EVERYONE" if r["open_to"] == "everyone" else r["open_to"]
            t.append(fixed(ot, 14) + " ", style=f"bold {P['warn']}" if r["open_to"] == "everyone" else
                     (P["ok"] if r["open_to"] == "this host" else P["fg"]))
            if show_fw:
                guard = {"none": "none", "allowed": "allowed", "blocked": "blocked", "bypassed": "BYPASSED", "unknown": "?"}[r["firewall"]]
                guard = "n/a" if r["open_to"] == "this host" else guard
                t.append(fixed(guard, 9) + " ", style=P["bad"] if guard == "BYPASSED" else P["ok"] if guard == "blocked" else P["mut"])
            if r["nconn"]:
                t.append(", ".join(f"{v} {FROM.get(k, k)}" for k, v in r["conns"].items()), style=f"bold {P['hi']}")
            else:
                t.append("—", style=P["mut"])
            return t
        items.append(item)
    if unknown:
        def other(w):
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(f"  {len(unknown)} more ports owned by root (name needs sudo): ", style=P["mut"])
            t.append(", ".join(f"{r['port']}" + (" (all)" if r["open_to"] == "everyone" else "") for r in unknown), style=P["fg"])
            return t
        items.append(other)
    svc_info = Text(f"{len(table)} services" + (f" · {hidden} local/udp hidden (a)" if hidden else ""), style=P["mut"])
    svc_card = Card("SERVICES", ScrollList("svc", ui, items, header=head, empty="no listening sockets"), P["warn"] if everyone else P["ok"],
                    svc_info)

    # who is connected, one line per remote machine
    known = {r["port"]: svc_name(r) for r in svcs if r["proto"] == "tcp"}

    def names_port(port):
        return f"{known[port]}:{port}" if port in known else f":{port}"
    peers = {}
    for c in ns.get("incoming") or []:
        p = peers.setdefault(c["ip"], {"from": c["from"], "you": c["you"], "ports": {}, "since": c["since"]})
        p["ports"][c["port"]] = p["ports"].get(c["port"], 0) + 1
        p["since"] = min(p["since"], c["since"])
    inc = []
    for ip, p in sorted(peers.items(), key=lambda kv: -sum(kv[1]["ports"].values())):
        def peer(w, ip=ip, p=p):
            t = Text(no_wrap=True)
            fcol = P["bad"] if p["from"] == "internet" else P["hi"]
            t.append(fixed(ip, 16) + " ", style=f"bold {P['fg']}")
            t.append(fixed(FROM.get(p["from"], p["from"]), 10) + " ", style=fcol)
            t.append("you  " if p["you"] else "     ", style=f"bold {P['ok']}")
            t.append("→ " + ", ".join(names_port(port) + (f" ×{cnt}" if cnt > 1 else "")
                                       for port, cnt in sorted(p["ports"].items(), key=lambda kv: -kv[1])), style=P["fg"])
            return t
        inc.append(peer)
    inc_card = Card("CONNECTED NOW", ScrollList("incoming", ui, inc, empty="no one is connected from outside this machine"),
                    P["hi"], Text(f"{len(peers)} machines", style=P["mut"]))

    log = []
    for e in ns.get("log") or []:
        def ev(w, e=e):
            t = Text(no_wrap=True)
            t.append(time.strftime("%H:%M:%S", time.localtime(e["ts"])) + "  ", style=P["mut"])
            t.append(fixed(e["ip"], 16) + " ", style=f"bold {P['fg']}")
            t.append(fixed(FROM.get(e["from"], e["from"]), 10) + " ", style=P["bad"] if e["from"] == "internet" else P["hi"])
            t.append(f"→ {names_port(e['port'])}", style=P["fg"])
            return t
        log.append(ev)
    log_card = Card("NEW CONNECTIONS", ScrollList("connlog", ui, log, empty="watching... new connections appear here"), P["tok"],
                    Text("since lmon started", style=P["mut"]))

    ui.split_x = w * 3 // 5
    ui.pulse_y = 1 + 6 + (h - 6) // 2
    gap = (Text(" "), 1)
    right = stack((inc_card, None), (Text(" "), 1), (log_card, None))
    foot = note("Connections shorter than the 3 s refresh (6 s for containers) are missed. '?' = likely owner (usual port of a running program), "
                "not verified" + ("" if fw.get("root") else " - run sudo lmon to name every port."))
    return stack((banner, 6), (row((svc_card, ("r", 3)), gap, (right, ("r", 2))), None), (foot, 1))


# ---------------------------------------------------------------- system page
def cpu_box(D, w, h):
    c = D.get("cpu")
    if not c:
        return Box("CPU", note("no data"), P["cpu"])
    count = len(c["per_core"])
    fr = [f for f in c["freq_mhz"] if f]
    info = Text(no_wrap=True)
    info.append(clean(D.get("cpu_model") or "cpu"), style=P["fg"])
    if fr:
        info.append(f" // {sum(fr) / len(fr) / 1000:.1f} GHz", style=P["mut"])
    if c.get("temp") is not None:
        info.append(f" // {c['temp']:.0f}°C", style=f"bold {tcol(c['temp'])}")

    iw, ih = w - 2, h - 2
    grid_h = max(1, ih - 2)
    cols = max(1, -(-count // grid_h))
    temps = any(t is not None for t in c["core_temps"])
    mw = 10
    while mw > 3 and cols * core_cell(count, mw, temps) > iw - 24:  # leave the graph >= 24 columns
        mw -= 1
    if cols * core_cell(count, mw, temps) > iw - 24 and temps:
        temps, mw = False, 10
        while mw > 3 and cols * core_cell(count, mw, temps) > iw - 24:
            mw -= 1
    while mw < 14 and cols * core_cell(count, mw + 1, temps) <= iw * 0.5:
        mw += 1
    grid_w = min(iw, cols * core_cell(count, mw, temps))
    load = Text(no_wrap=True, overflow="crop")
    load.append("load avg ", style=P["mut"])
    for x in c["load"]:
        load.append(f"{x:6.2f}", style=lvl(x / count * 100))
    load.append("   pwr ", style=P["mut"])
    load.append("—" if c["power_w"] is None else f"{c['power_w']:.0f}W", style=P["fg"])
    right = stack((MeterLine("CPU", c["pct"], f"{c['pct']:3.0f}%", lw=3, label_style=f"bold {P['cpu']}"), 1),
                  (Cores(c, cols, mw, temps), None), (load, 1))

    l1 = Text(no_wrap=True, overflow="crop")
    for i, (label, v) in enumerate((("user", c["user"]), ("sys", c["system"]), ("wait", c["iowait"]), ("irq", c["irq"]))):
        l1.append(("" if not i else "  │ ") + f"{label} ", style=P["mut"])
        l1.append(f"{v:.1f}%", style=lvl(v * 2) if label != "wait" else (P["warn"] if v >= 10 else P["fg"]))
    l2 = Text(no_wrap=True, overflow="crop")
    hot = max(c["per_core"])
    l2.append("busiest ", style=P["mut"])
    l2.append(f"{hot:.0f}%", style=lvl(hot))
    board = (D.get("temps") or {}).get("board")
    if board is not None:
        l2.append("  │ board ", style=P["mut"])
        l2.append(deg(board), style=tcol(board))
    if c.get("freq_max_mhz"):
        l2.append("  │ boost ", style=P["mut"])
        l2.append(f"{c['freq_max_mhz'] / 1000:.1f} GHz", style=P["fg"])
    graph_w = iw - grid_w - 1
    body = row((stack((Graph(c["hist"], P["grad"]), None), (Group(l1, l2), 2)), ("r", 1)), (Text(" "), 1), (right, grid_w)) \
        if graph_w >= 16 else right
    return Box(f"CPU // {count} THREADS", body, P["cpu"], info)


def mem_box(D):
    m, d = D.get("mem"), D.get("disk")
    if not m:
        return Box("MEM", note("no data"), P["mem"])
    tot = m["total_gb"] or 1
    cached = m.get("cached_gb") or 0
    lines = [kv([("total", f"{m['total_gb']:.1f} GiB")]),
             MeterLine("used", m["pct"], f"{gb(m['used_gb']):>6} {m['pct']:3.0f}%", stops=P["mgrad"]),
             MeterLine("avail", m["avail_gb"] / tot * 100, f"{gb(m['avail_gb']):>6} {m['avail_gb'] / tot * 100:3.0f}%",
                       stops=P["g_avail"]),
             MeterLine("cached", cached / tot * 100, f"{gb(cached):>6} {cached / tot * 100:3.0f}%", stops=P["g_cached"]),
             MeterLine("swap", m.get("swap_pct"), f"{gb(m['swap_used_gb']):>6} " + ("  — " if m.get("swap_pct") is None else f"{m['swap_pct']:3.0f}%"))]
    if d:
        hd = Text(no_wrap=True, overflow="ellipsis")
        hd.append(f"▼ disk {clean(d.get('name') or '')}", style=f"bold {P['disk']}")
        hd.append("  r ", style=P["mut"])
        hd.append(n(d["read_mbs"], 1), style=P["fg"])
        hd.append("  w ", style=P["mut"])
        hd.append(f"{n(d['write_mbs'], 1)} MB/s", style=P["fg"])
        hd.append(f"  {k(d['iops'])} iops", style=P["mut"])
        if d.get("temp") is not None:
            hd.append(f"  {deg(d['temp'])}", style=tcol(d["temp"]))
        lines.append(hd)
        lines += [MeterLine(mt["path"], mt["pct"], f"{mt['used_gb']:.0f}/{mt['total_gb']:.0f}G {mt['pct']:3.0f}%", lw=9)
                  for mt in d["mounts"]]
    return Box("MEM // DISK", Group(*lines), P["mem"])


def nice_scale(top):
    scale = 10 ** math.ceil(math.log10(top)) if top > 0 else 1.0
    for s in (scale / 5, scale / 2):  # 1-2-5 steps so small traffic still shows
        if top <= s:
            return max(s, 0.1)
    return max(scale, 0.1)


def net_box(D, w):
    nw = D.get("net")
    if not nw:
        return Box("NET", note("no data"), P["net"])
    rx, tx = nw.get("rx_hist") or [], nw.get("tx_hist") or []
    scale = nice_scale(max([0.0, *rx, *tx]))
    ups = [i for i in nw["ifaces"] if i["up"]]
    lines = []
    for arrow, label, key, hist, total, col in (("▼", "down", "rx_mbps", rx, nw.get("rx_total"), P["rx"][-1]),
                                                ("▲", "up", "tx_mbps", tx, nw.get("tx_total"), P["tx"][-1])):
        cur = sum(i[key] or 0 for i in ups) if ups else None
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(f"{arrow} {label:<5}", style=f"bold {col}")
        t.append(f"{rate(cur):>11}", style=f"bold {P['fg']}")
        lines.append(t)
        lines.append(kv([("  peak", rate(max(hist) if hist else None)), ("  total", size_b(total))]))
    for i in nw["ifaces"][:2]:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("● " if i["up"] else "○ ", style=P["ok"] if i["up"] else P["bad"])
        t.append(f"{clean(i['name'])} ", style=P["fg"])
        t.append(clean(i.get("ip") or ""), style=P["mut"])
        lines.append(t)
    graph = stack((Graph([v / scale * 100 for v in rx], P["rx"]), None), (Graph([v / scale * 100 for v in tx], P["tx"], down=True), None))
    info = Text(no_wrap=True)
    info.append(f"{rate(scale)}", style=P["mut"])
    info.append(f" // {k(nw['rx_pps'])}▼ {k(nw['tx_pps'])}▲ pkt/s", style=P["mut"])
    if w >= 56:
        body = row((graph, ("r", 1)), (Text(" "), 1), (Group(*lines), 24))
    else:
        body = stack((graph, None), (Group(lines[0], lines[2]), 2))
    return Box("NET", body, P["net"], info)


def gpu_box(D):
    g = D.get("gpu")
    if not g:
        return Box("GPU", note("No NVIDIA GPU (nvidia-smi not found)"), P["gpu"])
    vp = None if not g.get("vram_total_gb") or g.get("vram_used_gb") is None else g["vram_used_gb"] / g["vram_total_gb"] * 100
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("temp ", style=P["mut"])
    t.append(deg(g["temp"]), style=tcol(g["temp"]))
    t.append("  pwr ", style=P["mut"])
    t.append("—" if g["power_w"] is None else f"{g['power_w']:.0f}W", style=P["fg"])
    if g.get("pstate"):
        t.append(f"  {clean(g['pstate'])}", style=P["mut"])
    return Box("GPU", Group(MeterLine("util", g["util"], f"{n(g['util']):>3}%", lw=5, stops=P["ggrad"]),
                            MeterLine("vram", vp, f"{n(g['vram_used_gb'], 1)}/{n(g['vram_total_gb'])}G", lw=5, stops=P["vgrad"]), t),
               P["gpu"], Text(clean(g["name"]), style=P["fg"]))


def ollama_box(D):
    o = D.get("ollama") or {}
    a = D.get("ai") or {}
    rows = []
    if not o.get("running"):
        rows.append(note("not reachable · " + clean(o.get("url")), P["bad"]))
    elif not o.get("models"):
        rows.append(note("idle · no model loaded"))
    for m in (o.get("models") or [])[:2]:
        rows.append(kv([(m["name"], f"{m['size_gb']:.1f}G", f"bold {P['fg']}")]))
    r = a.get("requests")
    if r:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("1h ", style=P["mut"])
        t.append(f"{r['inference']} req", style=P["fg"])
        t.append(" · ", style=P["mut"])
        t.append(f"{r['errors']} err", style=P["bad"] if r["errors"] else P["fg"])
        rows.append(t)
    rows.append(note("press 3 for AI workloads", P["hi"]))
    state = Text(("● " + (clean(o.get("version")) + " " if o.get("version") else "") + "running") if o.get("running") else "● down",
                 style=P["ok"] if o.get("running") else P["bad"])
    return Box("OLLAMA", Group(*rows), P["ai"], state)


def proc_lines(rows, ui):
    def fn(w, h):
        vis = max(0, h - 1)
        top = ui.clamp("proc", len(rows), vis)
        wide = w >= 110
        cols = [("pid", "PID", 7, True), ("name", "PROGRAM", 15, False), ("cmd", "COMMAND", 0, False)]
        if wide:
            cols += [("threads", "THR", 4, True), ("user", "USER", 9, False)]
        cols += [("mem", "MEM", 6, True)] + ([("memp", "MEM%", 5, True)] if wide else []) + [("bar", "", 5, False), ("cpu", "CPU%", 6, True)]
        cmd_w = max(4, w - (1 + sum(c[2] for c in cols) + len(cols) + 1))  # marker, gaps, scrollbar
        arrow = "▲" if ui.reverse else "▼"
        head = Text(" ", no_wrap=True)
        for key, label, cw, right in cols:
            on = key == ui.sort
            head.append(fixed(label + arrow if on else label, cmd_w if key == "cmd" else cw, right) + " ",
                        style=f"bold {P['proc']}" if on else f"bold {P['mut']}")
        yield head
        bar = scrollbar(len(rows), vis, top)
        for i in range(vis):
            if top + i >= len(rows):
                yield Text("")
                continue
            p = rows[top + i]
            sel = p["pid"] == ui.sel_pid
            active = p["cpu"] >= 0.5
            t = Text(no_wrap=True, style=f"on {P['selbg']}" if sel else "")
            t.append("►" if sel else " ", style=P["hi"])
            cells = {"pid": (fixed(p["pid"], 7, True), P["mut"]),
                     "name": (fixed(p["name"], 15), f"bold {P['fg']}" if active or sel else P["fg"]),
                     "cmd": (fixed(p["cmd"], cmd_w), P["mut"]),
                     "threads": (fixed(n(p["threads"]), 4, True), P["mut"]),
                     "user": (fixed(p["user"], 9), P["mut"]),
                     "mem": (fixed(size_b(p.get("rss")), 6, True), grad(P["mgrad"], p["mem"] / 20)),
                     "memp": (fixed(f"{p['mem']:.1f}", 5, True), P["mut"]),
                     "cpu": (fixed(f"{p['cpu']:.1f}", 6, True), f"bold {lvl(clamp(p['cpu']))}" if active else P["mut"])}
            for key, *_ in cols:
                if key == "bar":
                    t.append_text(meter(p["cpu"], 5))
                else:
                    t.append(*cells[key])
                t.append(" ")
            t.append(bar[i][0], style=bar[i][1])
            yield t
    return Lines(fn)


def proc_box(D, ui):
    top = D.get("top")
    if not top:
        return Box("PROC", note("process list not collected"), P["proc"])
    rows = proc_rows(D, ui)
    info = Text(no_wrap=True)
    if ui.filter or ui.typing:
        info.append("filter ", style=P["mut"])
        info.append(clean(ui.filter) + ("█" if ui.typing else ""), style=f"bold {P['warn']}")
    else:
        info.append("/ filter", style=P["mut"])
    sel = next((i for i, p in enumerate(rows) if p["pid"] == ui.sel_pid), None)
    foot = Text(f"{sel + 1 if sel is not None else 0}/{len(rows)}" + (f" of {top['count']}" if len(rows) != top["count"] else ""),
                style=P["mut"])
    return Box(f"PROC // sort {ui.sort}", proc_lines(rows, ui), P["proc"], info, foot)


def system_page(D, ui, w, h):
    mounts = len((D.get("disk") or {}).get("mounts") or [])
    hm = max(9, min(12, 2 + 5 + (1 + mounts if D.get("disk") else 0)))
    show_proc = D.get("top") is not None and h - hm - 10 >= 6
    hc = max(8, min(20, round(h * 0.38))) if show_proc else h - hm
    hp = h - hc - hm
    if show_proc and hp < 6:
        hc -= 6 - hp
        hp = 6
    right = stack((gpu_box(D), 5), (ollama_box(D), None))
    mid = row((mem_box(D), ("r", 4)), (net_box(D, w * 4 // 11), ("r", 4)), (right, ("r", 3)))
    parts = [(cpu_box(D, w, hc), hc), (mid, hm)]
    ui.proc_y0 = None
    if show_proc:
        parts.append((proc_box(D, ui), hp))
        ui.proc_y0 = 1 + hc + hm + 2  # header row + boxes above + top border + column header
    return stack(*parts)


# ---------------------------------------------------------------- AI page
def pairs(items) -> Text:
    """'label value  label value' on one line."""
    t = Text(no_wrap=True, overflow="ellipsis")
    for i, (label, val, col) in enumerate(items):
        t.append(("  " if i else "") + (f"{label} " if label else ""), style=P["mut"])
        t.append(val, style=col)
    return t


def ctxk(v):
    """Context sizes the way model cards write them: 4K, 128K, 1M."""
    if not v:
        return "—"
    return f"{v / 1048576:.0f}M" if v >= 1048576 else f"{v / 1024:.0f}K" if v >= 1024 else str(v)


def gpu_detail_box(D):
    g = D.get("gpu")
    if not g:
        return Box("GPU", note("No NVIDIA GPU (nvidia-smi not found)"), P["gpu"])
    pp = None if not g.get("power_limit_w") or g.get("power_w") is None else g["power_w"] / g["power_limit_w"] * 100
    lines = [MeterLine("util", g["util"], f"{n(g['util']):>3}%", lw=5, stops=P["ggrad"]),
             MeterLine("vram", g.get("vram_pct"), f"{n(g['vram_used_gb'], 1)}/{n(g['vram_total_gb'], 1)}G", lw=5, stops=P["vgrad"]),
             MeterLine("power", pp, f"{n(g['power_w'])}/{n(g.get('power_limit_w'))}W", lw=5, stops=P["mgrad"]),
             pairs([("temp", deg(g["temp"]), tcol(g["temp"])), ("fan", "—" if g.get("fan") is None else f"{g['fan']:.0f}%", P["fg"])]),
             pairs([("clk", f"{n(g.get('sm_mhz'))}/{n(g.get('sm_max_mhz'))}", P["fg"]), ("mem", f"{n(g.get('mem_mhz'))} MHz", P["fg"])]),
             pairs([("", clean(g.get("pstate") or "—"), P["hi"]), ("pcie", clean(g.get("pcie") or "—"), P["fg"]),
                    ("drv", clean(g.get("driver") or "—"), P["fg"])])]
    apps = g.get("apps") or []
    lines.append(note("gpu processes" if apps else "no gpu compute processes", f"bold {P['gpu']}" if apps else P["mut"]))
    lines += [kv([(f"{a['pid']} {a['name']}", f"{a['vram_gb']:.2f}G")]) for a in apps[:4]]
    graphs = stack((Graph(g.get("util_hist") or [], P["ggrad"]), None), (Graph(g.get("vram_hist") or [], P["vgrad"], down=True), None))
    info = Text(no_wrap=True)
    info.append(clean(g["name"]), style=P["fg"])
    info.append(" // util▲ vram▼", style=P["mut"])
    return Box("GPU // AI ACCELERATOR", row((graphs, ("r", 1)), (Text(" "), 1), (Group(*lines), 34)), P["gpu"], info)


def exposed(host):
    """OLLAMA_HOST bound to every interface (a fact from the config, not a guess)."""
    return host.startswith(("0.0.0.0", "[::]", ":")) or host == "::"


def config_grid(rows, w):
    """label value pairs, two per line when the box is wide enough."""
    per = 2 if w >= 48 else 1
    cw = (w - (per - 1) * 2) // per
    for i in range(0, len(rows), per):
        t = Text(no_wrap=True)
        for j, (label, val, *st) in enumerate(rows[i:i + per]):
            val = clean(val)
            lw = max(1, cw - len(val) - 1) if len(val) < cw - 6 else 6
            t.append(("  " if j else "") + fixed(label, lw) + " ", style=P["mut"])
            t.append(fixed(val, cw - lw - 1, True), style=st[0] if st else P["fg"])
        yield t


def server_box(D):
    a = D.get("ai") or {}
    o = D.get("ollama") or {}
    cfg, srv = a.get("config"), a.get("server")
    lines = []
    t = Text(no_wrap=True, overflow="ellipsis")
    up = o.get("running")
    t.append("● ", style=P["ok"] if up else P["bad"])
    t.append("running" if up else "not reachable", style=f"bold {P['ok'] if up else P['bad']}")
    if o.get("version"):
        t.append(f"  v{clean(o['version'])}", style=P["fg"])
    t.append(f"  {clean(o.get('url'))}", style=P["mut"])
    lines.append(t)
    if srv:
        s = Text(no_wrap=True, overflow="ellipsis")
        for i, (label, val) in enumerate((("pid", srv["pid"]), ("up", updays(srv["uptime_s"])), ("cpu", f"{srv['cpu']:.0f}%"),
                                          ("rss", gb(srv["rss_gb"])), ("runners", srv["runners"]))):
            s.append(("" if not i else "  ") + f"{label} ", style=P["mut"])
            s.append(clean(val), style=P["fg"])
        lines.append(s)
    env = (cfg or {}).get("env") or {}
    if cfg is None:
        lines.append(note("config: no systemd unit 'ollama' (env not readable)"))
    else:
        lines.append(note(f"config // systemd {cfg['unit']} ({cfg.get('state')})", f"bold {P['ai']}"))
        shown = [("OLLAMA_HOST", "listen"), ("OLLAMA_FLASH_ATTENTION", "flash attention"), ("OLLAMA_KV_CACHE_TYPE", "kv cache"),
                 ("OLLAMA_NUM_PARALLEL", "parallel"), ("OLLAMA_MAX_LOADED_MODELS", "max loaded"), ("OLLAMA_KEEP_ALIVE", "keep alive"),
                 ("OLLAMA_CONTEXT_LENGTH", "context"), ("OLLAMA_MAX_QUEUE", "max queue"), ("OLLAMA_MODELS", "models dir")]
        rows = [(label, env[key], P["warn"] if key == "OLLAMA_HOST" and exposed(env[key]) else P["fg"]) if key in env
                else (label, "default", P["mut"]) for key, label in shown]
        rows += [(key.replace("OLLAMA_", "").lower(), val) for key, val in env.items() if key not in dict(shown)]
        lines.append(Lines(lambda w, h, rows=rows: config_grid(rows, w)))
        if exposed(env.get("OLLAMA_HOST", "")):
            lines.append(note("! listening on all interfaces - reachable from the network", P["warn"]))
    inst = a.get("installed") or []
    if inst:
        lines.append(note(f"{len(inst)} models installed · {sum(m['size_gb'] for m in inst):.0f} GB on disk"))
    return Box("OLLAMA // SERVER", Group(*lines), P["ai"])


def loaded_box(D):
    a = D.get("ai") or {}
    loaded = a.get("loaded") or []
    if not loaded:
        keep = ((a.get("config") or {}).get("env") or {}).get("OLLAMA_KEEP_ALIVE")
        return Box("LOADED MODELS", Group(note("no model in memory - idle"),
                                          note(f"models unload after keep-alive ({keep or 'default'})")), P["tok"])

    def fn(w, h):
        nw = max(10, w - 52)
        yield Text(f"{fixed('MODEL', nw)} {fixed('PARAMS', 7, True)} {fixed('QUANT', 7)} {fixed('SIZE', 6, True)} "
                   f"{fixed('PROCESSOR', 13)} {fixed('CTX', 6, True)} {fixed('UNLOAD', 7, True)}", style=f"bold {P['mut']}", no_wrap=True)
        for m in loaded[:max(0, h - 1)]:
            gp = m.get("gpu_pct")
            proc = "—" if gp is None else "100% GPU" if gp >= 99.5 else "100% CPU" if gp <= 0.5 else f"{100 - gp:.0f}/{gp:.0f} CPU/GPU"
            t = Text(no_wrap=True)
            t.append(fixed(m["name"], nw) + " ", style=f"bold {P['fg']}")
            t.append(fixed(m.get("params") or "—", 7, True) + " ", style=P["fg"])
            t.append(fixed(m.get("quant") or "—", 7) + " ", style=P["mut"])
            t.append(fixed(gb(m["size_gb"]), 6, True) + " ", style=P["fg"])
            t.append(fixed(proc, 13) + " ", style=P["ok"] if gp and gp >= 99.5 else P["warn"])
            t.append(fixed(ctxk(m.get("context")), 6, True) + " ", style=P["fg"])
            t.append(fixed(dur(m["expires_in"]) if m.get("expires_in") is not None else "—", 7, True), style=P["mut"])
            yield t
    return Box("LOADED MODELS", Lines(fn), P["tok"], Text(f"{len(loaded)} in memory", style=P["mut"]))


def tokens_box(D):
    """Live token telemetry from the runner log, plus the on-demand benchmark result."""
    a = D.get("ai") or {}
    tk = (a.get("requests") or {}).get("tokens")
    b = a.get("bench") or {}
    loaded = a.get("loaded") or []
    model = loaded[0]["name"] if len(loaded) == 1 else None
    if not tk:
        return Box("TOKENS // THROUGHPUT", note((a.get("errors") or {}).get("requests") or "runner log not available"), P["tok"])
    live = Text(no_wrap=True, overflow="ellipsis")
    if tk["live"]:
        pulse = P["tok"] if int(time.time() * 2) % 2 else P["hi"]
        for g in tk["live"]:
            live.append("● GENERATING ", style=f"bold {pulse}")
            live.append(f"slot {g['slot']} ", style=P["mut"])
            live.append(f"{g['n_gen']:,} tok ", style=f"bold {P['fg']}")
            live.append(f"{g['tps']:.1f} tok/s  ", style=f"bold {P['tok']}")
    else:
        last = tk.get("last")
        live.append("○ idle", style=P["mut"])
        if last:
            live.append(f" · last finished {time.strftime('%H:%M:%S', time.localtime(last['ts']))}", style=P["mut"])
    if model:
        live.append(f"  {clean(model)}", style=P["fg"])
    big = Text(no_wrap=True, overflow="ellipsis")
    big.append(n(tk["gen_tps"], 1), style=f"bold {P['tok']}")
    big.append(" tok/s gen", style=P["fg"])
    big.append("  ·  ", style=P["mut"])
    big.append(n(tk["prompt_tps"], 0), style=f"bold {P['hi']}")
    big.append(" tok/s prompt", style=P["fg"])
    big.append("  ·  peak ", style=P["mut"])
    big.append(n(tk["peak_tps"], 1), style=P["fg"])
    big.append("  (1h avg)", style=P["mut"])
    top = max(tk["per_min"]) or 1
    graph = Graph([v / top * 100 for v in tk["per_min"]], (dim(P["tok"], .45), P["tok"], "#ffffff" if not P["low"] else P["tok"]))
    tot = Text(no_wrap=True, overflow="ellipsis")
    for i, (label, val) in enumerate((("requests", n(tk["tasks"])), ("in", k(tk["in"])), ("out", k(tk["out"])),
                                      ("tok/min peak", k(max(tk["per_min"]))))):
        tot.append(("" if not i else "  │ ") + f"{label} ", style=P["mut"])
        tot.append(val, style=f"bold {P['fg']}")
    last = tk.get("last")
    lt = Text(no_wrap=True, overflow="ellipsis")
    if last:
        lt.append("last ", style=P["mut"])
        lt.append(f"{n(last.get('prompt_tokens'))} in / {n(last.get('gen_tokens'))} out", style=P["fg"])
        lt.append(f"  {n(last.get('gen_tps'), 1)} tok/s  {dur((last.get('total_ms') or 0) / 1000)}", style=P["fg"])
        if last.get("ctx"):
            lt.append(f"  ctx {last['ctx']:,}", style=P["mut"])
    bench = Text(no_wrap=True, overflow="ellipsis")
    if b.get("running"):
        bench.append(f"{'|/-' + chr(92)}"[int(time.time() * 2) % 4] + f" benchmarking {clean(b.get('model'))} ...", style=P["warn"])
    elif b.get("runs"):
        r = b["runs"][0]
        bench.append("bench ", style=P["mut"])
        bench.append(f"{n(r['gen_tps'], 1)} tok/s", style=f"bold {P['tok']}")
        bench.append(f"  ttft {dur(r['ttft_s'])}  {clean(r['model'])} {time.strftime('%H:%M', time.localtime(r['at']))}", style=P["mut"])
    elif b.get("error"):
        bench.append(f"bench error: {clean(b['error'])}", style=P["bad"])
    else:
        bench.append("t = run a 128-token benchmark on the loaded model", style=P["mut"])
    body = stack((Group(live, big), 2), (graph, None), (Group(tot, lt, bench), 3))
    return Box("TOKENS // THROUGHPUT", body, P["tok"], Text("generated tok/min · 60 min", style=P["mut"]))


def requests_box(D, ui):
    a = D.get("ai") or {}
    r = a.get("requests")
    if not r:
        return Box("REQUESTS // last 1h", note((a.get("errors") or {}).get("requests") or "request log not available"), P["ai"])
    items = []
    for q in r["recent"]:
        def item(w, q=q):
            t = Text(no_wrap=True)
            bad = q["status"] >= 400
            t.append(f"{q['t']} ", style=P["mut"])
            t.append(f"{q['status']} ", style=f"bold {P['bad'] if bad else P['ok']}")
            t.append(fixed(dur(q["lat"]), 7, True) + " ", style=P["warn"] if (q["lat"] or 0) >= 30 else P["fg"])
            t.append(fixed(q["client"], 15) + " ", style=P["mut"])
            t.append(f"{q['method']:<5}", style=P["mut"])
            task = q.get("task")
            tw = 26 if task and w >= 70 else 0
            t.append(fixed(q["path"], max(6, w - len(t) - tw)), style=P["bad"] if bad else (P["tok"] if q["infer"] else P["fg"]))
            if tw:
                t.append(fixed(f"{n(task.get('gen_tokens'))} tok", 10, True), style=P["fg"])
                t.append(fixed(f"{n(task.get('gen_tps'), 1)} t/s", 11, True), style=f"bold {P['tok']}")
            return t
        items.append(item)
    stats = Text(no_wrap=True, overflow="ellipsis")
    for i, (label, val, col) in enumerate((("inference", r["inference"], P["tok"]), ("errors", r["errors"], P["bad"] if r["errors"] else P["fg"]),
                                           ("avg", dur(r["avg_s"]), P["fg"]), ("p95", dur(r["p95_s"]), P["fg"]),
                                           ("max", dur(r["max_s"]), P["fg"]), ("polls", r["polls"], P["mut"]))):
        stats.append(("" if not i else "  │ ") + f"{label} ", style=P["mut"])
        stats.append(clean(val), style=f"bold {col}")
    paths = Text(no_wrap=True, overflow="ellipsis")
    paths.append("endpoints ", style=P["mut"])
    paths.append("  ".join(f"{clean(p)}×{v}" for p, v in r["by_path"]) or "—", style=P["fg"])
    who = Text(no_wrap=True, overflow="ellipsis")
    who.append("clients   ", style=P["mut"])
    who.append("  ".join(f"{clean(c)}×{v}" for c, v in r["by_client"]) or "—", style=P["fg"])
    body = stack((Group(stats, paths, who), 3), (ScrollList("reqs", ui, items, empty="no requests in the last hour"), None))
    return Box("REQUESTS // last 1h", body, P["ai"], Text("ollama access log", style=P["mut"]))


def models_box(D, ui):
    a = D.get("ai") or {}
    inst = a.get("installed") or []
    loaded = {m["name"] for m in a.get("loaded") or []}

    def header_(w):
        nw = max(8, w - 31)
        return Text(f"  {fixed('MODEL', nw)} {fixed('PARAMS', 7, True)} {fixed('QUANT', 7)} {fixed('SIZE', 6, True)} {fixed('CTX', 5, True)}",
                    style=f"bold {P['mut']}", no_wrap=True)
    items = []
    for m in inst:
        def item(w, m=m):
            nw = max(8, w - 30)
            on = m["name"] in loaded
            t = Text(no_wrap=True)
            t.append("● " if on else "  ", style=P["ok"])
            t.append(fixed(m["name"], nw) + " ", style=f"bold {P['fg']}" if on else P["fg"])
            t.append(fixed(m.get("params") or "—", 7, True) + " ", style=P["fg"])
            t.append(fixed(m.get("quant") or "—", 7) + " ", style=P["mut"])
            t.append(fixed(gb(m["size_gb"]), 6, True) + " ", style=grad(P["mgrad"], m["size_gb"] / 20))
            t.append(fixed(ctxk(m.get("ctx_max")), 5, True), style=P["mut"])
            return t
        items.append(item)
    return Box("MODELS // installed", ScrollList("models", ui, items, header=header_, empty="no models (or Ollama down)"),
               P["tok"], Text(f"{len(inst)} · ● loaded", style=P["mut"]))


def ai_page(D, ui, w, h):
    ha = max(10, min(14, round(h * 0.28)))
    hb = max(9, min(13, round(h * 0.27)))
    if h - ha - hb < 6:
        hb = 8
        ha = max(8, h - hb - 6)
    top = row((gpu_detail_box(D), ("r", 5)), (server_box(D), ("r", 4)))
    mid = row((loaded_box(D), ("r", 4)), (tokens_box(D), ("r", 5)))
    bot = row((requests_box(D, ui), ("r", 5)), (models_box(D, ui), ("r", 4)))
    ui.split_x = w * 5 // 9
    return stack((top, ha), (mid, hb), (bot, None))


# ---------------------------------------------------------------- advanced page
def adv_page(D, ui, w, h):
    a = D.get("adv") or {}
    tcp, ker, pr, sd, ps = (a.get(x) or {} for x in ("tcp", "kernel", "proc", "systemd", "pressure"))
    stats = Table.grid(expand=True)
    items = [("events / min", n(a.get("events_per_min")), P["warn"] if (a.get("events_per_min") or 0) >= 20 else P["ok"]),
             ("context switches / s", k(a.get("ctx_per_s")), P["hi"]), ("network packets / s", k(a.get("net_pps")), P["net"]),
             ("storage latency", "—" if a.get("io_latency_ms") is None else f"{a['io_latency_ms']:.1f} ms",
              P["warn"] if (a.get("io_latency_ms") or 0) >= 10 else P["ok"])]
    for _ in items:
        stats.add_column(ratio=1, justify="center")
    stats.add_row(*[Text(v, style=f"bold {c}") for _, v, c in items])
    stats.add_row(*[Text(lab, style=P["mut"]) for lab, _, _ in items])

    def flag(v, at, bad=False):
        return (P["bad"] if bad else P["warn"]) if v is not None and v >= at else P["fg"]

    def num(v, at=None, bad=False, fmt=n):
        return (fmt(v) + (" !" if at is not None and v is not None and v >= at else ""), flag(v, at, bad) if at is not None else P["fg"])

    events = []
    for e in a.get("events") or []:
        def item(w, e=e):
            t = Text(no_wrap=True)
            col = P["bad"] if e["level"] == "bad" else P["warn"]
            t.append("• ", style=col)
            t.append(f"{clean(e['t'])} ", style=P["mut"])
            t.append(f"{'x' + str(e['count']) if e['count'] > 1 else '':<5}", style=P["hi"])
            t.append(f"{clean(e['src'])}: ", style=P["fg"])
            t.append(clean(e["msg"]), style=col)
            return t
        events.append(item)
    pressure = Group(*[MeterLine(lab, ps.get(key), "—" if ps.get(key) is None else f"{ps[key]:3.0f}%", lw=11)
                       for lab, key in (("open files", "fd"), ("pids", "pid"), ("inotify", "inotify"))])
    tcpkv = kv([("established", k(tcp.get("est"))), ("time_wait", k(tcp.get("tw")), flag(tcp.get("tw"), 10000)),
                ("close_wait", n(tcp.get("cw")), flag(tcp.get("cw"), 300)),
                ("retransmit", "—" if tcp.get("retrans_per_s") is None else f"{tcp['retrans_per_s']:.1f}/s")])
    mid = row((Box("RECENT WARNINGS", ScrollList("events", ui, events, empty="No recent warnings, or journal not readable"),
                   P["proc"], Text("newest first", style=P["mut"])), ("r", 5)),
              (stack((Box("RESOURCE PRESSURE", pressure, P["mem"]), 5), (Box("TCP", tcpkv, P["net"]), None)), ("r", 2)))
    names = [note(f"× {x}", P["bad"]) for x in sd.get("failed_names") or []]
    bot = row(
        (Box("KERNEL // 1h", kv([("warnings", n(ker.get("warnings")), flag(ker.get("warnings"), 1)),
                                 ("oom kills", n(ker.get("oom")), flag(ker.get("oom"), 1, True)),
                                 ("rcu stalls", n(ker.get("rcu_stalls")), flag(ker.get("rcu_stalls"), 1, True)),
                                 ("machine checks", n(ker.get("mce")), flag(ker.get("mce"), 1, True))]), P["bad"]), ("r", 1)),
        (Box("PROCESSES", kv([("forks", "—" if pr.get("forks_per_s") is None else f"{pr['forks_per_s']:.0f}/s"),
                              ("D-state", *num(pr.get("dstate"), 3)), ("zombies", *num(pr.get("zombies"), 1))]), P["proc"]), ("r", 1)),
        (Box("SYSTEMD", Group(kv([("restarts", "—" if sd.get("restarts_per_min") is None else f"{sd['restarts_per_min']}/min",
                                   flag(sd.get("restarts_per_min"), 1)),
                                  ("failed units", *num(sd.get("failed"), 1, True)), ("stuck jobs", n(sd.get("stuck")), flag(sd.get("stuck"), 1))]),
                              *names), P["ai"]), ("r", 1)))
    return stack((Box("OVERVIEW", stats, P["adv"]), 4), (mid, None), (bot, 2 + max(4, 3 + len(names))))


# ---------------------------------------------------------------- frame
def merge(snap, ai, net=None):
    """Overlay the AI collector's GPU / Ollama data and the network view onto the system snapshot."""
    D = dict(snap)
    if net is not None:
        D["netsec"] = net
    if not ai:
        return D
    D["gpu"], D["ollama"], D["ai"] = ai.get("gpu"), ai.get("ollama"), ai
    D["temps"] = {**(snap.get("temps") or {}), "gpu": (ai.get("gpu") or {}).get("temp")}
    return D


def build(D, page, min_size=(80, 24), size=None, sort="cpu", ui=None):
    w, h = size or (80, 24)
    ui = ui or UI(page)
    ui.page = "system" if page == "basic" else page
    if ui.sort == "cpu":
        ui.sort = sort
    root = Layout()
    if w < min_size[0] or h < min_size[1]:
        root.update(Text(f"Terminal too small ({w}x{h}); need at least {min_size[0]}x{min_size[1]}.", style=P["warn"]))
        return root
    try:
        body = {"pulse": pulse_page, "system": system_page, "ai": ai_page, "advanced": adv_page, "net": net_page}[ui.page](D, ui, w, h - 2)
    except Exception as e:  # noqa: BLE001 - never crash the dashboard on one bad value
        body = Layout(Text(f"render error: {type(e).__name__}: {e}", style=P["bad"]))
    root.split_column(Layout(header(D, ui, w), size=1), body, Layout(footer(D, ui), size=1))
    return root


# ---------------------------------------------------------------- input
_SGR = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O[A-Za-z])")
_SEQ = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "OA": "up", "OB": "down", "OC": "right", "OD": "left",
        "[5~": "pgup", "[6~": "pgdn", "[H": "home", "[F": "end", "[1~": "home", "[4~": "end", "OH": "home", "OF": "end",
        "[7~": "home", "[8~": "end", "[Z": "backtab"}
_NAMES = {"\t": "tab", "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\x03": "ctrl-c"}


def _byte(ch):
    o = ord(ch)
    return o - 0xDC00 if 0xDC80 <= o <= 0xDCFF else o  # surrogateescape'd raw byte


def parse_keys(s):
    """Terminal input -> [("key", name) | ("mouse", button, x, y, pressed)]. Handles SGR and X10 mouse reports."""
    out, i = [], 0
    while i < len(s):
        if s[i] != "\x1b":
            out.append(("key", _NAMES.get(s[i], s[i])))
            i += 1
            continue
        m = _SGR.match(s, i)
        if m:
            out.append(("mouse", int(m[1]), int(m[2]) - 1, int(m[3]) - 1, m[4] == "M"))
            i = m.end()
        elif s.startswith("\x1b[M", i) and len(s) >= i + 6:
            b = _byte(s[i + 3]) - 32
            out.append(("mouse", b, _byte(s[i + 4]) - 33, _byte(s[i + 5]) - 33, b & 3 != 3 or b & 64 != 0))
            i += 6
        else:
            m = _CSI.match(s, i)
            if m:
                if m[1] in _SEQ:
                    out.append(("key", _SEQ[m[1]]))
                i = m.end()
            else:
                out.append(("key", "esc"))
                i += 1
    return out


def move(ui, D, key):
    if ui.page != "system":
        name = {"ai": "reqs", "pulse": "timeline", "net": "svc"}.get(ui.page, "events")
        page = max(1, ui.vis.get(name, 10))
        ui.scroll[name] = max(0, ui.scroll[name] + {"up": -1, "down": 1, "pgup": -page, "pgdn": page,
                                                    "home": -10**9, "end": 10**9}[key])
        return
    rows = proc_rows(D, ui)
    if not rows:
        return
    vis = max(1, ui.vis.get("proc", 10))
    idx = next((i for i, p in enumerate(rows) if p["pid"] == ui.sel_pid), None)
    if key == "home":
        idx = 0
    elif key == "end":
        idx = len(rows) - 1
    elif idx is None:  # first press selects the top visible row
        idx = ui.scroll["proc"]
    else:
        idx += {"up": -1, "down": 1, "pgup": -vis, "pgdn": vis}[key]
    idx = max(0, min(len(rows) - 1, idx))
    ui.sel_pid = rows[idx]["pid"]
    if idx < ui.scroll["proc"]:
        ui.scroll["proc"] = idx
    elif idx >= ui.scroll["proc"] + vis:
        ui.scroll["proc"] = idx - vis + 1


def handle(ev, ui, D, ai=None):
    if ev[0] == "mouse":
        _, b, x, y, pressed = ev
        if not pressed:
            return
        if b & 64:  # wheel: 64 up, 65 down
            if ui.page == "pulse":
                name = ("insights" if y < ui.pulse_y else "programs") if x < ui.split_x else "timeline"
            elif ui.page == "net":
                name = "svc" if x < ui.split_x else ("incoming" if y < ui.pulse_y else "connlog")
            else:
                name = {"system": "proc", "advanced": "events"}.get(ui.page) or ("models" if x >= ui.split_x else "reqs")
            ui.scroll[name] = max(0, ui.scroll[name] + (3 if b & 1 else -3))
        elif b & 3 == 0:  # left click
            if y == 0:
                ui.page = next((p for x0, x1, p in ui.tabs if x0 <= x < x1), ui.page)
            elif ui.page == "system" and ui.proc_y0 is not None and ui.proc_y0 <= y < ui.proc_y0 + ui.vis.get("proc", 0):
                rows = proc_rows(D, ui)
                i = ui.scroll["proc"] + y - ui.proc_y0
                if i < len(rows):
                    ui.sel_pid = None if rows[i]["pid"] == ui.sel_pid else rows[i]["pid"]
        return
    key = ev[1]
    if key == "ctrl-c":
        raise KeyboardInterrupt
    if ui.typing:
        if key == "enter":
            ui.typing = False
        elif key == "esc":
            ui.typing, ui.filter = False, ""
        elif key == "backspace":
            ui.filter = ui.filter[:-1]
        elif len(key) == 1 and key.isprintable():
            ui.filter += key
        ui.scroll["proc"] = 0
        return
    order = [p for p, _ in PAGES]
    if key in ("tab", "right"):
        ui.page = order[(order.index(ui.page) + 1) % len(order)]
    elif key in ("backtab", "left"):
        ui.page = order[(order.index(ui.page) - 1) % len(order)]
    elif key == "a" and ui.page == "net":
        ui.show_all = not ui.show_all
    elif key in ("1", "2", "3", "4", "5"):
        ui.page = order[int(key) - 1]
    elif key in ("up", "down", "pgup", "pgdn", "home", "end"):
        move(ui, D, key)
    elif key == "s" and ui.page == "system":
        names = list(SORTS)
        ui.sort = names[(names.index(ui.sort) + 1) % len(names)]
    elif key == "r" and ui.page == "system":
        ui.reverse = not ui.reverse
    elif key in ("/", "f") and ui.page == "system":
        ui.typing = True
    elif key == "t":
        ui.say(ai.start_bench() if ai else "benchmark is off in demo mode" if D.get("demo") else "AI collector not running")
    elif key == "c":
        names = list(THEMES)
        theme = names[(names.index(P["theme"]) + 1) % len(names)]
        use_palette("standard" if P["low"] else "truecolor", theme)
        save_theme(theme)
        ui.say(f"theme: {theme}" + (" (16-colour terminal: colours limited)" if P["low"] else ""))
    elif key == "esc":
        ui.sel_pid, ui.filter = None, ""
    elif key == "q":
        ui.say("lmon keeps running - press Ctrl+C to exit")


def enrich(D, timeline):
    """Add findings and the timeline to a merged snapshot (once per refresh)."""
    D = dict(D)
    D["insights"] = analyze(D)
    D["timeline"] = list(timeline.update(D, D["insights"])) if timeline else D.get("timeline") or []
    return D


def interactive(source, ai, console, ui, interval, timeline=None):
    """Live loop. `source()` returns the merged snapshot (collectors, or demo data); `ai` runs the benchmark."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    resized = [False]

    def stop(*_):
        raise KeyboardInterrupt

    prev = {sig: signal.signal(sig, h) for sig, h in ((signal.SIGWINCH, lambda *_: resized.__setitem__(0, True)),
                                                      (signal.SIGTERM, stop), (signal.SIGHUP, stop))}
    stdin_open = True
    try:
        tty.setcbreak(fd)
        with Live(console=console, screen=True, auto_refresh=False, redirect_stdout=False, redirect_stderr=False) as live:
            console.file.write(MOUSE_ON)  # wheel/clicks come to lmon instead of scrolling the terminal
            console.file.flush()
            last, dirty = 0.0, True
            frame = enrich(source(), timeline)
            while True:
                if resized[0]:  # redraw at the new window size right away
                    resized[0], dirty = False, True
                try:
                    ready = select.select([fd] if stdin_open else [], [], [], 0.1)[0]
                except InterruptedError:
                    continue
                if ready:
                    data = os.read(fd, 4096)
                    if not data:
                        stdin_open = False  # keyboard gone; keep showing data until a signal stops us
                    D = source()
                    for ev in parse_keys(data.decode("utf-8", "surrogateescape")):
                        try:
                            handle(ev, ui, D, ai)
                        except KeyboardInterrupt:
                            raise
                        except Exception as e:  # noqa: BLE001 - a bad key must never end the session
                            ui.say(f"input error: {type(e).__name__}")
                    dirty = True
                if time.monotonic() - last >= interval:  # new data once per interval; keys/resizes only redraw
                    frame = enrich(source(), timeline)
                    last, dirty = time.monotonic(), True
                if dirty:
                    live.update(build(frame, ui.page, size=console.size, ui=ui), refresh=True)
                    dirty = False
    finally:
        try:
            console.file.write(MOUSE_OFF)
            console.file.flush()
        except OSError:
            pass
        for sig, h in prev.items():
            signal.signal(sig, h)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version as pkg_version
        return pkg_version("linux-monitor")
    except PackageNotFoundError:
        return "dev (not installed)"


def color_system(choice):
    """btop-style default: 24-bit colour unless the terminal clearly cannot do it (Linux VT, dumb)."""
    if choice == "16":
        return "standard"
    if choice in ("256", "truecolor"):
        return choice
    if os.getenv("NO_COLOR"):
        return None
    if os.getenv("TERM", "") in ("linux", "dumb", "vt100", "vt220"):
        return "auto"
    return "truecolor"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lmon", description="Linux + AI workload monitor: terminal dashboard (default) or web server (--web)")
    ap.add_argument("--page", choices=["pulse", "system", "ai", "advanced", "net", "basic"], default="pulse")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--color", choices=["auto", "truecolor", "256", "16"], default=os.getenv("LMON_COLOR", "auto"),
                    help="colour depth (default: 24-bit like btop; use 256 or 16 for old terminals)")
    ap.add_argument("--graph", choices=["braille", "block"], default=os.getenv("LMON_GRAPH", "braille"),
                    help="graph glyphs: braille (like btop) or block (for fonts without braille)")
    ap.add_argument("--theme", choices=list(THEMES), default=os.getenv("LMON_THEME") or load_theme() or DEFAULT_THEME,
                    help="colour theme (default: last one picked with the c key, else btop)")
    ap.add_argument("--web", action="store_true", help="run the web dashboard server (MONITOR_HOST/MONITOR_PORT)")
    ap.add_argument("--demo", action="store_true", help="show a made-up machine (for screenshots and trying lmon); reads nothing")
    ap.add_argument("--version", action="version", version=f"lmon {version()}")
    args = ap.parse_args(argv)
    if args.web:
        from .__main__ import main as serve
        serve()
        return 0
    GRAPH["mode"] = args.graph
    if args.demo:  # made-up machine: no collector runs, nothing about this computer is read
        from . import demo
        ui = UI(args.page)
        try:
            if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
                cs = color_system(args.color) if sys.stdout.isatty() or args.color != "auto" else "auto"
                console = Console(width=args.width, height=args.height, color_system=cs)
                use_palette(console.color_system, args.theme)
                console.print(build(enrich(demo.snapshot(), None), ui.page, size=console.size, ui=ui))
                return 0
            console = Console(color_system=color_system(args.color))
            use_palette(console.color_system, args.theme)
            return interactive(demo.snapshot, None, console, ui, args.interval)
        except KeyboardInterrupt:
            return 0
    from .ai import AICollector
    from .netsec import NetSec
    sampler = Sampler(interval=args.interval, procs=True, external_ai=True)
    ai = AICollector(interval=args.interval)
    net = NetSec()
    sampler.start()
    ai.start()
    net.start()
    ui = UI(args.page)
    try:
        if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
            time.sleep(2.2)  # let rate counters and history accumulate
            cs = color_system(args.color) if sys.stdout.isatty() or args.color != "auto" else "auto"
            console = Console(width=args.width, height=args.height, color_system=cs)
            use_palette(console.color_system, args.theme)
            console.print(build(enrich(source(), Timeline(write=False)), ui.page, size=console.size, ui=ui))
            return 0
        console = Console(color_system=color_system(args.color))
        use_palette(console.color_system, args.theme)
        return interactive(lambda: merge(sampler.snapshot, ai.snapshot, net.snapshot), ai, console, ui, args.interval, Timeline())
    except KeyboardInterrupt:
        return 0
    finally:
        sampler.stop(wait=False)
        ai.stop()
        net.stop()


if __name__ == "__main__":
    raise SystemExit(main())
