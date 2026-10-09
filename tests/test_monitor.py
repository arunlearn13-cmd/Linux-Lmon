from fastapi.testclient import TestClient

from monitor.sampler import Rates, pct, phys_kind


def test_rates():
    r = Rates()
    assert r.rate("a", 10, 0.0) is None
    assert r.rate("a", 30, 2.0) == 10
    assert r.rate("a", 5, 3.0) == 0  # counter reset never goes negative


def test_pct():
    assert pct(1, 4) == 25 and pct(None, 4) is None and pct(1, 0) is None


def test_snapshot_shape():
    from monitor.server import app
    with TestClient(app) as c:
        body = c.get("/api/metrics").json()
        assert body["cpu"]["per_core"] and 0 <= body["mem"]["pct"] <= 100
        cpu = body["cpu"]
        assert len(cpu["core_hist"]) == len(cpu["per_core"]) and cpu["hist"]
        assert {"tcp", "pressure", "systemd"} <= body["adv"].keys()
        assert c.get("/").status_code == 200


def test_token(monkeypatch):
    import monitor.server as s
    monkeypatch.setattr(s, "TOKEN", "s3cret")
    with TestClient(s.app) as c:
        assert c.get("/api/metrics").status_code == 401
        assert c.get("/api/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_phys_kind_excludes_virtual():
    assert phys_kind("lo") is None
    assert phys_kind("definitely-not-an-iface") is None
