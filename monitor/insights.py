"""Insights and timeline for the terminal UI.

analyze(D)  turns one merged snapshot into plain-language findings. Every rule tests a measured value
            (no prediction, no guessing); each finding has a stable key so the timeline can tell when it
            appears and when it resolves.
Timeline    remembers what happened - findings appearing / resolving, models loading / unloading, Ollama
            going up / down, finished generations, benchmarks - and keeps it in
            ~/.local/state/lmon/timeline.jsonl so it survives restarts ("what happened while I was away").
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import defaultdict

from . import userpaths

BAD, WARN, INFO = "bad", "warn", "info"
WEIGHT = {BAD: (20, 60), WARN: (5, 25), INFO: (1, 5)}  # points per finding, cap per severity
STATE_FILE = os.path.join(userpaths.home(), ".local/state/lmon/timeline.jsonl")
INFER = ("/api/chat", "/api/generate", "/api/embed", "/api/embeddings")
HOLD = 20     # seconds a finding must persist (or stay gone) before the timeline records it
SETTLE = 20   # seconds after start while slower collectors fill in; findings seen then are "already open"


def _f(level, key, title, detail=""):
    return {"level": level, "key": key, "title": title, "detail": detail}


def _busiest(D):
    procs = (D.get("top") or {}).get("procs") or []
    p = max(procs, key=lambda p: p["cpu"], default=None)
    return f"{p['name']} ({p['cpu']:.0f}%)" if p and p["cpu"] >= 1 else None


def every(ts):
    """Median gap between timestamps, as text ('~10 min'), when they look periodic."""
    if len(ts) < 3:
        return None
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    med = statistics.median(gaps)
    if med <= 0 or max(abs(g - med) for g in gaps) > max(5, med * 0.2):
        return None
    return f"~{med / 60:.0f} min" if med >= 90 else f"~{med:.0f} s"


def analyze(D) -> list[dict]:
    out = []
    c, m, d = D.get("cpu") or {}, D.get("mem") or {}, D.get("disk") or {}
    a = D.get("adv") or {}
    ai = D.get("ai") or {}

    # ---- CPU
    if c:
        t = c.get("temp")
        if t is not None and t >= 75:
            hot = [i for i, v in enumerate(c.get("core_temps") or []) if v is not None and v >= t - 1]
            out.append(_f(BAD if t >= 85 else WARN, "cpu.hot", f"CPU package at {t:.0f}°C",
                          (f"hottest threads C{', C'.join(map(str, hot[:4]))}" if hot else "") + " · sustained heat throttles clocks"))
        hist = (c.get("hist") or [])[-30:]
        if len(hist) >= 10 and sum(hist) / len(hist) >= 85:
            out.append(_f(WARN, "cpu.busy", f"CPU busy: {sum(hist) / len(hist):.0f}% average over {len(hist)}s",
                          f"busiest process {_busiest(D) or '—'}"))
        pinned = [i for i, h in enumerate(c.get("core_hist") or []) if len(h) >= 10 and min(h[-10:]) >= 95]
        if pinned:
            out.append(_f(INFO, "cpu.pinned", f"{len(pinned)} thread{'s' if len(pinned) > 1 else ''} pinned at 100% for 10s+",
                          f"C{', C'.join(map(str, pinned[:6]))} · busiest process {_busiest(D) or '—'}"))
        n = len(c.get("per_core") or []) or 1
        if c.get("load") and c["load"][0] > n:
            out.append(_f(WARN, "cpu.load", f"load {c['load'][0]:.1f} exceeds {n} threads", "work is queueing for CPU time"))
        if (c.get("iowait") or 0) >= 20:
            out.append(_f(WARN, "cpu.iowait", f"I/O wait {c['iowait']:.0f}%", "CPUs idle waiting for storage"))

    # ---- memory / disk
    if m and m.get("total_gb"):
        avail = m["avail_gb"] / m["total_gb"] * 100
        if avail < 20:
            out.append(_f(BAD if avail < 10 else WARN, "mem.low", f"only {avail:.0f}% memory available ({m['avail_gb']:.1f} GiB)",
                          "the kernel will start reclaiming cache, then swapping"))
        if (m.get("swap_pct") or 0) >= 50:
            out.append(_f(WARN, "mem.swap", f"swap {m['swap_pct']:.0f}% used", f"{m['swap_used_gb']:.1f} GiB swapped out"))
    for mt in d.get("mounts") or []:
        if mt["pct"] >= 80:
            out.append(_f(BAD if mt["pct"] >= 90 else WARN, f"disk.full:{mt['path']}", f"{mt['path']} is {mt['pct']:.0f}% full",
                          f"{mt['total_gb'] - mt['used_gb']:.0f} GiB left of {mt['total_gb']:.0f}"))

    # ---- GPU / models
    g = D.get("gpu") or {}
    if g.get("temp") is not None and g["temp"] >= 83:
        out.append(_f(WARN, "gpu.hot", f"GPU at {g['temp']:.0f}°C", "close to the point where NVIDIA cards lower clocks"))
    if g.get("vram_used_gb") is not None and g.get("vram_total_gb") and g["vram_used_gb"] / g["vram_total_gb"] >= 0.95:
        out.append(_f(WARN, "gpu.vram", f"VRAM {g['vram_used_gb']:.1f}/{g['vram_total_gb']:.0f} GB full",
                      "the next model or a longer context will spill to CPU"))
    for lm in ai.get("loaded") or []:
        gp = lm.get("gpu_pct")
        if gp is not None and gp < 99.5:
            out.append(_f(WARN, f"model.split:{lm['name']}", f"{lm['name']} runs {100 - gp:.0f}% on CPU",
                          "it does not fit in VRAM - layers on CPU generate much slower"))

    # ---- Ollama
    o = D.get("ollama")
    if o is not None and not o.get("running"):
        out.append(_f(BAD, "ollama.down", "Ollama is not reachable", str(o.get("url") or "")))
    reqs = (ai.get("requests") or {}).get("recent") or []
    errs = defaultdict(list)
    for q in reqs:
        if q["status"] >= 400:
            errs[(q["status"], q["method"], q["path"].split("?")[0], q["client"])].append(q.get("ts") or 0)
    for (status, method, path, client), ts in sorted(errs.items(), key=lambda kv: -len(kv[1])):
        if len(ts) < 3:
            continue
        why = "Ollama answers 404 here when the requested model is not installed" if status == 404 and path in INFER else ""
        period = every(sorted(ts))
        out.append(_f(WARN, f"ollama.err:{status}:{path}:{client}", f"{len(ts)}x {status} {method} {path} from {client}",
                      " · ".join(x for x in ((f"repeats every {period}" if period else ""), why) if x)))
    env = ((ai.get("config") or {}).get("env") or {})
    host = env.get("OLLAMA_HOST", "")
    if host.startswith(("0.0.0.0", "[::]", ":")):
        out.append(_f(INFO, "ollama.exposed", f"Ollama listens on all interfaces ({host})", "anyone who can reach this host can use the models"))

    # ---- network exposure (monitor.netsec view)
    ns = D.get("netsec") or {}
    tcp = [r for r in ns.get("services") or [] if r["proto"] == "tcp"]
    everyone = [r for r in tcp if r["open_to"] == "everyone"]
    fw = ns.get("firewall") or {}
    nets = ", ".join(f"{n} {a}" for _, n, a in ns.get("attached") or [])
    if everyone and fw and not fw.get("on"):
        why = "UFW is installed but disabled" if fw.get("ufw_installed") else "no ufw / firewalld / nftables active"
        out.append(_f(WARN, "net.nofirewall", f"no firewall: {len(everyone)} services open to every network",
                      f"{why} · reachable via {nets}" if nets else why))
    risky = [r for r in everyone if r["what"] in ("database", "cache / key-value", "vector database", "admin web UI")]
    if risky:
        out.append(_f(WARN, "net.risky", f"{len(risky)} databases / admin UIs open to every network",
                      ", ".join(f"{r['name'] or r['port']}:{r['port']}" for r in risky[:6]) + (" …" if len(risky) > 6 else "")
                      + " · bind them to localhost or firewall them"))
    if fw.get("ufw_enabled") and any(r["via"] == "docker" for r in everyone):
        out.append(_f(INFO, "net.dockerbypass", "Docker-published ports bypass UFW",
                      "Docker's iptables rules come before UFW's, so UFW does not filter them"))
    internet = (ns.get("by_from") or {}).get("internet")
    if internet:
        ips = sorted({c["ip"] for c in ns.get("incoming") or [] if c["from"] == "internet"})
        out.append(_f(WARN, "net.internet", f"{internet} connection(s) from the internet right now", ", ".join(ips[:5])))

    # ---- system health
    sd, ker, pr, tcp = (a.get(x) or {} for x in ("systemd", "kernel", "proc", "tcp"))
    if sd.get("failed"):
        out.append(_f(BAD, "systemd.failed", f"{sd['failed']} failed systemd unit{'s' if sd['failed'] > 1 else ''}",
                      ", ".join(sd.get("failed_names") or [])))
    if (sd.get("restarts_per_min") or 0) >= 1:
        out.append(_f(WARN, "systemd.restarts", f"{sd['restarts_per_min']} service restart(s) in the last minute",
                      "a unit is restarting in a loop"))
    if ker.get("oom"):
        out.append(_f(BAD, "kernel.oom", f"{ker['oom']} out-of-memory kill(s) in the last hour", "the kernel killed processes to free RAM"))
    if ker.get("mce"):
        out.append(_f(BAD, "kernel.mce", f"{ker['mce']} machine-check event(s) in the last hour", "hardware reported an error"))
    if ker.get("rcu_stalls"):
        out.append(_f(BAD, "kernel.rcu", f"{ker['rcu_stalls']} RCU stall(s) in the last hour", "a CPU stopped responding to the kernel"))
    if (pr.get("zombies") or 0) >= 5:
        out.append(_f(WARN, "proc.zombie", f"{pr['zombies']} zombie processes", "a parent is not reaping its children"))
    if (pr.get("dstate") or 0) >= 3:
        out.append(_f(WARN, "proc.dstate", f"{pr['dstate']} processes stuck in uninterruptible I/O", "usually slow or hung storage / NFS"))
    if (tcp.get("cw") or 0) >= 300:
        out.append(_f(WARN, "tcp.closewait", f"{tcp['cw']} TCP connections in CLOSE_WAIT", "an application is not closing sockets"))
    for e in (a.get("events") or [])[:20]:
        if e["count"] >= 20:
            out.append(_f(WARN, f"journal.noisy:{e['src']}", f"{e['src']} logged the same warning {e['count']}x",
                          e["msg"][:120]))
    order = {BAD: 0, WARN: 1, INFO: 2}
    return sorted(out, key=lambda f: order[f["level"]])


def score(findings) -> int:
    """100 = nothing to report. Each severity has a cap, so many small findings never look like an outage."""
    lost = 0
    for level, (per, cap) in WEIGHT.items():
        lost += min(cap, per * sum(f["level"] == level for f in findings))
    return max(0, 100 - lost)


def verdict(findings) -> str:
    """The word follows the worst findings, not the arithmetic: 2+ critical = CRITICAL, 1 = DEGRADED."""
    bad = sum(f["level"] == BAD for f in findings)
    warn = sum(f["level"] == WARN for f in findings)
    return "CRITICAL" if bad >= 2 else "DEGRADED" if bad else "WATCH" if warn else "NOMINAL"


class Timeline:
    def __init__(self, path: str | None = STATE_FILE, keep: int = 400, write: bool = True):
        self.path, self.keep, self.write = path, keep, write
        self.events: list[dict] = self._load()
        self._started = time.time()
        self._settled = False
        self._logged: dict = {}    # findings the timeline has announced, as first announced
        self._pending: dict = {}   # key -> first time seen, not yet announced
        self._gone: dict = {}      # key -> first time missing, not yet resolved
        self._loaded: set | None = None
        self._peers: dict = {}     # remote IP -> last time seen connected (for "new machine connected" events)
        self._up: bool | None = None
        self._task_ts = None
        self._bench_at = None

    def _load(self):
        if not self.path:
            return []
        try:
            with open(self.path) as f:
                lines = f.readlines()[-self.keep:]
            return [json.loads(x) for x in lines if x.strip()]
        except (OSError, ValueError):
            return []

    def add(self, level, text, ts=None):
        ev = {"ts": ts or time.time(), "level": level, "text": text}
        self.events.append(ev)
        del self.events[:-self.keep]
        if self.path and self.write:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(ev) + "\n")
                userpaths.give_back(self.path)
                if os.path.getsize(self.path) > 400_000:  # keep the file small: rewrite with the recent tail
                    with open(self.path, "w") as f:
                        f.writelines(json.dumps(e) + "\n" for e in self.events)
            except OSError:
                pass

    def update(self, D, findings, now: float | None = None):
        now = time.time() if now is None else now
        ai = D.get("ai") or {}
        cur = {f["key"]: f for f in findings}
        ns = D.get("netsec") or {}
        names = {r["port"]: r["name"] or str(r["port"]) for r in ns.get("services") or [] if r["proto"] == "tcp"}
        peers = {}
        for c in ns.get("incoming") or []:
            p = peers.setdefault(c["ip"], {"from": c["from"], "you": c["you"], "ports": set()})
            p["ports"].add(c["port"])
        if not self._settled:
            # start-up: slower collectors are still arriving, so whatever shows up now was already open
            self._logged = dict(cur)
            self._peers.update({ip: now for ip in peers})
            if now - self._started >= SETTLE:
                self._settled = True
                bad = sum(f["level"] == BAD for f in findings)
                self.add(INFO, f"lmon started · health {score(findings)} · {len(findings)} open finding(s), {bad} critical", ts=now)
        else:
            for key, f in cur.items():
                self._gone.pop(key, None)
                if key not in self._logged:
                    first = self._pending.setdefault(key, now)
                    if now - first >= HOLD:
                        self.add(f["level"], f["title"], ts=first)
                        self._logged[key] = f
                        self._pending.pop(key)
            for key in [k for k in self._pending if k not in cur]:
                self._pending.pop(key)  # came and went within HOLD: a blip, not an event
            for key in [k for k in self._logged if k not in cur]:
                first = self._gone.setdefault(key, now)
                if now - first >= HOLD:
                    self.add("ok", "resolved: " + self._logged.pop(key)["title"], ts=first)
                    self._gone.pop(key)

        if self._settled:  # a machine that was not connected in the last 10 minutes
            for ip, p in peers.items():
                if now - self._peers.get(ip, 0) > 600:
                    to = ", ".join(f"{names.get(port, port)}:{port}" for port in sorted(p["ports"]))
                    who = {"lan": "LAN", "tailscale": "Tailscale", "vpn": "VPN", "docker": "container", "internet": "INTERNET"}.get(p["from"], p["from"])
                    self.add("bad" if p["from"] == "internet" else "net", f"{ip} ({who}{', you' if p['you'] else ''}) connected → {to}", ts=now)
            self._peers.update({ip: now for ip in peers})

        if ai:
            loaded = {m["name"]: m for m in ai.get("loaded") or []}
            if self._loaded is not None:
                for name in loaded.keys() - self._loaded:
                    m = loaded[name]
                    where = "100% GPU" if (m.get("gpu_pct") or 0) >= 99.5 else f"{100 - (m.get('gpu_pct') or 0):.0f}% CPU"
                    self.add("ai", f"model loaded: {name} ({m['size_gb']:.1f} GB, {where})")
                for name in self._loaded - loaded.keys():
                    self.add("ai", f"model unloaded: {name}")
            self._loaded = set(loaded)
            up = bool((D.get("ollama") or {}).get("running"))
            if self._up is not None and up != self._up:
                self.add("ok" if up else BAD, "Ollama is back up" if up else "Ollama stopped responding")
            self._up = up
            tasks = ((ai.get("requests") or {}).get("tokens") or {}).get("recent_tasks") or []
            if self._task_ts is None:
                self._task_ts = max((t["ts"] for t in tasks), default=0)
            new = [t for t in tasks if t["ts"] > self._task_ts]
            if new:
                out = sum(t.get("gen_tokens") or 0 for t in new)
                ms = sum(t.get("gen_ms") or 0 for t in new)
                self.add("ai", f"{len(new)} generation{'s' if len(new) > 1 else ''} finished · {out:,} tokens"
                         + (f" · {out / ms * 1000:.1f} tok/s" if ms else ""), ts=new[-1]["ts"])
                self._task_ts = new[-1]["ts"]
            runs = (ai.get("bench") or {}).get("runs") or []
            if runs and runs[0]["at"] != self._bench_at:
                if self._bench_at is not None or time.time() - runs[0]["at"] < 5:
                    r = runs[0]
                    self.add("ai", f"benchmark {r['model']}: {r['gen_tps'] or 0:.1f} tok/s, first token {r['ttft_s']:.2f}s", ts=r["at"])
                self._bench_at = runs[0]["at"]
        return self.events
