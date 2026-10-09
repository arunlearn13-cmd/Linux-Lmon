"""Metric collection for the Linux monitor (Linux only).

Two daemon threads feed an atomically swapped snapshot dict:
  fast (1s): CPU, memory, GPU, network, disk, temps, Ollama, /proc counters
  slow (5s): journal events, kernel health, systemd, process states (inotify: 15s)
Every collector fails independently: a missing tool yields null values plus an
entry in snapshot["errors"], never a broken dashboard.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import psutil

GB = 1024**3
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
if not OLLAMA_URL.startswith(("http://", "https://")):
    raise ValueError("OLLAMA_URL must be http(s)")

PHYS_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+)$")
SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "iso9660", "efivarfs"}
TCP_STATES = {"01": "est", "06": "tw", "08": "cw"}


def read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def read_int(path: str) -> int | None:
    txt = read(path)
    try:
        return int(txt) if txt else None
    except ValueError:
        return None


def run(cmd: list[str], timeout: float = 3, ok_codes: tuple = (0,)) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode in ok_codes else None


def num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pct(used, total) -> float | None:
    return used / total * 100 if used is not None and total else None


class Rates:
    """Per-second rate of monotonically increasing counters."""

    def __init__(self):
        self._prev: dict[str, tuple[float, float]] = {}

    def rate(self, key: str, value: float, now: float) -> float | None:
        prev, self._prev[key] = self._prev.get(key), (value, now)
        if not prev or now <= prev[1]:
            return None
        return max(value - prev[0], 0) / (now - prev[1])


def read_temps() -> dict:
    raw = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}

    def first(chip, pred=lambda label: True):
        for e in raw.get(chip, []):
            if pred(e.label or ""):
                return e.current
        return None

    cpu = first("coretemp", lambda l: l.startswith("Package")) or first("k10temp") or first("cpu_thermal")
    by_core = {}
    for e in raw.get("coretemp", []):
        m = re.fullmatch(r"Core (\d+)", e.label or "")
        if m:
            by_core[int(m.group(1))] = e.current
    return {"cpu": cpu, "by_core": by_core, "nvme": first("nvme"), "board": first("acpitz"), "ssd": first("drivetemp")}


def cpu_model() -> str | None:
    for line in (read("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def core_ids(n: int) -> list[int | None]:
    """Logical CPU -> physical core id, from sysfs topology (used to map coretemp sensors to threads)."""
    return [read_int(f"/sys/devices/system/cpu/cpu{i}/topology/core_id") for i in range(n)]


def phys_kind(name: str) -> str | None:
    """'ethernet' / 'wifi' for physical adapters, None for virtual ones (lo, docker, veth, bridges, tunnels...)."""
    base = f"/sys/class/net/{name}"
    if "/virtual/" in os.path.realpath(base):
        return None
    if os.path.isdir(f"{base}/wireless") or os.path.isdir(f"{base}/phy80211"):
        return "wifi"
    return "ethernet" if read_int(f"{base}/type") == 1 else None  # 1 = ARPHRD_ETHER


def inotify_usage() -> float | None:
    """Approximate inotify watch usage (%) over processes we may inspect."""
    limit = read_int("/proc/sys/fs/inotify/max_user_watches")
    used = 0
    for pid in filter(str.isdigit, os.listdir("/proc")):
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") != "anon_inode:inotify":
                        continue
                    info = read(f"/proc/{pid}/fdinfo/{fd}") or ""
                    used += sum(1 for line in info.splitlines() if line.startswith("inotify"))
                except OSError:
                    continue
        except OSError:
            continue
    return pct(used, limit)


class Sampler:
    def __init__(self, interval: float = 1.0, procs: bool = False, external_ai: bool = False):
        self.interval = interval
        self.procs = procs  # per-process table: only the terminal UI asks for it
        self.external_ai = external_ai  # GPU + Ollama come from monitor.ai instead (terminal UI)
        self.rates = Rates()
        self.snapshot: dict = {}
        self._slow: dict = {}
        self._inotify = None
        self._errors: dict[str, str] = {}
        self._kinds: dict[str, str | None] = {}
        self._core_ids = None
        self._core_hist: list[deque] = []
        self._hist: deque = deque(maxlen=400)  # wide enough to fill a full-width terminal graph
        self._mem_hist: deque = deque(maxlen=400)
        self._top_cache: tuple[float, dict | None] = (0.0, None)
        self._net_hist: deque = deque(maxlen=400)  # (rx_mbps, tx_mbps) summed over physical adapters
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._static = {"host": socket.gethostname(), "kernel": platform.release(), "cpu_model": cpu_model()}
        psutil.cpu_percent(percpu=True)  # prime the counters
        psutil.cpu_times_percent()
        psutil.cpu_times_percent(percpu=True)

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self.stop()  # idempotent: safe to start again after a stop (reload, tests)
        self._stop = threading.Event()
        time.sleep(0.5)  # give rate/CPU counters a real measurement window
        self.snapshot = self._collect(time.monotonic())
        self._threads = [threading.Thread(target=self._fast_loop, daemon=True),
                         threading.Thread(target=self._slow_loop, daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, wait: bool = True):
        self._stop.set()
        if wait:
            for t in self._threads:
                t.join(timeout=6)
        self._threads = []

    def _fast_loop(self):
        while not self._stop.is_set():
            t = time.monotonic()
            self.snapshot = self._collect(t)
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - t)))

    def _slow_loop(self):
        n = 0
        while not self._stop.is_set():
            if n % 3 == 0:
                self._inotify = self._g("inotify", inotify_usage)
            self._slow = {
                "journal": self._g("journal", self._journal),
                "kernel": self._g("kernel", self._kernel),
                "procs": self._g("procs", self._procs),
                "systemd": self._g("systemd", self._systemd),
            }
            n += 1
            self._stop.wait(5)

    def _g(self, name, fn):
        try:
            self._errors.pop(name, None)
            return fn()
        except Exception as e:  # noqa: BLE001 - isolate collectors from each other
            self._errors[name] = f"{type(e).__name__}: {e}"
            return None

    # ---- fast tier -------------------------------------------------------
    def _collect(self, now: float) -> dict:
        temps = self._g("temps", read_temps) or {}
        net = self._g("net", lambda: self._net(now))
        disk = self._g("disk", lambda: self._disk(now, temps))
        ks = self._g("proc_stat", lambda: self._proc_stat(now)) or {}
        slow = self._slow
        j = slow.get("journal") or {}
        gpu = None if self.external_ai else self._g("gpu", self._gpu)
        adv = {
            "events_per_min": j.get("per_min"),
            "ctx_per_s": ks.get("ctx"),
            "net_pps": (net["rx_pps"] + net["tx_pps"]) if net else None,
            "io_latency_ms": disk.get("latency_ms") if disk else None,
            "events": j.get("events", []),
            "pressure": {**(self._g("pressure", self._pressure) or {}), "inotify": slow.get("inotify", self._inotify)},
            "tcp": self._g("tcp", lambda: self._tcp(now)),
            "kernel": slow.get("kernel"),
            "proc": {"forks_per_s": ks.get("forks"), **(slow.get("procs") or {})},
            "systemd": slow.get("systemd"),
        }
        return {
            **self._static,
            "ts": time.time(),
            "uptime_s": int(time.time() - psutil.boot_time()),
            "sessions": self._g("sessions", lambda: len(psutil.users())),
            "cpu": self._g("cpu", lambda: self._cpu(now, temps)),
            "mem": self._g("mem", self._mem),
            "gpu": gpu,
            "net": net,
            "disk": disk,
            "temps": {"cpu": temps.get("cpu"), "gpu": gpu.get("temp") if gpu else None,
                      "nvme": temps.get("nvme"), "board": temps.get("board"), "ssd": temps.get("ssd")},
            "ollama": None if self.external_ai else self._g("ollama", self._ollama),
            "top": self._top_cached(now) if self.procs else None,
            "adv": adv,
            "errors": dict(list(self._errors.items())),
        }

    def _cpu(self, now, temps):
        per = psutil.cpu_percent(percpu=True)
        n = len(per)
        if self._core_ids is None or len(self._core_ids) != n:
            self._core_ids = core_ids(n)
            self._core_hist = [deque(maxlen=60) for _ in range(n)]
        by = temps.get("by_core") or {}
        try:
            fr = psutil.cpu_freq(percpu=True) or []
        except (OSError, NotImplementedError, AttributeError):
            fr = []
        has_f = len(fr) == n
        tt, tp = psutil.cpu_times_percent(), psutil.cpu_times_percent(percpu=True)
        g = lambda t, a: getattr(t, a, 0.0)  # noqa: E731 - fields differ per platform
        total = sum(per) / n
        self._hist.append(round(total))
        for h, v in zip(self._core_hist, per):
            h.append(round(v))
        uj = read_int("/sys/class/powercap/intel-rapl:0/energy_uj")  # often root-only
        r = self.rates.rate("rapl", uj, now) if uj is not None else None
        fmax = [f.max for f in fr if f.max] if has_f else []
        return {"pct": total, "per_core": per, "core_temps": [by.get(c) for c in self._core_ids],
                "freq_mhz": [f.current for f in fr] if has_f else [None] * n,
                "freq_max_mhz": max(fmax) if fmax else None,
                "times": [{"u": round(g(t, "user") + g(t, "nice"), 1), "s": round(g(t, "system"), 1),
                           "w": round(g(t, "iowait"), 1)} for t in tp],
                "user": g(tt, "user") + g(tt, "nice"), "system": g(tt, "system"), "iowait": g(tt, "iowait"),
                "irq": g(tt, "irq") + g(tt, "softirq"),
                "hist": list(self._hist), "core_hist": [list(h) for h in self._core_hist],
                "load": list(os.getloadavg()), "temp": temps.get("cpu"),
                "power_w": r / 1e6 if r is not None else None}

    def _mem(self):
        m, s = psutil.virtual_memory(), psutil.swap_memory()
        self._mem_hist.append(m.percent)
        return {"pct": m.percent, "hist": list(self._mem_hist), "used_gb": (m.total - m.available) / GB, "total_gb": m.total / GB,
                "avail_gb": m.available / GB, "cached_gb": (getattr(m, "cached", 0) + getattr(m, "buffers", 0)) / GB,
                "swap_used_gb": s.used / GB, "swap_total_gb": s.total / GB, "swap_pct": s.percent if s.total else None}

    @staticmethod
    def _gpu():
        if not shutil.which("nvidia-smi"):
            return None
        q = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
        out = run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"])
        if not out:
            return None
        name, util, used, total, temp, power = [x.strip() for x in out.splitlines()[0].split(",")]
        gb = lambda v: (num(v) / 1024) if num(v) is not None else None  # noqa: E731
        return {"name": name, "util": num(util), "vram_used_gb": gb(used), "vram_total_gb": gb(total),
                "temp": num(temp), "power_w": num(power)}

    def _net(self, now):
        stats, io = psutil.net_if_stats(), psutil.net_io_counters(pernic=True)
        addrs = psutil.net_if_addrs()
        ifaces, rx_pps, tx_pps, rx_bytes, tx_bytes = [], 0.0, 0.0, 0, 0
        for name, c in io.items():
            if name not in self._kinds:
                self._kinds[name] = phys_kind(name)
            kind = self._kinds[name]
            if kind is None:
                continue
            rb = self.rates.rate(f"{name}.rb", c.bytes_recv, now)
            tb = self.rates.rate(f"{name}.tb", c.bytes_sent, now)
            rp = self.rates.rate(f"{name}.rp", c.packets_recv, now)
            tp = self.rates.rate(f"{name}.tp", c.packets_sent, now)
            rx_bytes, tx_bytes = rx_bytes + c.bytes_recv, tx_bytes + c.bytes_sent
            ip = next((a.address for a in addrs.get(name, []) if a.family == socket.AF_INET), None)
            ifaces.append({"name": name, "kind": kind, "up": name in stats and stats[name].isup, "ip": ip,
                           "rx_mbps": rb * 8 / 1e6 if rb is not None else None,
                           "tx_mbps": tb * 8 / 1e6 if tb is not None else None})
            rx_pps, tx_pps = rx_pps + (rp or 0), tx_pps + (tp or 0)
        ifaces.sort(key=lambda i: (i["kind"] != "ethernet", i["name"]))
        rx = [i["rx_mbps"] for i in ifaces if i["rx_mbps"] is not None]
        tx = [i["tx_mbps"] for i in ifaces if i["tx_mbps"] is not None]
        if rx or tx:
            self._net_hist.append((sum(rx), sum(tx)))
        return {"ifaces": ifaces, "rx_pps": rx_pps, "tx_pps": tx_pps, "rx_total": rx_bytes, "tx_total": tx_bytes,
                "rx_hist": [r for r, _ in self._net_hist], "tx_hist": [t for _, t in self._net_hist]}

    def _disk(self, now, temps):
        io = psutil.disk_io_counters(perdisk=True) or {}
        disks = [d for d in io if PHYS_DISK.match(d)]
        tot = lambda attr: sum(getattr(io[d], attr) for d in disks)  # noqa: E731
        rb = self.rates.rate("d.rb", tot("read_bytes"), now)
        wb = self.rates.rate("d.wb", tot("write_bytes"), now)
        ops = self.rates.rate("d.ops", tot("read_count") + tot("write_count"), now)
        busy = self.rates.rate("d.ms", tot("read_time") + tot("write_time"), now)
        latency = None if not ops or busy is None else busy / ops  # no I/O => undefined, not 0
        mounts, seen = [], set()
        for p in psutil.disk_partitions(all=False):
            if (p.device in seen or p.fstype in SKIP_FS or p.device.startswith("/dev/loop")
                    or p.mountpoint.startswith(("/snap", "/var/lib/snapd"))):
                continue
            seen.add(p.device)
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            if not u.total:
                continue
            mounts.append({"path": p.mountpoint, "pct": u.percent, "used_gb": u.used / GB, "total_gb": u.total / GB})
        return {"name": ", ".join(disks) or None, "read_mbs": rb / 1e6 if rb is not None else None,
                "write_mbs": wb / 1e6 if wb is not None else None,
                "iops": ops, "latency_ms": latency, "temp": temps.get("nvme"), "mounts": mounts[:4]}

    @staticmethod
    def _ollama():
        try:
            with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=0.8) as r:  # noqa: S310 - scheme validated
                data = json.load(r)
        except (OSError, ValueError):
            return {"running": False, "url": OLLAMA_URL, "models": []}
        models = [{"name": m.get("name"), "size_gb": m.get("size", 0) / GB, "vram_gb": m.get("size_vram", 0) / GB,
                   "context": m.get("context_length"), "expires_at": m.get("expires_at")}
                  for m in data.get("models", [])]
        # Ollama exposes no global tokens/sec; it is only reported per request.
        return {"running": True, "url": OLLAMA_URL, "models": models}

    def _top_cached(self, now):
        """The process scan is the costliest collector, so refresh it every 2s (btop's default)."""
        if now - self._top_cache[0] >= 2 or self._top_cache[1] is None:
            self._top_cache = (now, self._g("top", self._top))
        return self._top_cache[1]

    @staticmethod
    def _top() -> dict:
        """Every process (CPU % is per core, like top: can exceed 100). Fields we may not read are None/0."""
        rows = []
        for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent", "memory_info",
                                      "num_threads", "cmdline"]):
            i = p.info
            if i["pid"] == 0:
                continue
            rows.append({"pid": i["pid"], "name": i["name"] or "", "user": i["username"] or "", "cpu": i["cpu_percent"] or 0.0,
                         "mem": i["memory_percent"] or 0.0, "rss": i["memory_info"].rss if i["memory_info"] else None,
                         "threads": i["num_threads"], "cmd": " ".join(i["cmdline"] or []) or f"[{i['name']}]"})
        return {"count": len(rows), "procs": rows}

    def _proc_stat(self, now):
        vals = {}
        for line in (read("/proc/stat") or "").splitlines():
            k, _, rest = line.partition(" ")
            if k in ("ctxt", "processes"):
                vals[k] = int(rest)
        return {"ctx": self.rates.rate("ctxt", vals["ctxt"], now),
                "forks": self.rates.rate("forks", vals["processes"], now)}

    @staticmethod
    def _pressure():
        fd = (read("/proc/sys/fs/file-nr") or "").split()
        fmax = int(fd[2]) if len(fd) == 3 else None
        threads = int((read("/proc/loadavg") or "0 0 0 0/0").split()[3].split("/")[1])
        return {"fd": pct(int(fd[0]), fmax) if fmax else None,
                "pid": pct(threads, read_int("/proc/sys/kernel/pid_max"))}

    def _tcp(self, now):
        counts = {"est": 0, "tw": 0, "cw": 0}
        for f in ("/proc/net/tcp", "/proc/net/tcp6"):
            for line in (read(f) or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) > 3 and parts[3] in TCP_STATES:
                    counts[TCP_STATES[parts[3]]] += 1
        rows = [l.split() for l in (read("/proc/net/snmp") or "").splitlines() if l.startswith("Tcp:")]
        retr = None
        if len(rows) == 2:
            retr = self.rates.rate("retrans", int(dict(zip(rows[0][1:], rows[1][1:]))["RetransSegs"]), now)
        return {**counts, "retrans_per_s": retr}

    # ---- slow tier -------------------------------------------------------
    @staticmethod
    def _journal():
        out = run(["journalctl", "-p", "warning", "-n", "80", "-o", "json", "--no-pager"])
        if out is None:
            raise RuntimeError("journalctl unavailable (add the service user to group systemd-journal)")
        events, seen = [], {}
        for line in reversed(out.splitlines()):  # newest first
            try:
                e = json.loads(line)
                ts = int(e["__REALTIME_TIMESTAMP"]) / 1e6
            except (ValueError, KeyError):
                continue
            msg = e.get("MESSAGE")
            msg = msg[:200] if isinstance(msg, str) else "<binary>"
            src = e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or ""
            if (src, msg) in seen:
                seen[(src, msg)]["count"] += 1
                continue
            ev = {"t": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)), "msg": msg, "src": src, "count": 1,
                  "level": "bad" if int(e.get("PRIORITY", 4)) <= 3 else "warn"}
            seen[(src, msg)] = ev
            events.append(ev)
        recent = run(["journalctl", "-p", "warning", "--since", "1 minute ago", "-o", "cat", "--no-pager"])
        per_min = len([l for l in recent.splitlines() if l.strip()]) if recent is not None else None
        return {"events": events[:20], "per_min": per_min}

    @staticmethod
    def _kernel():
        out = run(["journalctl", "-k", "--since", "1 hour ago", "-p", "warning", "-o", "cat", "--no-pager"])
        if out is None:
            raise RuntimeError("kernel journal unavailable")
        lines = [l.lower() for l in out.splitlines()]
        return {"warnings": len(lines),
                "oom": sum(bool(re.search(r"out of memory|oom-kill", l)) for l in lines),
                "rcu_stalls": sum("rcu" in l and "stall" in l for l in lines),
                "mce": sum("mce" in l or "machine check" in l for l in lines)}

    @staticmethod
    def _procs():
        d = z = 0
        for p in psutil.process_iter(["status"]):
            s = p.info["status"]
            d += s == psutil.STATUS_DISK_SLEEP
            z += s == psutil.STATUS_ZOMBIE
        return {"dstate": d, "zombies": z}

    @staticmethod
    def _systemd():
        count = lambda out: None if out is None else len([l for l in out.splitlines() if l.strip()])  # noqa: E731
        failed = run(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])
        jobs = run(["systemctl", "list-jobs", "--no-legend", "--no-pager"])
        restarts = run(["journalctl", "--since", "1 minute ago", "-o", "cat", "--no-pager",
                        "--grep", "Scheduled restart job"], ok_codes=(0, 1))
        stuck = None if jobs is None else len([l for l in jobs.splitlines() if l.split()[:1] and l.split()[0].isdigit()])
        names = [l.split()[0] for l in (failed or "").splitlines() if l.strip()][:5]
        return {"failed": count(failed), "failed_names": names, "stuck": stuck, "restarts_per_min": count(restarts)}
