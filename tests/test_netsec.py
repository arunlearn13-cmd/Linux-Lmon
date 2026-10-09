import ipaddress

from monitor import netsec
from monitor.netsec import DOCKER_PORT, bind_class, kind, origin

NETS = [(ipaddress.ip_network("192.0.2.0/24"), "eth0", "lan", "192.0.2.10"),
        (ipaddress.ip_network("198.51.100.0/24"), "br-x", "docker", "198.51.100.1"),
        (ipaddress.ip_network("100.64.0.5/32"), "tailscale0", "tailscale", "100.64.0.5"),
        (ipaddress.ip_network("127.0.0.0/8"), "lo", "local", "127.0.0.1")]


def test_origin_classes():
    assert origin("192.0.2.20", NETS) == "lan"
    assert origin("::ffff:192.0.2.21", NETS) == "lan"  # IPv4-mapped IPv6
    assert origin("198.51.100.6", NETS) == "docker"
    assert origin("100.101.1.2", NETS) == "tailscale"  # Tailscale CGNAT range even if not our subnet
    assert origin("192.0.2.10", NETS) == "this host"
    assert origin("203.0.113.9", NETS) == "private" and origin("8.8.8.8", NETS) == "internet"


def test_bind_scope():
    assert bind_class("0.0.0.0", NETS) == "all" and bind_class("::", NETS) == "all"
    assert bind_class("127.0.0.1", NETS) == "local" and bind_class("192.0.2.10", NETS) == "lan"
    assert bind_class("fd7a:115c:a1e0::1", NETS) == "tailscale"


def test_docker_ports_parse(monkeypatch):
    ps = ("aaa111\tvector-qdrant\tqdrant/qdrant\t0.0.0.0:6333-6334->6333-6334/tcp, [::]:6333-6334->6333-6334/tcp\n"
          "bbb222\tpgadmin\tdpage/pgadmin4\t443/tcp, 0.0.0.0:5050->80/tcp, [::]:5050->80/tcp\n"
          "ccc333\tplaywright\tmcr/pw\t3000-3001/tcp\n")
    inspect = "aaa111ffff\t4114\t198.51.100.1 \nbbb222ffff\t4200\t198.51.100.1 \n"
    monkeypatch.setattr(netsec, "run", lambda cmd, **k: ps if cmd[1] == "ps" else inspect)
    ports = netsec.docker_ports()
    assert ports[("tcp", 6334)]["container"] == "vector-qdrant" and ports[("tcp", 6334)]["cport"] == 6334
    assert ports[("tcp", 5050)]["cport"] == 80 and ports[("tcp", 5050)]["pid"] == 4200 and ports[("tcp", 5050)]["gateways"] == {"198.51.100.1"}
    assert ("tcp", 443) not in ports and ("tcp", 3000) not in ports  # exposed but not published
    assert DOCKER_PORT.findall("0.0.0.0:53->53/udp")[0][-1] == "udp"


def test_container_namespace_connections(tmp_path, monkeypatch):
    real_open = open
    table = ("  sl  local_address rem_address   st\n"
             "   0: 056433C6:18EB 140200C0:9E02 01 0\n"      # 198.51.100.5:6379 <- 192.0.2.20:40450 established
             "   1: 056433C6:18EB 016433C6:9E03 0A 0\n")     # listen-ish state: ignored
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: __import__("io").StringIO(table) if str(f).endswith("/net/tcp")
                        else (_ for _ in ()).throw(OSError()) if str(f).endswith("/net/tcp6") else real_open(f, *a, **k))
    assert netsec.netns_established(1234) == [(6379, "192.0.2.20", 40450)]
    assert netsec._hexaddr("0000000000000000FFFF00000100007F") == "127.0.0.1"


def test_ssh_client_found_from_parent_when_sudo_drops_it(monkeypatch):
    monkeypatch.delenv("SSH_CLIENT", raising=False)

    class Proc:
        def __init__(self, env, parent=None):
            self._env, self._parent = env, parent

        def environ(self):
            return self._env

        def parent(self):
            return self._parent
    shell = Proc({"SSH_CLIENT": "192.0.2.20 51000 22"})
    monkeypatch.setattr(netsec.psutil, "Process", lambda *a: Proc({}, Proc({}, shell)))
    assert netsec.ssh_client() == "192.0.2.20"


def test_kinds():
    assert kind("redisinsight") == "admin web UI" and kind("app-redis") == "cache / key-value"
    assert kind("metrics-postgres-1") == "database" and kind("vector-qdrant") == "vector database"
    assert kind("mystery") == ""


def test_live_collect_runs_and_logs_only_new_connections():
    n = netsec.NetSec()
    first = n._collect()
    assert "services" in first and first["log"] == []  # connections open at start are not "new"
    assert all(r["open_to"] for r in first["services"])
