# Contributing to lmon

Thanks for helping. Bug reports, ideas, docs and code are all welcome. For anything bigger than a small fix,
please open an issue first so we can agree on the approach before you spend time on it.

## Ground rules

These are what make lmon trustworthy. Pull requests that break them will be asked to change.

1. **Real data only.** Every number on screen comes from a source on the machine (`/proc`, `/sys`, a command's
   output, a local API, a log). No estimates, no simulated values, no "typical" defaults dressed up as
   measurements. If a source is missing, show `—` and say why in the panel.
2. **Read-only.** lmon never changes the system. The only thing it sends is the `t` benchmark, which the user
   triggers and which only uses an already-loaded model.
3. **No new outbound traffic.** No telemetry, no update checks, no calls beyond the local Ollama endpoint.
4. **Fail per collector, not per screen.** A collector that cannot read its source returns `None` (and records why);
   the rest of the dashboard keeps working.
5. **Untrusted text is cleaned.** Process names, command lines, log messages and hostnames go through
   `clean()` before display (it strips terminal escape sequences).
6. **Say what you are unsure about.** Findings and labels that are inferred rather than measured must say so
   (for example the `name?` suffix for a likely, unverified port owner).
7. **Cheap to run.** lmon should stay under ~10% of one CPU core. Expensive sources get their own refresh
   interval (see `_every()` in `monitor/ai.py` and `monitor/netsec.py`).

## Development setup

```bash
git clone https://github.com/arunlearn13-cmd/Linux-Lmon.git
cd Linux-Lmon
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest                       # all tests, a few seconds
lmon                         # run your working copy
lmon --demo                  # a made-up machine: use it to check layouts and for screenshots
lmon --once --page net --width 160 --height 45   # one frame, no TTY needed
```

Tests run on Python 3.9 to 3.13 in CI; please keep 3.9 syntax working.

## How the code is organised

| File | What it does |
|---|---|
| `monitor/sampler.py` | System metrics shared by terminal and web UI: CPU, memory, disks, network, processes, journal, kernel, systemd. |
| `monitor/ai.py` | GPU detail (`nvidia-smi`), Ollama API, its systemd config, the request/token log parser, the benchmark. |
| `monitor/netsec.py` | Listening sockets, Docker port mapping, firewall state, who is connected (host and container namespaces). |
| `monitor/insights.py` | Health rules (`analyze`), the score/verdict, and the timeline that is kept on disk. |
| `monitor/tui.py` | The terminal UI: themes, panels, pages, input handling, the main loop. |
| `monitor/server.py`, `monitor/static/index.html` | The web dashboard and `/api/metrics`. |
| `monitor/userpaths.py` | Where per-user files live (also under `sudo`). |
| `monitor/demo.py` | The made-up machine behind `lmon --demo` and the README screenshots (`scripts/screenshots.py`). |

Data flows one way: collectors run in background threads and publish a snapshot dict; the UI merges the
snapshots (`merge`), adds findings and the timeline (`enrich`) and renders (`build`). Rendering never blocks on I/O.

## Common changes

**A new insight rule** - add it to `analyze()` in `monitor/insights.py`. Give it a stable `key` (the timeline uses
it to detect appear/resolve), a short title with the measured value, and a detail line explaining why it matters.
Add a test in `tests/test_insights.py` that shows it fires on the bad value and not on a normal one.

**A new theme** - add an entry to `THEMES` in `monitor/tui.py` with the same keys as the existing ones. Check it
with `lmon --theme yours` and in 16-colour mode (`lmon --color 16`).

**A new data source** - put the collector next to related ones, return plain dicts, handle the source being
absent, and give it a refresh interval that matches its cost.

**Drawing** - panel chrome uses only WGL4 characters (they exist in every Windows console font, e.g. PuTTY's
default). Braille is allowed in graphs only, with the block fallback for `--graph block`.

## Pull requests

- Keep each PR to one change, and match the style of the surrounding code.
- Add or update tests; run `pytest` before pushing.
- For UI changes, include a screenshot taken with `lmon --demo` (never of your own machine - lmon shows hostnames,
  addresses, process and container names). If the change affects the README images, run
  `python scripts/screenshots.py` and commit the updated `docs/screenshots/*.svg`.
- New fields in a page need matching made-up values in `monitor/demo.py`, so demo mode and the screenshots show them.
- Add a line under "Unreleased" in `CHANGELOG.md`.

## Reporting bugs

Use the bug report template. The output of `lmon --version`, your distro, Python version and terminal help a lot.
Security problems go to [SECURITY.md](SECURITY.md), not to public issues.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
