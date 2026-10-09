"""Demo mode must never show anything about the machine it runs on."""
import getpass
import re
import socket
from pathlib import Path

import pytest
from rich.console import Console

from monitor import ai, demo, netsec, sampler, tui

IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){3,7}\b", re.I)
SCREENSHOTS = Path(__file__).resolve().parent.parent / "docs" / "screenshots"


def render(page, w=150, h=42):
    c = Console(width=w, height=h, record=True, file=open("/dev/null", "w"))
    c.print(tui.build(tui.enrich(demo.snapshot(), None), page, size=(w, h)))
    return c.export_text()


def private_markers():
    return {getpass.getuser(), socket.gethostname()} - {"demo", "root", ""}


@pytest.mark.parametrize("page", ["pulse", "system", "ai", "advanced", "net"])
def test_demo_pages_show_no_real_details(page):
    out = render(page)
    assert "DEMO DATA" in out and "demo@demo-server" in out
    for marker in private_markers():
        assert marker not in out, marker
    assert not IP.search(out), IP.search(out)


def test_demo_reads_nothing(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("a real collector was started in demo mode")
    for target in (sampler.Sampler, ai.AICollector, netsec.NetSec):
        monkeypatch.setattr(target, "start", boom)
    monkeypatch.setattr(sampler, "run", boom)  # no commands either
    assert tui.main(["--demo", "--once", "--page", "net", "--width", "120", "--height", "35", "--color", "16"]) == 0
    assert "DEMO DATA" in capsys.readouterr().out


def test_committed_screenshots_are_clean():
    files = sorted(SCREENSHOTS.glob("*.svg"))
    assert files, "run: python scripts/screenshots.py"
    for f in files:
        text = f.read_text()
        visible = " ".join(re.findall(r">([^<]*)<", text)).replace("&#160;", " ")
        for marker in private_markers():
            assert marker not in visible, (f.name, marker)
        assert not IP.search(visible), (f.name, IP.search(visible))
        assert "DEMO" in visible
