"""Demo mode: a made-up machine for screenshots, docs and trying lmon out.

`lmon --demo` renders every page from this module instead of the collectors, so nothing about the computer it
runs on is read or shown - no hostname, user, addresses, processes or logs. Values move over time so the
graphs look alive. Remote machines are shown by made-up names, never by address, and the header carries a
DEMO DATA badge so a screenshot cannot be mistaken for a real system.
"""
from __future__ import annotations

import math
import random
import time

GB = 1024 ** 3
CORES = 16


def _wave(t, period, lo, hi, phase=0.0):
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(2 * math.pi * (t / period) + phase))


def _series(t, n, period, lo, hi, phase=0.0, jitter=0.0, seed=0):
    rnd = random.Random(seed)
    return [max(0.0, _wave(t - (n - i), period, lo, hi, phase) + rnd.uniform(-jitter, jitter)) for i in range(n)]


def _procs(t):
    rows = [("postgres", "postgres", 24, 1.8, 180, "postgres: app appdb [local] SELECT"),
            ("python3", "app", 3, 22.0, 410, "python3 -m uvicorn api.main:app --workers 4"),
            ("node", "app", 2, 14.0, 520, "node /srv/web/server.js"),
            ("ollama", "ollama", 2, 6.0, 900, "ollama serve"),
            ("llama-server", "ollama", 1, 3.0, 5200, "llama-server --model qwen3-8b-q4_k_m.gguf"),
            ("redis-server", "redis", 1, 1.2, 64, "redis-server *:6379"),
            ("nginx", "www-data", 5, 0.4, 12, "nginx: worker process"),
            ("dockerd", "root", 1, 0.8, 95, "/usr/bin/dockerd -H fd://"),
            ("sshd", "root", 3, 0.1, 9, "sshd: demo@pts/0"),
            ("systemd-journald", "root", 1, 0.3, 40, "/usr/lib/systemd/systemd-journald"),
            ("lmon", "demo", 1, 2.5, 48, "lmon --demo")]
    out, pid = [], 1200
    rnd = random.Random(int(t // 2))
    for name, user, count, cpu, rss_mb, cmd in rows:
        for i in range(count):
            pid += rnd.randint(3, 40)
            c = max(0.0, cpu * (1.6 if i == 0 else 0.5) * rnd.uniform(0.6, 1.4))
            out.append({"pid": pid, "name": name, "user": user, "cpu": c, "mem": rss_mb / 320, "rss": rss_mb * 1024 * 1024 * (1 if i == 0 else 0.6),
                        "threads": 4 if name in ("python3", "node", "ollama") else 1, "cmd": cmd})
    return out


def _service(port, name, what, open_to, via="", conns=None, sure=True):
    conns = conns or {}
    return {"proto": "tcp", "port": port, "name": name, "sure": sure, "via": via, "registry": None, "what": what,
            "open_to": open_to, "binds": [], "firewall": "none", "conns": conns, "peers": [], "nconn": sum(conns.values())}


def snapshot(now: float | None = None) -> dict:
    t = time.time() if now is None else now
    per_core = [_wave(t + i * 7, 9 + i % 5, 4, 55 if i % 4 else 85) for i in range(CORES)]
    pct = sum(per_core) / CORES
    hist = _series(t, 400, 90, 15, 60, jitter=8, seed=1)
    gpu_hist = _series(t, 400, 45, 0, 92, jitter=6, seed=2)
    rx = _series(t, 400, 60, 2, 45, jitter=5, seed=3)
    tx = _series(t, 400, 75, 1, 18, jitter=3, seed=4)
    per_min = [int(max(0, _wave(i * 60, 900, -800, 5200) + random.Random(i).uniform(0, 600))) for i in range(60)]
    gen = _wave(t, 20, 0, 1)
    procs = _procs(t)
    base = t - 3600

    def req(ago, status, lat, client, method, path, toks=None):
        return {"ts": t - ago, "t": time.strftime("%H:%M:%S", time.localtime(t - ago)), "status": status, "lat": lat,
                "client": client, "method": method, "path": path, "infer": path.startswith(("/api/chat", "/api/generate", "/v1/")),
                "task": {"gen_tokens": toks[0], "gen_tps": toks[1]} if toks else None}

    recent = [req(40, 200, 6.2, "chat-ui", "POST", "/api/chat", (812, 131.0)),
              req(95, 200, 2.1, "notebook", "POST", "/api/generate", (240, 128.6)),
              req(130, 200, 0.4, "rag-worker", "POST", "/api/embed"),
              req(600, 404, 0.002, "cron-bot", "POST", "/api/chat"),
              req(700, 200, 11.8, "chat-ui", "POST", "/api/chat", (1530, 129.4)),
              req(1200, 404, 0.002, "cron-bot", "POST", "/api/chat"),
              req(1800, 404, 0.002, "cron-bot", "POST", "/api/chat"),
              req(2400, 200, 3.3, "notebook", "POST", "/v1/chat/completions", (402, 133.1))]
    tasks = [{"ts": t - 40, "gen_tokens": 812, "gen_ms": 6200, "prompt_tokens": 64, "prompt_ms": 70, "gen_tps": 131.0,
              "prompt_tps": 914.0, "total_ms": 6270, "ctx": 1840, "slot": 0}]
    loaded = [{"name": "qwen3:8b", "size_gb": 6.2, "vram_gb": 6.2, "gpu_pct": 100.0, "context": 8192, "expires_in": 1500 - (t % 1500),
               "params": "8.2B", "quant": "Q4_K_M", "family": "qwen3", "ctx_max": 40960},
              {"name": "llama3.1:70b", "size_gb": 42.0, "vram_gb": 11.0, "gpu_pct": 26.2, "context": 4096, "expires_in": 620,
               "params": "70.6B", "quant": "Q4_K_M", "family": "llama", "ctx_max": 131072}]
    installed = [{"name": n, "size_gb": s, "params": p, "quant": q, "family": f, "ctx_max": c} for n, s, p, q, f, c in (
        ("qwen3:8b", 5.2, "8.2B", "Q4_K_M", "qwen3", 40960), ("llama3.1:70b", 42.5, "70.6B", "Q4_K_M", "llama", 131072),
        ("nomic-embed-text:latest", 0.3, "137M", "F16", "nomic-bert", 2048), ("qwen2.5-coder:7b", 4.7, "7.6B", "Q4_K_M", "qwen2", 32768),
        ("gemma3:12b", 8.1, "12.2B", "Q4_K_M", "gemma3", 131072), ("mistral:7b", 4.4, "7.2B", "Q4_0", "llama", 32768))]
    gpu = {"name": "Demo GPU 24GB", "util": gpu_hist[-1], "mem_util": 30.0, "vram_used_gb": 17.2, "vram_total_gb": 24.0, "vram_pct": 71.7,
           "temp": _wave(t, 120, 58, 74), "power_w": _wave(t, 45, 80, 260), "power_limit_w": 300.0, "sm_mhz": 2100.0, "mem_mhz": 9500.0,
           "sm_max_mhz": 2600.0, "fan": 42.0, "pstate": "P2", "pcie": "gen4 x16", "driver": "demo", "util_hist": gpu_hist,
           "vram_hist": [71.7] * 400, "apps": [{"pid": 4242, "name": "llama-server", "vram_gb": 17.0}]}
    ai = {"ts": t, "gpu": gpu,
          "ollama": {"running": True, "url": "http://localhost:11434", "version": "demo",
                     "models": [{"name": m["name"], "size_gb": m["size_gb"], "vram_gb": m["vram_gb"], "context": m["context"]} for m in loaded]},
          "loaded": loaded, "installed": installed,
          "config": {"unit": "ollama", "state": "active", "pid": 4100,
                     "env": {"OLLAMA_HOST": "localhost:11434", "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
                             "OLLAMA_NUM_PARALLEL": "2", "OLLAMA_KEEP_ALIVE": "30m"}},
          "server": {"pid": 4100, "cpu": 4.0, "rss_gb": 0.9, "runners": 2, "uptime_s": 86400 * 3 + 7200},
          "requests": {"requests": 64, "inference": 58, "errors": 6, "polls": 1800, "avg_s": 4.6, "p95_s": 12.4, "max_s": 18.0,
                       "by_path": [("/api/chat", 40), ("/api/generate", 12), ("/api/embed", 8), ("/v1/chat/completions", 4)],
                       "by_client": [("chat-ui", 31), ("notebook", 17), ("rag-worker", 10), ("cron-bot", 6)], "recent": recent,
                       "tokens": {"tasks": 52, "in": 18400, "out": 61200, "gen_tps": 130.2, "prompt_tps": 905.0, "peak_tps": 141.8,
                                  "last": tasks[-1], "per_min": per_min, "tps_hist": [128 + 6 * math.sin(i) for i in range(60)],
                                  "live": [{"slot": 1, "n_gen": int(gen * 600), "tps": 129.0 + 4 * gen, "ts": t}] if gen > 0.3 else [],
                                  "recent_tasks": tasks}},
          "bench": {"running": False, "model": None, "error": None, "started": None,
                    "runs": [{"model": "qwen3:8b", "at": t - 900, "gen_tps": 132.4, "prompt_tps": 1210.0, "gen_tokens": 128,
                              "prompt_tokens": 18, "load_s": 0.02, "total_s": 1.1, "ttft_s": 0.04}]},
          "errors": {}}
    services = [_service(6379, "cache-redis", "cache / key-value", "everyone", "docker", {"docker": 2}),
                _service(5432, "app-postgres", "database", "everyone", "docker"),
                _service(5050, "pgadmin", "admin web UI", "everyone", "docker"),
                _service(22, "ssh", "remote login", "everyone", "systemd", {"lan": 1}),
                _service(443, "nginx", "web server", "everyone", "", {"lan": 3, "internet": 1}),
                _service(80, "nginx", "web server", "everyone"),
                _service(8000, "uvicorn", "app server", "LAN only", "", {"lan": 2}),
                _service(11434, "ollama", "AI models", "this host", "", {}),
                _service(9100, "node_exporter", "metrics", "LAN only", "", {"lan": 1}, sure=False),
                _service(631, "cupsd", "printing", "this host")]
    incoming = [{"ip": ip, "rport": 50000 + i, "port": port, "from": frm, "since": t - 300 * i, "you": you}
                for i, (ip, port, frm, you) in enumerate((("laptop.lan", 22, "lan", True), ("build-01.lan", 8000, "lan", False),
                                                          ("build-01.lan", 443, "lan", False), ("prometheus.lan", 9100, "lan", False),
                                                          ("phone.lan", 443, "lan", False), ("visitor.example", 443, "internet", False),
                                                          ("worker-container", 6379, "docker", False)))]
    netsec = {"ts": t, "services": services, "docker": True,
              "firewall": {"ufw_installed": True, "ufw_enabled": False, "firewalld": False, "nftables": False, "on": False, "root": False},
              "attached": [("lan", "eth0", "address hidden"), ("tailscale", "tailscale0", "address hidden")],
              "incoming": incoming, "by_from": {"lan": 7, "internet": 1, "docker": 2},
              "log": [{"ts": t - 60, "ip": "phone.lan", "from": "lan", "port": 443},
                      {"ts": t - 420, "ip": "visitor.example", "from": "internet", "port": 443}], "you": "laptop.lan"}
    timeline = [{"ts": base + 300, "level": "info", "text": "lmon started · health 74 · 5 open finding(s), 1 critical"},
                {"ts": base + 900, "level": "ai", "text": "model loaded: qwen3:8b (6.2 GB, 100% GPU)"},
                {"ts": base + 1500, "level": "warn", "text": "CPU package at 82°C"},
                {"ts": base + 1800, "level": "ok", "text": "resolved: CPU package at 82°C"},
                {"ts": base + 2400, "level": "net", "text": "build-01.lan (LAN) connected → uvicorn:8000, nginx:443"},
                {"ts": base + 2700, "level": "ai", "text": "model loaded: llama3.1:70b (42.0 GB, 74% CPU)"},
                {"ts": base + 3000, "level": "ai", "text": "benchmark qwen3:8b: 132.4 tok/s, first token 0.04s"},
                {"ts": base + 3300, "level": "bad", "text": "visitor.example (INTERNET) connected → nginx:443"},
                {"ts": base + 3500, "level": "ai", "text": "3 generations finished · 2,584 tokens · 130.1 tok/s"}]
    return {
        "demo": True, "user": "demo", "host": "demo-server", "kernel": "6.8.0-demo", "cpu_model": "Demo CPU 16-thread",
        "ts": t, "uptime_s": 86400 * 12 + 3600 * 5, "sessions": 2,
        "cpu": {"pct": pct, "per_core": per_core, "core_temps": [_wave(t + i, 30, 52, 78) for i in range(CORES)],
                "freq_mhz": [_wave(t + i * 3, 11, 1800, 4900) for i in range(CORES)], "freq_max_mhz": 5000,
                "times": [], "user": pct * 0.7, "system": pct * 0.2, "iowait": 1.4, "irq": 0.2, "hist": hist,
                "core_hist": [_series(t + i * 13, 60, 9 + i % 5, 2, 70 if i % 4 else 95, jitter=6, seed=10 + i) for i in range(CORES)],
                "load": [pct / 100 * CORES * 0.9, 5.1, 4.6], "temp": max(_wave(t + i, 30, 52, 78) for i in range(CORES)), "power_w": None},
        "mem": {"pct": 46.0, "hist": _series(t, 400, 300, 40, 50, seed=5), "used_gb": 29.4, "total_gb": 64.0, "avail_gb": 34.6,
                "cached_gb": 21.0, "swap_used_gb": 0.4, "swap_total_gb": 8.0, "swap_pct": 5.0},
        "net": {"ifaces": [{"name": "eth0", "kind": "ethernet", "up": True, "ip": None, "rx_mbps": rx[-1], "tx_mbps": tx[-1]}],
                "rx_pps": 4200, "tx_pps": 2100, "rx_total": 812 * GB, "tx_total": 196 * GB, "rx_hist": rx, "tx_hist": tx},
        "disk": {"name": "nvme0n1", "read_mbs": _wave(t, 25, 0, 180), "write_mbs": _wave(t, 33, 2, 60), "iops": 1800, "latency_ms": 0.4,
                 "temp": 46, "mounts": [{"path": "/", "pct": 86.0, "used_gb": 1650, "total_gb": 1920},
                                        {"path": "/boot/efi", "pct": 2.0, "used_gb": 0, "total_gb": 1}]},
        "temps": {"cpu": 71, "gpu": gpu["temp"], "nvme": 46, "board": 31, "ssd": None},
        "top": {"count": len(procs), "procs": procs},
        "adv": {"events_per_min": 3, "ctx_per_s": 41000, "net_pps": 6300, "io_latency_ms": 0.4,
                "events": [{"t": time.strftime("%m-%d %H:%M:%S", time.localtime(t - 30 * i)), "msg": m, "src": s, "count": c, "level": lv}
                           for i, (s, m, c, lv) in enumerate((("backup.service", "Failed with result 'exit-code'.", 4, "bad"),
                                                               ("kernel", "nvme0: temperature above threshold, throttling", 2, "warn"),
                                                               ("sshd", "Invalid user admin from visitor.example", 9, "warn"),
                                                               ("dockerd", "container restarted: cache-redis", 1, "warn")))],
                "pressure": {"fd": 3.0, "pid": 1.0, "inotify": 18.0}, "tcp": {"est": 140, "tw": 35, "cw": 0, "retrans_per_s": 0.4},
                "kernel": {"warnings": 2, "oom": 0, "rcu_stalls": 0, "mce": 0}, "proc": {"forks_per_s": 12, "dstate": 0, "zombies": 0},
                "systemd": {"restarts_per_min": 0, "failed": 1, "failed_names": ["backup.service"], "stuck": 0}},
        "errors": {}, "ollama": ai["ollama"], "gpu": gpu, "ai": ai, "netsec": netsec, "timeline": timeline,
    }
