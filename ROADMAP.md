# Roadmap

Where lmon is heading. These are directions, not promises - priorities move with what users need. Anyone can
propose an item: open an issue with the "Feature request" template and explain the problem it solves. Items someone is
working on get an issue with the `roadmap` label, so you can see what is in progress and avoid duplicate work.

## Next

- **Publish to PyPI** so `pipx install <name>` works without a git URL (pick a name that is free on PyPI).
- **Config file** (`~/.config/lmon/config.toml`) for thresholds of the insight rules, refresh intervals and the
  default page.
- **Web dashboard parity**: the pulse, ai and net views in the browser, using the same collectors.

## Later

- **More GPUs**: AMD (`rocm-smi` / amdgpu sysfs) and Intel (`intel_gpu_top`, sysfs).
- **More AI runtimes**: vLLM and llama.cpp server (`/metrics`), LM Studio, text-generation-inference.
- **Containers page**: per-container CPU, memory, network and restarts (Docker / Podman).
- **Storage health**: SMART, NVMe wear, RAID / ZFS / btrfs status.
- **Alerts**: optional desktop notification or webhook when the health verdict changes.
- **Non-systemd systems**: fall back gracefully on distros without systemd / journald.

## Good first issues

Small, self-contained tasks for new contributors (each needs a test):

- A new insight rule with a clear measured trigger (see "A new insight rule" in CONTRIBUTING.md).
- A new colour theme.
- Recognise more services by Docker image or process name in `KINDS` (`monitor/netsec.py`).
- Help text overlay on `?` listing the keys of the current page.

## Done

See [CHANGELOG.md](CHANGELOG.md).
