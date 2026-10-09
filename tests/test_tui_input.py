import pytest

from rich.console import Console

from monitor import tui
from monitor.tui import UI, handle, parse_keys

from test_tui import SNAP, frame

PROCS = [{"pid": i, "name": f"p{i}", "user": "u", "cpu": float(100 - i), "mem": 0.1, "rss": 1 << 20, "threads": 1,
          "cmd": f"/bin/p{i}"} for i in range(1, 61)]
D = {**SNAP, "top": {"count": len(PROCS), "procs": PROCS}}


def draw(ui, page="system", w=120, h=35):
    """Render a frame: list sizes and click targets are recorded while drawing, as in the live loop."""
    Console(width=w, height=h, file=open("/dev/null", "w")).print(tui.build(D, page, size=(w, h), ui=ui))


def test_parse_keys_arrows_mouse_and_text():
    evs = parse_keys("a\x1b[A\x1bOB\x1b[5~\x1b[<65;10;20M\x1b[<0;3;1M\x1b[<0;3;1m\t\x1b")
    assert evs == [("key", "a"), ("key", "up"), ("key", "down"), ("key", "pgup"), ("mouse", 65, 9, 19, True),
                   ("mouse", 0, 2, 0, True), ("mouse", 0, 2, 0, False), ("key", "tab"), ("key", "esc")]
    assert parse_keys("\x1b[M`!!") == [("mouse", 64, 0, 0, True)]  # X10 wheel up at (0, 0)
    assert parse_keys("\x1b[99;5Zx") == [("key", "x")]  # unknown sequences are swallowed, not typed


def test_only_ctrl_c_exits():
    ui = UI()
    for key in ("q", "esc", "x"):
        handle(("key", key), ui, D)  # must not raise
    with pytest.raises(KeyboardInterrupt):
        handle(("key", "ctrl-c"), ui, D)


def test_wheel_scrolls_list_and_arrows_select():
    ui = UI("system")
    draw(ui)
    assert ui.vis["proc"] > 0
    handle(("mouse", 65, 50, 30, True), ui, D)  # wheel down
    assert ui.scroll["proc"] == 3
    handle(("mouse", 64, 50, 30, True), ui, D)
    assert ui.scroll["proc"] == 0
    handle(("key", "down"), ui, D)
    assert ui.sel_pid == 1  # busiest process first
    handle(("key", "down"), ui, D)
    assert ui.sel_pid == 2
    handle(("key", "end"), ui, D)
    assert ui.sel_pid == 60 and ui.scroll["proc"] == 60 - ui.vis["proc"]


def test_click_selects_process_and_switches_tab():
    ui = UI("system")
    draw(ui)
    handle(("mouse", 0, 20, ui.proc_y0 + 2, True), ui, D)
    assert ui.sel_pid == 3
    x0, _, page = ui.tabs[2]
    handle(("mouse", 0, x0 + 1, 0, True), ui, D)
    assert ui.page == page == "ai"


def test_filter_and_sort():
    ui = UI("system")
    for ch in "/p5":
        handle(("key", ch), ui, D)
    handle(("key", "enter"), ui, D)
    rows = tui.proc_rows(D, ui)
    assert ui.filter == "p5" and {p["name"] for p in rows} == {"p5", *(f"p5{i}" for i in range(10))}
    handle(("key", "s"), ui, D)
    assert ui.sort == "mem"
    handle(("key", "r"), ui, D)
    assert ui.reverse


def test_pages_cycle():
    ui = UI()
    assert ui.page == "pulse"  # lmon opens on its own home screen
    for expect in ("system", "ai", "advanced", "net", "pulse"):
        handle(("key", "tab"), ui, D)
        assert ui.page == expect
    handle(("key", "4"), ui, D)
    assert ui.page == "advanced"


def test_theme_key_cycles_renders_and_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "THEME_FILE", str(tmp_path / "theme"))
    tui.use_palette("truecolor", "aurora")
    ui = UI()
    try:
        seen = []
        for _ in tui.THEMES:
            seen.append(tui.P["theme"])
            for page in ("pulse", "system", "ai"):
                draw(ui, page)
            handle(("key", "c"), ui, D)
        assert sorted(seen) == sorted(tui.THEMES) and tui.P["theme"] == "aurora"
        assert tui.load_theme() == "aurora"
    finally:
        tui.use_palette("truecolor", "aurora")
