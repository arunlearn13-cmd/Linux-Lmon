"""AI workload collectors for the terminal UI: NVIDIA GPU detail, Ollama server / config / models /
request log, and an on-demand token benchmark.

Everything shown is read from a real source:
  GPU        nvidia-smi (1s), per-process VRAM via --query-compute-apps (3s)
  Ollama     /api/ps (3s), /api/version and /api/tags (30s)
  config     `systemctl show <unit>` Environment (30s) - only OLLAMA_* / CUDA_* variables
  requests   the server's own [GIN] access log in the journal, last hour (read incrementally every 2s)
  tokens     the model runner's `slot print_timing` lines in the same log: prompt / generated tokens and
             tokens/s for every finished request, plus in-flight progress (n_gen, tg_3s) while generating
  benchmark  on demand only: one short /api/generate on an already-loaded model (Ollama's eval counters)
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import threading
import time
import urllib.request
from collections import Counter, deque

import psutil

from .sampler import GB, OLLAMA_URL, num, run

UNIT = "ollama"
POLL_PATHS = {"/api/ps", "/api/tags", "/api/version", "/", "/api/show", "/v1/models", "/favicon.ico"}
INFER_PATHS = {"/api/generate", "/api/chat", "/api/embed", "/api/embeddings"}
GIN = re.compile(r"\[GIN\] (\d{4}/\d\d/\d\d) - (\d\d:\d\d:\d\d) \| (\d{3}) \|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\w+)\s+\"([^\"]*)\"")
TIMING = re.compile(r"slot print_timing: id\s+(\d+) \| task (\d+) \|\s*(prompt eval|eval|total) time =\s*([\d.]+) ms /\s*(\d+) tokens")
PROGRESS = re.compile(r"slot print_timing: id\s+(\d+) \| task (\d+) \| n_gen =\s*(\d+), tg =\s*([\d.]+) t/s(?:, tg_3s =\s*([\d.]+) t/s)?")
RELEASE = re.compile(r"slot\s+release: id\s+(\d+) \| task (\d+) \| stop processing: n_tokens = (\d+), truncated = (\d+)")
UNIX_LINE = re.compile(r"^(\d+\.\d+) \S+ \S+?: (.*)$")
GO_DUR = re.compile(r"(\d+(?:\.\d+)?)(h|ms|µs|us|ns|m|s)")
UNIT_S = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}
BENCH_PROMPT = "Explain in one paragraph how a CPU cache works."
GPU_Q = ("name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
         "clocks.sm,clocks.mem,clocks.max.sm,fan.speed,pstate,pcie.link.gen.current,pcie.link.width.current,driver_version")


def go_duration(s: str) -> float | None:
    """Gin latency ('22.9µs', '36.6ms', '4m30s', '1h2m3.5s') -> seconds."""
    parts = GO_DUR.findall(s)
    return sum(float(v) * UNIT_S[u] for v, u in parts) if parts else None


def is_inference(path: str) -> bool:
    p = path.split("?")[0]
    return p in INFER_PATHS or (p.startswith("/v1/") and p != "/v1/models")


class OllamaLog:
    """Rolling one-hour view of Ollama's log: access lines ([GIN]) and runner token timings.

    Lines are `journalctl -o short-unix` ("<unix ts> host unit[pid]: msg"); bare messages are taken as "now".
    Finished runner tasks are paired FIFO with the next successful inference request that could contain them,
    which is exact for one request at a time and approximate when requests overlap (NUM_PARALLEL > 1).
    """

    def __init__(self, window: float = 3600):
        self.window = window
        self.reqs: deque = deque()
        self.polls: deque = deque()
        self.tasks: deque = deque()
        self._open: dict = {}
        self._done: dict = {}
        self._unclaimed: deque = deque()
        self.live: dict = {}

    def feed(self, lines, now: float | None = None):
        now = time.time() if now is None else now
        for line in lines:
            m = UNIX_LINE.match(line)
            ts, msg = (float(m[1]), m[2]) if m else (now, line)
            if "[GIN]" in msg:
                self._gin(ts, msg)
            elif "print_timing" in msg:
                self._timing(ts, msg)
            elif "release" in msg:
                r = RELEASE.search(msg)
                if r and (r[1], r[2]) in self._done:
                    self._done.pop((r[1], r[2]))["ctx"] = int(r[3])
        return self

    def _gin(self, ts, msg):
        m = GIN.search(msg)
        if not m:
            return
        _, t, status, lat, client, method, path = m.groups()
        if path.split("?")[0] in POLL_PATHS and status.startswith("2"):
            self.polls.append(ts)
            return
        q = {"ts": ts, "t": t, "status": int(status), "lat": go_duration(lat), "client": client, "method": method,
             "path": path, "infer": is_inference(path), "task": None}
        if q["infer"] and q["status"] < 400:
            start = ts - (q["lat"] or 0) - 1
            while self._unclaimed and self._unclaimed[0]["ts"] < start:
                self._unclaimed.popleft()  # finished before this request began: belongs to nothing we saw
            if self._unclaimed and self._unclaimed[0]["ts"] <= ts + 1:
                q["task"] = self._unclaimed.popleft()
        self.reqs.append(q)

    def _timing(self, ts, msg):
        p = PROGRESS.search(msg)
        if p:
            self.live[(p[1], p[2])] = {"slot": int(p[1]), "n_gen": int(p[3]), "tps": float(p[5] or p[4]), "ts": ts}
            return
        m = TIMING.search(msg)
        if not m:
            return
        key = (m[1], m[2])
        cur = self._open.setdefault(key, {"slot": int(m[1])})
        kind, ms, toks = m[3], float(m[4]), int(m[5])
        if kind == "prompt eval":
            cur.update(prompt_tokens=toks, prompt_ms=ms)
        elif kind == "eval":
            cur.update(gen_tokens=toks, gen_ms=ms)
        else:  # total: the task is finished
            task = {**self._open.pop(key), "total_ms": ms, "ts": ts, "ctx": None}
            pt, pm, gt, gm = (task.get(x) for x in ("prompt_tokens", "prompt_ms", "gen_tokens", "gen_ms"))
            task["prompt_tps"] = pt / pm * 1000 if pt and pm else None
            task["gen_tps"] = gt / gm * 1000 if gt and gm else None
            self.tasks.append(task)
            self._unclaimed.append(task)
            self._done[key] = task
            self.live.pop(key, None)

    def summary(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        cut = now - self.window
        for dq in (self.reqs, self.tasks, self._unclaimed):
            while dq and dq[0]["ts"] < cut:
                dq.popleft()
        while self.polls and self.polls[0] < cut:
            self.polls.popleft()
        for key in [k for k, v in self.live.items() if now - v["ts"] > 10]:  # progress lines stop when a task ends
            self.live.pop(key)
        reqs, tasks = list(self.reqs), list(self.tasks)
        infer = [r for r in reqs if r["infer"]]
        ok = sorted(r["lat"] for r in infer if r["status"] < 400 and r["lat"] is not None)
        gen = [t for t in tasks if t.get("gen_ms")]
        prm = [t for t in tasks if t.get("prompt_ms")]
        per_min = [0] * 60
        for t in tasks:
            i = int((t["ts"] - cut) // 60)
            if 0 <= i < 60:
                per_min[i] += t.get("gen_tokens") or 0
        return {
            "requests": len(reqs), "inference": len(infer), "errors": sum(r["status"] >= 400 for r in reqs), "polls": len(self.polls),
            "avg_s": sum(ok) / len(ok) if ok else None, "p95_s": ok[min(len(ok) - 1, int(len(ok) * .95))] if ok else None,
            "max_s": ok[-1] if ok else None,
            "by_path": Counter(r["path"].split("?")[0] for r in reqs).most_common(6),
            "by_client": Counter(r["client"] for r in reqs).most_common(4),
            "recent": reqs[::-1][:80],
            "tokens": {
                "tasks": len(tasks), "in": sum(t.get("prompt_tokens") or 0 for t in tasks), "out": sum(t.get("gen_tokens") or 0 for t in tasks),
                "gen_tps": sum(t["gen_tokens"] for t in gen) / (sum(t["gen_ms"] for t in gen) / 1000) if gen else None,
                "prompt_tps": sum(t["prompt_tokens"] for t in prm) / (sum(t["prompt_ms"] for t in prm) / 1000) if prm else None,
                "peak_tps": max((t["gen_tps"] for t in gen if t["gen_tps"]), default=None),
                "last": tasks[-1] if tasks else None, "per_min": per_min,
                "tps_hist": [t["gen_tps"] for t in gen[-200:] if t["gen_tps"]],
                "live": sorted(self.live.values(), key=lambda v: v["slot"]),
                "recent_tasks": tasks[-50:],
            },
        }


def parse_requests(text: str) -> dict:
    """Summarise a block of Ollama log text (tests, one-off use)."""
    return OllamaLog().feed(text.splitlines()).summary()


def parse_env(out: str) -> dict:
    env = {}
    for line in out.splitlines():
        if line.startswith("Environment="):
            for item in shlex.split(line[len("Environment="):]):
                key, _, val = item.partition("=")
                if key.startswith(("OLLAMA_", "CUDA_", "GPU_", "HIP_")):
                    env[key] = val
    return env


def _get(path: str, timeout: float = 2.0):
    with urllib.request.urlopen(f"{OLLAMA_URL}{path}", timeout=timeout) as r:  # noqa: S310 - scheme validated in sampler
        return json.load(r)


class AICollector:
    def __init__(self, interval: float = 1.0, unit: str = UNIT):
        self.interval, self.unit = interval, unit
        self.snapshot: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._at: dict[str, float] = {}
        self._cache: dict = {}
        self._errors: dict[str, str] = {}
        self._procs: dict[int, psutil.Process] = {}
        self._util = deque(maxlen=400)
        self._vram = deque(maxlen=400)
        self._log = OllamaLog()
        self._cursor: str | None = None
        self._bench = {"running": False, "model": None, "runs": [], "error": None, "started": None}
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self._stop.clear()
        self.snapshot = self._collect()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            t = time.monotonic()
            self.snapshot = self._collect()
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - t)))

    def _every(self, name, secs, fn):
        """Run fn at most every `secs`, keeping the last result (and error) in between."""
        now = time.monotonic()
        if name not in self._at or now - self._at[name] >= secs:
            self._at[name] = now
            try:
                self._cache[name] = fn()
                self._errors.pop(name, None)
            except Exception as e:  # noqa: BLE001 - one failing source must not blank the page
                self._cache[name] = None
                self._errors[name] = f"{type(e).__name__}: {e}"
        return self._cache.get(name)

    # ---- collection ------------------------------------------------------
    def _collect(self) -> dict:
        gpu = self._every("gpu", 0, self._gpu)
        if gpu:
            self._util.append(gpu["util"] or 0)
            self._vram.append(gpu["vram_pct"] or 0)
            gpu = {**gpu, "util_hist": list(self._util), "vram_hist": list(self._vram),
                   "apps": self._every("apps", 3, self._apps) or []}
        ps = self._every("ps", 3, lambda: _get("/api/ps", 0.8))
        running = ps is not None
        tags = self._every("tags", 30, lambda: _get("/api/tags", 5)) if running else None
        version = self._every("version", 30, lambda: _get("/api/version", 1)) if running else None
        cfg = self._every("cfg", 30, self._config)
        loaded = [self._loaded(m) for m in (ps or {}).get("models", [])]
        installed = sorted(({"name": m.get("name"), "size_gb": m.get("size", 0) / GB, **self._details(m)}
                            for m in (tags or {}).get("models", [])), key=lambda m: m["name"] or "")
        with self._lock:
            bench = {**self._bench, "runs": list(self._bench["runs"])}
        return {
            "ts": time.time(), "gpu": gpu,
            "ollama": {"running": running, "url": OLLAMA_URL, "version": (version or {}).get("version"),
                       "models": [{"name": m["name"], "size_gb": m["size_gb"], "vram_gb": m["vram_gb"], "context": m["context"]}
                                  for m in loaded]},
            "loaded": loaded, "installed": installed,
            "config": cfg, "server": self._server(cfg),
            "requests": self._every("requests", 2, self._requests),
            "bench": bench, "errors": dict(self._errors),
        }

    @staticmethod
    def _details(m: dict) -> dict:
        d = m.get("details") or {}
        return {"params": d.get("parameter_size"), "quant": d.get("quantization_level"), "family": d.get("family"),
                "ctx_max": d.get("context_length")}

    def _loaded(self, m: dict) -> dict:
        size, vram = m.get("size", 0), m.get("size_vram", 0)
        exp = m.get("expires_at")
        try:  # RFC3339 with nanoseconds and offset, e.g. 2026-01-15T10:30:00.123456789+00:00
            base = re.sub(r"(\.\d{6})\d+", r"\1", exp)
            from datetime import datetime
            left = datetime.fromisoformat(base.replace("Z", "+00:00")).timestamp() - time.time()
        except (TypeError, ValueError):
            left = None
        return {"name": m.get("name"), "size_gb": size / GB, "vram_gb": vram / GB, "gpu_pct": vram / size * 100 if size else None,
                "context": m.get("context_length"), "expires_in": left, **self._details(m)}

    @staticmethod
    def _gpu():
        if not shutil.which("nvidia-smi"):
            return None
        out = run(["nvidia-smi", f"--query-gpu={GPU_Q}", "--format=csv,noheader,nounits"])
        if not out:
            return None
        f = [x.strip() for x in out.splitlines()[0].split(",")]
        name, util, mutil, used, total, temp, pw, plim, sm, mem, smmax, fan, pstate, gen, width, drv = f
        used_gb = num(used) / 1024 if num(used) is not None else None
        total_gb = num(total) / 1024 if num(total) is not None else None
        return {"name": name, "util": num(util), "mem_util": num(mutil), "vram_used_gb": used_gb, "vram_total_gb": total_gb,
                "vram_pct": used_gb / total_gb * 100 if used_gb is not None and total_gb else None,
                "temp": num(temp), "power_w": num(pw), "power_limit_w": num(plim), "sm_mhz": num(sm), "mem_mhz": num(mem),
                "sm_max_mhz": num(smmax), "fan": num(fan), "pstate": pstate, "pcie": f"gen{gen} x{width}" if num(gen) else None,
                "driver": drv}

    @staticmethod
    def _apps():
        out = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
        apps = []
        for line in (out or "").splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) == 3 and parts[0].isdigit():
                pid = int(parts[0])
                try:
                    name = psutil.Process(pid).name()
                except psutil.Error:
                    name = parts[1].rsplit("/", 1)[-1]
                apps.append({"pid": pid, "name": name, "vram_gb": (num(parts[2]) or 0) / 1024})
        return apps

    def _config(self):
        out = run(["systemctl", "show", self.unit, "-p", "Environment", "-p", "MainPID", "-p", "ActiveState", "-p", "LoadState"])
        if not out:
            return None
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("Environment="))
        if props.get("LoadState") != "loaded":
            return None
        return {"unit": self.unit, "state": props.get("ActiveState"), "pid": int(props.get("MainPID") or 0) or None,
                "env": parse_env(out)}

    def _server(self, cfg):
        """CPU/RAM of the Ollama server and its model runners (children)."""
        pid = (cfg or {}).get("pid")
        if not pid:
            pid = next((p.pid for p in psutil.process_iter(["name", "cmdline"])
                        if p.info["name"] == "ollama" and "serve" in (p.info["cmdline"] or [])), None)
        if not pid:
            return None
        try:
            main = self._procs.setdefault(pid, psutil.Process(pid))
            procs = [main] + main.children(recursive=True)
        except psutil.Error:
            self._procs.pop(pid, None)
            return None
        cpu = rss = 0.0
        runners = 0
        for p in procs:
            p = self._procs.setdefault(p.pid, p)
            try:
                cpu += p.cpu_percent()
                rss += p.memory_info().rss
                runners += p is not main
            except psutil.Error:
                self._procs.pop(p.pid, None)
        alive = {p.pid for p in procs}
        for gone in set(self._procs) - alive:
            self._procs.pop(gone, None)
        return {"pid": pid, "cpu": cpu, "rss_gb": rss / GB, "runners": runners, "uptime_s": time.time() - main.create_time()}

    def _requests(self):
        """Read only new journal lines since the last call (cursor), first call back-fills one hour."""
        cmd = ["journalctl", "-u", self.unit, "-o", "short-unix", "--no-pager", "-q", "--show-cursor"]
        cmd += ["--after-cursor", self._cursor] if self._cursor else ["--since", "1 hour ago"]
        out = run(cmd, timeout=8, ok_codes=(0, 1))
        if out is None:
            raise RuntimeError(f"journal for {self.unit} not readable (systemd-journal group)")
        lines = out.splitlines()
        if lines and lines[-1].startswith("-- cursor: "):
            self._cursor = lines.pop()[len("-- cursor: "):]
        return self._log.feed(lines).summary()

    # ---- token benchmark ---------------------------------------------------
    def start_bench(self) -> str:
        """Measure tokens/s on the first loaded model. Never loads a model on its own."""
        with self._lock:
            if self._bench["running"]:
                return "benchmark already running"
            loaded = self.snapshot.get("loaded") or []
            gen = [m for m in loaded if (m.get("family") or "") not in ("bert", "nomic-bert")]
            if not gen:
                return "no generation model loaded - load one in Ollama first (lmon never loads models)"
            model = gen[0]["name"]
            self._bench.update(running=True, model=model, error=None, started=time.time())
        threading.Thread(target=self._run_bench, args=(model,), daemon=True).start()
        return f"measuring tokens/s on {model}..."

    def _run_bench(self, model):
        body = json.dumps({"model": model, "prompt": BENCH_PROMPT, "stream": False,
                           "options": {"num_predict": 128, "temperature": 0}}).encode()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310 - scheme validated in sampler
                d = json.load(r)
            ns = 1e9
            res = {"model": model, "at": time.time(),
                   "gen_tokens": d.get("eval_count"), "prompt_tokens": d.get("prompt_eval_count"),
                   "gen_tps": d["eval_count"] / (d["eval_duration"] / ns) if d.get("eval_duration") else None,
                   "prompt_tps": d["prompt_eval_count"] / (d["prompt_eval_duration"] / ns) if d.get("prompt_eval_duration") else None,
                   "load_s": (d.get("load_duration") or 0) / ns, "total_s": (d.get("total_duration") or 0) / ns,
                   "ttft_s": ((d.get("load_duration") or 0) + (d.get("prompt_eval_duration") or 0)) / ns}
            with self._lock:
                self._bench["runs"] = ([res] + self._bench["runs"])[:6]
        except Exception as e:  # noqa: BLE001 - report any failure in the panel
            with self._lock:
                self._bench["error"] = f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                self._bench["running"] = False
