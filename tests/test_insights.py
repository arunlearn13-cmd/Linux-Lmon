import copy
import time

from rich.console import Console

from monitor import tui
from monitor.insights import Timeline, analyze, every, score, verdict

from test_tui import SNAP


def svc(port, name, what, open_to, via="", conns=None):
    return {"proto": "tcp", "port": port, "name": name, "sure": True, "via": via, "registry": None, "what": what,
            "open_to": open_to, "binds": [], "firewall": "none", "conns": conns or {}, "peers": [], "nconn": sum((conns or {}).values())}


NET = {"services": [svc(6379, "app-redis", "cache / key-value", "everyone", "docker", {"docker": 1}),
                    svc(22, "ssh", "remote login", "everyone", "systemd", {"lan": 3}),
                    svc(5432, "postgres", "database", "this host")],
       "firewall": {"ufw_installed": True, "ufw_enabled": False, "on": False},
       "attached": [("lan", "eth0", "192.0.2.10")], "docker": True,
       "incoming": [{"ip": "192.0.2.20", "rport": 5000, "port": 22, "from": "lan", "since": 0, "you": True},
                    {"ip": "8.8.4.4", "rport": 6000, "port": 22, "from": "internet", "since": 0, "you": False}],
       "by_from": {"lan": 3, "internet": 1}, "log": [{"ts": 0, "ip": "192.0.2.21", "from": "lan", "port": 9100}], "you": "192.0.2.20"}


def keys(D):
    return {f["key"]: f for f in analyze(D)}


def test_rules_fire_only_on_measured_values():
    calm = copy.deepcopy(SNAP)
    calm["adv"]["systemd"].update(failed=0, failed_names=[])
    calm["cpu"]["temp"] = 50
    assert analyze(calm) == [] and score([]) == 100 and verdict([]) == "NOMINAL"

    D = copy.deepcopy(SNAP)
    D["cpu"]["temp"] = 88
    D["mem"].update(avail_gb=4, total_gb=63)
    D["disk"]["mounts"] = [{"path": "/", "pct": 93, "used_gb": 465, "total_gb": 500}]
    D["netsec"] = NET
    f = keys(D)
    assert f["cpu.hot"]["level"] == "bad" and f["mem.low"]["level"] == "bad" and f["disk.full:/"]["level"] == "bad"
    assert f["net.risky"]["title"] == "1 databases / admin UIs open to every network"  # localhost-only postgres is fine
    assert "app-redis:6379" in f["net.risky"]["detail"]
    assert f["net.nofirewall"]["title"] == "no firewall: 2 services open to every network"
    assert f["net.internet"]["detail"] == "8.8.4.4"
    assert f["systemd.failed"]["detail"] == "foo.service"
    assert verdict(analyze(D)) == "CRITICAL"  # cpu.hot + mem.low + disk.full + systemd.failed are all critical
    many_small = [{"level": "info"}] * 30 + [{"level": "warn"}] * 2
    assert score(many_small) == 85 and verdict(many_small) == "WATCH"


def test_repeating_ollama_errors_are_explained():
    now = time.time()
    reqs = [{"ts": now - 600 * i, "status": 404, "method": "POST", "path": "/api/chat", "client": "100.64.0.5"} for i in range(6)]
    D = {**SNAP, "ai": {"requests": {"recent": reqs}}}
    f = keys(D)["ollama.err:404:/api/chat:100.64.0.5"]
    assert f["title"] == "6x 404 POST /api/chat from 100.64.0.5"
    assert "every ~10 min" in f["detail"] and "model is not installed" in f["detail"]
    assert every([0, 10, 20, 31]) == "~10 s" and every([0, 10, 200]) is None


def test_timeline_records_transitions_and_persists(tmp_path):
    path = tmp_path / "t.jsonl"
    tl = Timeline(str(path))
    t0 = tl._started
    D = copy.deepcopy(SNAP)
    D["ai"] = {"loaded": [], "requests": {"tokens": {"recent_tasks": []}}, "bench": {"runs": []}}
    D["ollama"] = {"running": True}
    tl.update(D, analyze(D), now=t0 + 1)
    assert tl.events == []  # still settling
    D["cpu"]["temp"] = 90  # appears while settling: counted as already open, never announced
    tl.update(D, analyze(D), now=t0 + 21)
    assert len(tl.events) == 1 and "lmon started" in tl.events[0]["text"] and "2 open finding(s)" in tl.events[0]["text"]

    D["ai"]["loaded"] = [{"name": "qwen3:8b", "size_gb": 5.0, "gpu_pct": 100}]
    D["ai"]["requests"]["tokens"]["recent_tasks"] = [{"ts": time.time(), "gen_tokens": 100, "gen_ms": 1000}]
    D["mem"].update(avail_gb=4, total_gb=63)
    tl.update(D, analyze(D), now=t0 + 30)
    texts = [e["text"] for e in tl.events]
    assert "model loaded: qwen3:8b (5.0 GB, 100% GPU)" in texts
    assert "1 generation finished · 100 tokens · 100.0 tok/s" in texts
    assert not any("memory available" in x for x in texts)  # not yet held for 20 s
    tl.update(D, analyze(D), now=t0 + 51)
    assert any("memory available" in e["text"] for e in tl.events)

    D["cpu"]["temp"] = 50  # resolves only after staying gone for 20 s; a blip back does not count
    tl.update(D, analyze(D), now=t0 + 60)
    D["cpu"]["temp"] = 90
    tl.update(D, analyze(D), now=t0 + 65)
    D["cpu"]["temp"] = 50
    tl.update(D, analyze(D), now=t0 + 70)
    assert not any(e["text"].startswith("resolved: CPU") for e in tl.events)
    D["ollama"] = {"running": False}
    tl.update(D, analyze(D), now=t0 + 91)
    texts = [e["text"] for e in tl.events]
    assert "resolved: CPU package at 90°C" in texts and "Ollama stopped responding" in texts
    assert [e["text"] for e in Timeline(str(path)).events] == texts  # survives a restart


def test_big_digits_and_top_programs():
    rows = tui.big_number(92, "bold")
    assert len(rows) == 3 and len({len(r) for r in rows}) == 1
    procs = [{"pid": i, "name": "postgres", "user": "70", "cpu": 10.0, "mem": 0.2, "rss": 100 << 20, "threads": 1, "cmd": "pg"}
             for i in range(5)] + [{"pid": 99, "name": "node", "user": "me", "cpu": 30.0, "mem": 0.3, "rss": 200 << 20, "threads": 12, "cmd": "n"}]
    D = {**SNAP, "top": {"count": 6, "procs": procs}}
    c = Console(width=140, height=40, record=True, file=open("/dev/null", "w"))
    c.print(tui.build(D, "pulse", size=(140, 40)))
    out = c.export_text()
    assert "TOP PROGRAMS" in out and "THREAD HEATMAP" not in out
    pg = next(l for l in out.splitlines() if "postgres" in l and "500M" in l)  # 5 processes summed into one row
    assert " 5 " in pg and "50%" in pg
    assert out.index("postgres") < out.index("node ")  # 50% total beats 30%


def test_net_page_is_readable():
    D = {**SNAP, "netsec": NET}
    c = Console(width=160, height=40, record=True, file=open("/dev/null", "w"))
    c.print(tui.build(D, "net", size=(160, 40)))
    out = c.export_text()
    for s in ("FIREWALL OFF", "UFW is installed but disabled", "LAN eth0 192.0.2.10", "2 open to EVERY network",
              "app-redis", "cache / key-value", "EVERYONE", "1 container", "192.0.2.20", "you", "ssh:22",
              "INTERNET", "8.8.4.4", "local/udp hidden (a)"):
        assert s in out, s
    assert "postgres" not in out.split("CONNECTED NOW")[0].split("SERVICES")[1]  # localhost-only hidden by default
    assert "FIREWALL " not in out.split("SERVICES")[1].split("\n")[1]  # no per-row firewall column when there is no firewall
    on = {**NET, "firewall": {"ufw_installed": True, "ufw_enabled": True, "on": True},
          "services": [{**NET["services"][0], "firewall": "bypassed"}]}
    c = Console(width=160, height=40, record=True, file=open("/dev/null", "w"))
    c.print(tui.build({**SNAP, "netsec": on}, "net", size=(160, 40)))
    out_on = c.export_text()
    assert "FIREWALL ON" in out_on and "BYPASSED" in out_on and "Docker bypasses UFW" in out_on
    c = Console(width=160, height=40, record=True, file=open("/dev/null", "w"))
    c.print(tui.build(D, "pulse", size=(160, 40)))
    out = c.export_text()
    assert "NETWORK ACCESS" in out and "press 5" in out
