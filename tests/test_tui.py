from rich.console import Console

from monitor import tui
from monitor.tui import build, clean

N = 32
H = [(i * 7) % 90 for i in range(120)]
SNAP = {
    "host": "h", "kernel": "k", "cpu_model": "Test CPU", "ts": __import__("time").time(), "uptime_s": 99999, "errors": {},
    "cpu": {"pct": 12, "per_core": [30] * N, "core_temps": [55] * N, "freq_mhz": [4200] * N, "freq_max_mhz": 4800,
            "times": [], "user": 10, "system": 2, "iowait": .1, "irq": .2, "hist": H, "core_hist": [H[:60]] * N,
            "load": [1, 2, 3], "temp": 50, "power_w": None},
    "mem": {"pct": 17, "used_gb": 10, "total_gb": 32, "avail_gb": 20, "swap_used_gb": 0}, "gpu": None,
    "net": {"ifaces": [{"name": "eth0", "kind": "ethernet", "up": True, "rx_mbps": 1, "tx_mbps": 2}], "rx_pps": 10, "tx_pps": 3},
    "disk": {"name": "sda", "read_mbs": 0, "write_mbs": 1, "iops": 84, "latency_ms": 1.1, "temp": None,
             "mounts": [{"path": "/", "pct": 20, "used_gb": 100, "total_gb": 500}]},
    "temps": {"cpu": 41, "gpu": None, "nvme": None, "board": 28, "ssd": None},
    "ollama": {"running": True, "url": "http://127.0.0.1:11434", "models": []},
    "top": {"count": 2, "procs": [{"pid": 1, "name": "init", "user": "root", "cpu": 3.0, "mem": 0.1, "threads": 1, "cmd": "/sbin/init"},
                                  {"pid": 42, "name": "evil\x1b[2J", "user": "u", "cpu": 150.0, "mem": 2.0, "threads": 9,
                                   "cmd": "x\x1b]0;pwned\x07"}]},
    "adv": {"events_per_min": 0, "ctx_per_s": 23000, "net_pps": 13, "io_latency_ms": 1.1,
            "events": [{"t": "01-15 10:00:00", "msg": "boom\x1b[31m", "src": "pipewire", "count": 5, "level": "warn"}],
            "pressure": {"fd": 1, "pid": 1, "inotify": 0}, "tcp": {"est": 6, "tw": 1, "cw": 0, "retrans_per_s": 1},
            "kernel": {"warnings": 0, "oom": 0, "rcu_stalls": 0, "mce": 0}, "proc": {"forks_per_s": 0, "dstate": 0, "zombies": 1},
            "systemd": {"restarts_per_min": 0, "failed": 1, "failed_names": ["foo.service"], "stuck": 0}},
}


def frame(page, w, h):
    c = Console(width=w, height=h, record=True, file=open("/dev/null", "w"))
    c.print(build(SNAP, page, size=(w, h)))
    return c.export_text()


def test_pages_fit_and_render():
    for w, h in ((160, 45), (120, 35), (80, 24)):
        for page in ("pulse", "basic", "ai", "advanced"):
            out = frame(page, w, h).splitlines()
            assert len(out) == h and all(len(l) <= w for l in out)
    assert "C27" in frame("basic", 120, 35)


def test_control_codes_stripped():
    assert clean("a\x1b[31mb\x07") == "a[31mb"
    assert "\x1b" not in frame("advanced", 120, 35)


def test_no_placeholder_boxes_and_threads_spread_on_wide_terminals():
    out = frame("basic", 200, 50)
    assert "╭" not in out and "╰" not in out  # rich draws a rounded placeholder for empty Layouts
    # more than one column of threads when there is room
    assert any("C0 " in l and len(__import__("re").findall(r"\bC\d+\b", l)) > 1 for l in out.splitlines())


def test_process_list_shown_and_sanitised():
    out = frame("basic", 120, 35)
    assert "evil" in out and "/sbin/init" in out and "\x1b" not in out
    assert out.index("evil") < out.index("/sbin/init")  # sorted by CPU, busiest first


def test_only_wgl4_glyphs_in_block_mode():
    """Glyphs missing from Windows console fonts (PuTTY default) render as empty boxes."""
    bad = set("▁▂▃▅▆▇╭╮╰╯⚠✖›") | {chr(c) for c in range(0x2800, 0x2900)}
    tui.GRAPH["mode"] = "block"
    try:
        for page in ("pulse", "system", "ai", "advanced"):
            assert not bad & set(frame(page, 160, 45))
    finally:
        tui.GRAPH["mode"] = "braille"
    assert any(0x2800 <= ord(ch) < 0x2900 for ch in frame("system", 160, 45))  # braille graphs by default


def test_16_colour_palette_renders():
    tui.use_palette("standard")
    try:
        test_pages_fit_and_render()
    finally:
        tui.use_palette("truecolor")
