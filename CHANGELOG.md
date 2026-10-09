# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/) (0.x: anything may still change).

## [Unreleased]

## [0.1.0] - 2026-10-09

First public release.

### Added
- `lmon` terminal dashboard (runs until Ctrl+C) with mouse support, resize-aware layout and four colour themes
  (`aurora`, `btop`, `neon`, `classic`), braille or block graphs, 24-bit / 256 / 16 colour modes.
- **pulse** page: health score and verdict, live vitals, plain-language insights, a timeline kept across
  restarts, network access summary and processes grouped by program.
- **system** page: CPU graph and per-thread history, memory, disks, network, GPU, Ollama summary and a
  selectable, filterable, sortable process list.
- **ai** page: NVIDIA GPU detail, Ollama server and its systemd configuration, loaded/installed models, live
  tokens/s and per-request token figures from Ollama's runner log, request latency/errors, on-demand benchmark.
- **advanced** page: journal warnings, resource pressure, TCP, kernel, process and systemd health.
- **net** page: firewall state, every listening service with what it is and who it is open to, who is
  connected now (also inside Docker containers) and a log of new connections.
- `sudo lmon` support: firewall rules, every port's owner, CPU power; per-user files stay owned by the user.
- Web dashboard and `/api/metrics` (`lmon --web`), localhost by default, optional bearer token.
- `lmon --demo`: a made-up machine for trying lmon and for screenshots; reads nothing from the computer.
  README screenshots are generated from it (`scripts/screenshots.py`).

[Unreleased]: https://github.com/arunlearn13-cmd/Linux-Lmon/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/arunlearn13-cmd/Linux-Lmon/releases/tag/v0.1.0
