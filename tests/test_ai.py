import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from monitor import ai, tui
from monitor.ai import AICollector, go_duration, parse_env, parse_requests

from test_tui import SNAP, frame

LOG = """\
[GIN] 2026/01/15 - 10:00:00 | 404 |       2.5ms |    100.64.0.5 | POST     "/api/chat"
[GIN] 2026/01/15 - 10:00:05 | 200 |        20µs |       127.0.0.1 | GET      "/api/ps"
slot print_timing: id  1 | task 7 | n_gen =    400, tg = 150.00 t/s, tg_3s = 150.00 t/s
slot print_timing: id  1 | task 7 | prompt eval time =      20.00 ms /    20 tokens (    1.00 ms per token,  1000.00 tokens per second)
slot print_timing: id  1 | task 7 |        eval time =    6000.00 ms /   900 tokens (    6.67 ms per token,   150.00 tokens per second)
slot print_timing: id  1 | task 7 |       total time =    6020.00 ms /   920 tokens
slot      release: id  1 | task 7 | stop processing: n_tokens = 1000, truncated = 0
[GIN] 2026/01/15 - 10:01:00 | 200 |         4m30s |      198.51.100.6 | POST     "/api/chat"
[GIN] 2026/01/15 - 10:02:00 | 200 |      1.5s |      198.51.100.6 | POST     "/v1/chat/completions"
[GIN] 2026/01/15 - 10:03:00 | 200 |      10ms |      198.51.100.6 | GET     "/v1/models"
time=2026-01-15 level=INFO msg="not a request line"
"""


def test_go_duration():
    assert go_duration("4m30s") == 270 and abs(go_duration("22.982µs") - 22.982e-6) < 1e-12
    assert go_duration("1h2m3.5s") == 3723.5 and go_duration("n/a") is None


def test_parse_requests_separates_polling_and_errors():
    r = parse_requests(LOG)
    assert r["polls"] == 2 and r["requests"] == 3 and r["inference"] == 3 and r["errors"] == 1
    assert r["max_s"] == 270 and r["recent"][0]["path"] == "/v1/chat/completions"


def test_runner_token_timings_paired_with_requests():
    tk = parse_requests(LOG)["tokens"]
    assert tk["tasks"] == 1 and tk["in"] == 20 and tk["out"] == 900 and abs(tk["gen_tps"] - 150.0) < 0.01
    assert tk["last"]["ctx"] == 1000 and tk["live"] == []  # finished task is no longer live
    chat = next(q for q in parse_requests(LOG)["recent"] if q["path"] == "/api/chat" and q["status"] == 200)
    assert chat["task"]["gen_tokens"] == 900
    live = parse_requests("slot print_timing: id  0 | task 3 | n_gen =  50, tg = 90.0 t/s, tg_3s = 95.5 t/s")["tokens"]["live"]
    assert live[0]["n_gen"] == 50 and live[0]["tps"] == 95.5


def test_parse_env_keeps_only_ai_vars():
    out = 'Environment=PATH=/usr/bin OLLAMA_HOST=0.0.0.0:11434 "OLLAMA_MODELS=/data/my models" CUDA_VISIBLE_DEVICES=0\nMainPID=7'
    assert parse_env(out) == {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": "/data/my models", "CUDA_VISIBLE_DEVICES": "0"}


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["model"] == "qwen3:8b" and body["stream"] is False
        out = {"eval_count": 100, "eval_duration": 2_000_000_000, "prompt_eval_count": 20, "prompt_eval_duration": 100_000_000,
               "load_duration": 50_000_000, "total_duration": 2_200_000_000}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    def log_message(self, *a):
        pass


def test_bench_measures_tokens_and_never_loads(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(ai, "OLLAMA_URL", f"http://127.0.0.1:{srv.server_port}")
    c = AICollector()
    c.snapshot = {"loaded": []}
    assert "no generation model loaded" in c.start_bench()
    c.snapshot = {"loaded": [{"name": "nomic-embed-text", "family": "nomic-bert"}, {"name": "qwen3:8b", "family": "qwen3"}]}
    assert "qwen3:8b" in c.start_bench()
    for _ in range(50):
        if not c._bench["running"]:
            break
        time.sleep(0.05)
    srv.shutdown()
    run = c._bench["runs"][0]
    assert c._bench["error"] is None and run["gen_tps"] == 50 and run["prompt_tps"] == 200 and abs(run["ttft_s"] - 0.15) < 1e-9


AI = {"gpu": {"name": "RTX", "util": 40, "mem_util": 5, "vram_used_gb": 4, "vram_total_gb": 12, "vram_pct": 33, "temp": 60,
              "power_w": 90, "power_limit_w": 200, "sm_mhz": 1800, "mem_mhz": 7000, "sm_max_mhz": 2400, "fan": 30, "pstate": "P2",
              "pcie": "gen4 x8", "driver": "550", "util_hist": [10, 50, 90], "vram_hist": [50, 50, 50],
              "apps": [{"pid": 9, "name": "llama-server", "vram_gb": 3.5}]},
      "ollama": {"running": True, "url": "http://127.0.0.1:11434", "version": "0.5.0",
                 "models": [{"name": "qwen3:8b", "size_gb": 5, "vram_gb": 5, "context": 8192}]},
      "loaded": [{"name": "qwen3:8b", "size_gb": 5, "vram_gb": 5, "gpu_pct": 100, "context": 8192, "expires_in": 600,
                  "params": "8.2B", "quant": "Q4_K_M", "family": "qwen3", "ctx_max": 40960}],
      "installed": [{"name": "qwen3:8b", "size_gb": 5, "params": "8.2B", "quant": "Q4_K_M", "family": "qwen3", "ctx_max": 40960}],
      "config": {"unit": "ollama", "state": "active", "pid": 1, "env": {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_NUM_PARALLEL": "2"}},
      "server": {"pid": 1, "cpu": 3.0, "rss_gb": 0.5, "runners": 1, "uptime_s": 99},
      "requests": {**parse_requests(LOG), "tokens": {**parse_requests(LOG)["tokens"],
                                                     "live": [{"slot": 0, "n_gen": 50, "tps": 95.5, "ts": 0}]}},
      "bench": {"running": False, "runs": [{"model": "qwen3:8b", "at": 0, "gen_tps": 87.5, "prompt_tps": 900, "gen_tokens": 128,
                                            "prompt_tokens": 20, "load_s": 0.01, "total_s": 1.6, "ttft_s": 0.05}], "error": None},
      "errors": {}}


def test_ai_page_shows_real_fields():
    D = tui.merge(SNAP, AI)
    from rich.console import Console
    c = Console(width=160, height=45, record=True, file=open("/dev/null", "w"))
    c.print(tui.build(D, "ai", size=(160, 45)))
    out = c.export_text()
    for s in ("qwen3:8b", "100% GPU", "87.5", "tok/s", "listening on all interfaces", "404", "/api/chat", "llama-server", "P2",
              "GENERATING", "150.0", "900 tok"):
        assert s in out, s
    assert len(out.splitlines()) == 45
