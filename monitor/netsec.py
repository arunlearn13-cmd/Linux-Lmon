"""Network access view for the terminal UI: what is listening, who it is open to, whether a firewall is in
front of it, and who is connected right now.

Sources (no root needed):
  sockets      psutil.net_connections (= /proc/net/tcp{,6}, udp): listeners and established connections
  names        docker ps (published ports -> container), systemctl list-sockets (port -> unit),
               the owning process when it is ours, OLLAMA_HOST, /etc/services. A running program whose
               usual port matches is shown with a trailing '?' - likely, not verified (needs root).
  firewall     /etc/ufw/ufw.conf ENABLED=, systemctl is-active for ufw/firewalld/nftables; the rule list
               itself is only read when lmon runs as root (`ufw status verbose`).
  containers   each published container's own /proc/<pid>/net/tcp (its network namespace, readable without
               root): connections Docker forwards with NAT never appear in the host table, only there.
Run as root (`sudo lmon`) it also reads the firewall rules (ufw status, iptables INPUT) and names every
socket's process. Host sockets refresh every 3 s and container tables every 6 s; shorter connections are missed.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
from collections import Counter, deque

import psutil

from .sampler import run

# usual port -> programs that normally own it; only used when such a program is running ("apache2?")
USUAL = {80: ("apache2", "nginx", "httpd", "caddy", "lighttpd"), 443: ("apache2", "nginx", "httpd", "caddy"),
         9100: ("node_exporter", "prometheus-node-exporter"), 9090: ("prometheus",), 3000: ("grafana", "grafana-server"),
         32765: ("rpc.statd",), 32767: ("rpc.mountd",), 2049: ("nfsd",), 631: ("cupsd",), 25: ("master", "exim4", "postfix"),
         3306: ("mysqld", "mariadbd"), 5432: ("postgres",), 6379: ("redis-server",), 27017: ("mongod",), 11211: ("memcached",)}
# what a service is, from words in its name - shown so a row reads as "app-redis · cache"
KINDS = [(r"kibana|grafana|pgadmin|redisinsight|cockpit|portainer|adminer", "admin web UI"),
         (r"postgres|mysql|maria|mongo|couch|cassandra|clickhouse", "database"), (r"redis|memcache|valkey|keydb", "cache / key-value"),
         (r"qdrant|milvus|weaviate|chroma", "vector database"), (r"ollama|llama|vllm|tgi", "AI models"),
         (r"elastic|opensearch|searx|meili", "search"), (r"ssh", "remote login"), (r"nfs|rpcbind|sunrpc|statd|mountd|smb|samba|netbios", "file sharing"),
         (r"apache|nginx|httpd|caddy|traefik|lighttpd|^http", "web server"), (r"exporter|prometheus|metrics", "metrics"),
         (r"domain|dns|dnsmasq|resolved", "DNS"), (r"ipp|cups", "printing"), (r"mcp", "MCP server"), (r"playwright|chrom", "browser automation"),
         (r"next-server|node|uvicorn|gunicorn|python|java|deno|bun", "app server")]
DOCKER_PORT = re.compile(r"(?:\[?([0-9a-f.:]*)\]?):(\d+)(?:-(\d+))?->(\d+)(?:-\d+)?/(tcp|udp)")


TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))  # Tailscale's fixed ranges


def is_tailscale(a) -> bool:
    return any(a.version == n.version and a in n for n in TAILSCALE)


def kind(name: str | None) -> str:
    for pat, label in KINDS:
        if name and re.search(pat, name, re.I):
            return label
    return ""


def unmap(ip: str) -> str:
    return ip[7:] if ip.startswith("::ffff:") else ip.split("%")[0]


def interfaces():
    """[(network, iface, class)] for every local address; class is lan / tailscale / vpn / docker / local."""
    nets = []
    for name, addrs in psutil.net_if_addrs().items():
        cls = ("local" if name == "lo" else "docker" if name.startswith(("docker", "br-", "veth")) else
               "tailscale" if name.startswith("tailscale") else "vpn" if name.startswith(("ppp", "tun", "wg", "tap")) else "lan")
        for a in addrs:
            if a.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            try:  # some addresses (e.g. Tailscale's IPv6) carry no netmask: treat them as a single host
                mask = a.netmask or (32 if a.family == socket.AF_INET else 128)
                net = ipaddress.ip_network(f"{unmap(a.address)}/{mask}", strict=False)
            except ValueError:
                continue
            nets.append((net, name, cls, unmap(a.address)))
    return nets


def origin(ip: str, nets) -> str:
    """Where a remote address comes from, judged by the local interface subnets."""
    try:
        a = ipaddress.ip_address(unmap(ip))
    except ValueError:
        return "?"
    if a.is_loopback:
        return "local"
    for net, _, cls, addr in nets:
        if a.version == net.version and a in net:
            return "this host" if str(a) == addr else cls
    if is_tailscale(a):
        return "tailscale"
    return "private" if a.is_private else "internet"


def bind_class(ip: str, nets) -> str:
    """Which network a listening address serves: the class of the interface that owns it."""
    if ip in ("0.0.0.0", "::"):
        return "all"
    for _, _, cls, addr in nets:
        if addr == ip:
            return cls
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "lan"
    return "local" if a.is_loopback else "tailscale" if is_tailscale(a) else "lan"


NET_LABEL = {"lan": "LAN", "tailscale": "Tailscale", "vpn": "VPN", "docker": "containers"}


def docker_ports():
    """(proto, host port) -> container; plus each container's PID and gateway for reading its connections."""
    out = run(["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}"], timeout=4)
    if out is None:
        return None
    ports, ids = {}, []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        cid, name, image, spec = parts
        found = DOCKER_PORT.findall(spec)
        for host_ip, p0, p1, c0, proto in found:
            for i, p in enumerate(range(int(p0), int(p1 or p0) + 1)):
                ports[(proto, p)] = {"container": name, "image": image, "cport": int(c0) + i, "id": cid}
        if found:
            ids.append(cid)
    meta = {}
    if ids:
        fmt = "{{.Id}}\t{{.State.Pid}}\t{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}"
        for line in (run(["docker", "inspect", "--format", fmt, *ids], timeout=4) or "").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1].isdigit():
                meta[parts[0]] = {"pid": int(parts[1]), "gateways": set(parts[2].split())}
    for d in ports.values():  # `docker ps` gives short IDs, `docker inspect` full ones
        d.update(next((m for full, m in meta.items() if full.startswith(d["id"])), {"pid": None, "gateways": set()}))
    return ports


def _hexaddr(h: str) -> str:
    """/proc/net/tcp address field (little-endian 32-bit words) -> text IP."""
    raw = bytes.fromhex(h)
    raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    a = ipaddress.ip_address(raw)
    return str(a.ipv4_mapped) if a.version == 6 and a.ipv4_mapped else str(a)


def netns_established(pid: int):
    """[(local_port, remote_ip, remote_port)] for ESTABLISHED TCP in another process's network namespace."""
    out = []
    for f in ("tcp", "tcp6"):
        try:
            with open(f"/proc/{pid}/net/{f}") as fh:
                lines = fh.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "01":
                continue
            (lip, lport), (rip, rport) = (x.split(":") for x in parts[1:3])
            try:
                out.append((int(lport, 16), unmap(_hexaddr(rip)), int(rport, 16)))
            except ValueError:
                continue
    return out


def ssh_client():
    """IP of the SSH client lmon was started from - also under sudo, which drops SSH_CLIENT, via the parent shell."""
    ip = (os.getenv("SSH_CLIENT") or "").split(" ")[0]
    if ip:
        return ip
    try:
        p = psutil.Process().parent()
        for _ in range(6):
            if p is None:
                break
            try:
                env = p.environ()
            except psutil.Error:
                env = {}
            if env.get("SSH_CLIENT"):
                return env["SSH_CLIENT"].split(" ")[0]
            p = p.parent()
    except psutil.Error:
        pass
    return None


def systemd_sockets():
    out = run(["systemctl", "list-sockets", "--no-legend", "--no-pager", "--full"], timeout=4) or ""
    units = {}
    for line in out.splitlines():
        parts = line.split()
        m = re.search(r":(\d+)$", parts[0]) if parts else None
        if m and len(parts) >= 2:
            units[int(m.group(1))] = parts[1].removesuffix(".socket")
    return units


def firewall():
    """Facts about the host firewall that a normal user can read."""
    conf = {}
    try:
        with open("/etc/ufw/ufw.conf") as f:
            conf = dict(line.strip().split("=", 1) for line in f if "=" in line and not line.startswith("#"))
    except OSError:
        pass
    active = {}
    for unit in ("firewalld", "nftables"):
        out = run(["systemctl", "is-active", unit], ok_codes=(0, 3, 4))
        active[unit] = (out or "").strip() == "active"
    ufw = None if not conf else conf.get("ENABLED", "").strip().lower() == "yes"
    fw = {"ufw_installed": bool(conf), "ufw_enabled": ufw, "firewalld": active["firewalld"], "nftables": active["nftables"],
          "rules": None, "default_in": None}
    if os.geteuid() == 0 and ufw:  # rules are root-only
        out = run(["ufw", "status", "verbose"]) or ""
        m = re.search(r"Default:\s*(\w+) \(incoming\)", out)
        fw["default_in"] = m.group(1) if m else None
        allowed = set()
        for line in out.splitlines():
            m = re.match(r"(\d+)(?::(\d+))?(?:/(tcp|udp))?\s+(?:\(v6\)\s+)?ALLOW IN", line.strip())
            if m:
                for p in range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1):
                    allowed.add(p)
        fw["rules"] = sorted(allowed)
    fw["root"] = os.geteuid() == 0
    if fw["root"]:  # what actually filters INPUT, whoever configured it
        out = run(["iptables", "-S", "INPUT"]) or ""
        pol = re.search(r"^-P INPUT (\w+)", out, re.M)
        fw["input_policy"] = pol.group(1) if pol else None
        fw["input_rules"] = sum(1 for line in out.splitlines() if line.startswith("-A INPUT"))
    fw["on"] = bool(ufw or active["firewalld"] or active["nftables"] or fw.get("input_policy") in ("DROP", "REJECT"))
    return fw


def registry_name(port, proto):
    """The /etc/services name for a port (a convention, not proof of what runs there)."""
    try:
        return socket.getservbyport(port, proto)
    except OSError:
        return None


class NetSec:
    def __init__(self, interval: float = 3.0, ollama_port: int | None = 11434):
        self.interval, self.ollama_port = interval, ollama_port
        self.snapshot: dict = {}
        self._stop = threading.Event()
        self._at: dict = {}
        self._cache: dict = {}
        self._seen: dict = {}
        self.log: deque = deque(maxlen=200)
        self._first = True
        self.you = ssh_client()
        self._origin: dict = {}   # remote ip -> origin class; reset when the interface list is re-read
        self._nets_id = None

    def start(self):
        self.snapshot = self._collect()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            try:
                self.snapshot = self._collect()
            except Exception as e:  # noqa: BLE001 - keep the last good view
                self.snapshot = {**self.snapshot, "error": f"{type(e).__name__}: {e}"}

    def _every(self, name, secs, fn):
        now = time.monotonic()
        if name not in self._at or now - self._at[name] >= secs:
            self._at[name] = now
            try:
                self._cache[name] = fn()
            except Exception:  # noqa: BLE001
                self._cache[name] = None
        return self._cache.get(name)

    def _collect(self) -> dict:
        nets = self._every("ifaces", 30, interfaces) or []
        docker = self._every("docker", 15, docker_ports)
        units = self._every("units", 60, systemd_sockets) or {}
        fw = self._every("fw", 30, firewall) or {}
        running = self._every("names", 30, lambda: {p.info["name"] for p in psutil.process_iter(["name"])}) or set()
        conns = psutil.net_connections(kind="inet")
        if id(nets) != self._nets_id:  # interfaces re-read (every 30 s): classifications may change
            self._origin, self._nets_id = {}, id(nets)
        cache = self._origin

        def where(ip):
            if ip not in cache:
                cache[ip] = origin(ip, nets)
            return cache[ip]

        services: dict = {}
        for c in conns:
            tcp = c.type == socket.SOCK_STREAM
            if not c.laddr or (tcp and c.status != psutil.CONN_LISTEN) or (not tcp and c.raddr):
                continue
            ip = unmap(c.laddr.ip)
            if ip.startswith("fe80"):
                continue
            proto = "tcp" if tcp else "udp"
            s = services.setdefault((proto, c.laddr.port), {"proto": proto, "port": c.laddr.port, "binds": set(), "pid": None})
            s["binds"].add(ip)
            if c.pid and not s["pid"]:
                s["pid"] = c.pid

        listening = {p for (proto, p) in services if proto == "tcp"}
        now = time.time()
        current = {}
        for c in conns:
            if c.type != socket.SOCK_STREAM or c.status != psutil.CONN_ESTABLISHED or not c.raddr or c.laddr.port not in listening:
                continue
            frm = where(c.raddr.ip)
            if frm in ("local", "this host"):
                continue
            key = (unmap(c.raddr.ip), c.raddr.port, c.laddr.port)
            current[key] = frm
        # connections Docker NATs into containers: only visible inside each container's namespace. Each read makes
        # the kernel walk the whole host socket table, so the tables are refreshed every 6 s, not every tick.
        pids = sorted({d["pid"] for (proto, _), d in (docker or {}).items() if proto == "tcp" and d.get("pid")})
        ns_tables = self._every("netns", 6, lambda: {pid: netns_established(pid) for pid in pids}) or {}
        seen_ns = set()
        for (proto, hport), d in (docker or {}).items():
            if proto != "tcp" or not d.get("pid") or (d["pid"], d["cport"]) in seen_ns:
                continue
            seen_ns.add((d["pid"], d["cport"]))
            for lport, rip, rport in ns_tables.get(d["pid"], []):
                if lport != d["cport"] or rip in d["gateways"]:  # gateway = docker-proxy, i.e. a client on this host
                    continue
                frm = where(rip)
                if frm not in ("local", "this host"):
                    current[(rip, rport, hport)] = frm
        new = [k for k in current if k not in self._seen]
        for k in new:
            self._seen[k] = now
        for k in [k for k in self._seen if k not in current]:
            self._seen.pop(k)
        if not self._first:  # everything open at start is "already connected", not news
            fresh = {}
            for ip, _, port in new:  # one log line per (remote address, service), however many sockets it opened
                fresh.setdefault((ip, port), current[(ip, _, port)])
            for (ip, port), frm in sorted(fresh.items()):
                self.log.appendleft({"ts": now, "ip": ip, "from": frm, "port": port})
        self._first = False

        per_port: dict = {}
        for (ip, rp, lp), frm in current.items():
            pp = per_port.setdefault(lp, (Counter(), set()))
            pp[0][frm] += 1
            pp[1].add(ip)
        rows = []
        for (proto, port), s in services.items():
            binds = s.pop("binds")
            classes = {bind_class(b, nets) for b in binds}
            if "all" in classes:
                open_to = "everyone"
            elif classes <= {"local"}:
                open_to = "this host"
            else:
                open_to = " + ".join(NET_LABEL.get(c, c) for c in sorted(classes - {"local"})) + " only"
            name, sure, via = None, True, ""
            d = (docker or {}).get((proto, port))
            if d:
                name, via = d["container"], "docker"
            elif port in units:
                name, via = units[port], "systemd"
            elif s["pid"]:
                try:
                    name = psutil.Process(s["pid"]).name()
                except psutil.Error:
                    pass
            if not name and self.ollama_port == port and proto == "tcp":
                name = "ollama"
            if not name:
                guess = next((p for p in USUAL.get(port, ()) if p in running), None)
                if guess:
                    name, sure = guess, False
            if not name:
                name, sure, via = registry_name(port, proto), False, "root"
            registry = registry_name(port, proto)
            what = kind(d["image"] if d else None) or kind(name) or kind(registry)
            conn, peer_set = per_port.get(port, (Counter(), set())) if proto == "tcp" else (Counter(), set())
            peers = sorted(peer_set)
            if fw.get("rules") is not None:
                guard = "allowed" if port in fw["rules"] else "blocked"
            else:
                guard = "unknown" if fw.get("on") else "none"
            if d and fw.get("ufw_enabled"):
                guard = "bypassed"  # Docker's iptables rules sit in front of UFW
            rows.append({"proto": proto, "port": port, "name": name, "sure": sure, "via": via, "registry": registry,
                         "what": what, "open_to": open_to, "binds": sorted(binds), "firewall": guard,
                         "conns": dict(conn), "peers": peers, "nconn": sum(conn.values())})
        rows.sort(key=lambda r: (r["proto"] != "tcp", r["open_to"] != "everyone", -r["nconn"], r["port"]))

        attached = [(cls, name, addr) for net, name, cls, addr in nets if cls in ("lan", "tailscale", "vpn") and ":" not in addr]
        by_from = Counter(current.values())
        return {"ts": now, "services": rows, "firewall": fw, "docker": docker is not None, "attached": attached,
                "incoming": [{"ip": ip, "rport": rp, "port": lp, "from": frm, "since": self._seen.get((ip, rp, lp), now),
                              "you": ip == self.you} for (ip, rp, lp), frm in current.items()],
                "by_from": dict(by_from), "log": list(self.log), "you": self.you}
