"""Regenerate the README screenshots from demo mode (made-up data - nothing about this machine is read).

    python scripts/screenshots.py            # writes docs/screenshots/<page>.svg

Uses the default theme, a fixed clock (UTC) and fixed demo time, so the images only change when the UI does.
"""
import io
import os
import sys
import time
from pathlib import Path

os.environ["TZ"] = "UTC"
time.tzset()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402

from monitor import demo, tui  # noqa: E402

WIDTH, HEIGHT = 150, 42
FIXED = 1767225600 + 10 * 3600 + 42 * 60  # a fixed moment, so screenshots are reproducible


def main():
    out = ROOT / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    class FrozenTime:  # the UI's clock and "today" both read this fixed moment
        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return FIXED

        def localtime(self, secs=None):
            return time.localtime(FIXED if secs is None else secs)

        def strftime(self, fmt, t=None):
            return time.strftime(fmt, time.localtime(FIXED) if t is None else t)
    tui.time = FrozenTime()
    for page in ("pulse", "system", "ai", "net", "advanced"):
        console = Console(width=WIDTH, height=HEIGHT, record=True, color_system="truecolor", file=io.StringIO())
        tui.use_palette(console.color_system, tui.DEFAULT_THEME)
        console.print(tui.build(tui.enrich(demo.snapshot(FIXED), None), page, size=(WIDTH, HEIGHT)))
        path = out / f"{page}.svg"
        console.save_svg(str(path), title=f"lmon --demo  ·  {page}")
        print("wrote", path.relative_to(ROOT))


if __name__ == "__main__":
    main()
