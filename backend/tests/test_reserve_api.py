"""HTTP 层测试：路由把硬上限、并发发布、三链路一致性暴露成正确的状态码与报文。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.reserve.service as service_module
from app import main as main_module
from app.store import Store


@pytest.fixture
def client(monkeypatch):
    rebuilt = Store()
    monkeypatch.setattr(service_module, "store", rebuilt)
    # 路由模块在导入时已 bootstrap 过旧仓库；这里重建服务实例并重新引导。
    import app.routers.reserve as reserve_router
    monkeypatch.setattr(reserve_router, "service", service_module.ReserveService())
    reserve_router.service.bootstrap()
    return TestClient(main_module.app)


def test_台账列表返回统一口径列(client):
    data = client.get("/api/reserve").json()
    assert data["total"] == 8
    row = next(i for i in data["items"] if i["id"] == 2004)
    assert row["块段边界"] and row["封边规则"]


def test_估算详情与列表口径一致(client):
    listing = client.get("/api/reserve").json()["items"]
    row = next(i for i in listing if i["id"] == 2004)
    detail = client.get("/api/reserve/2004").json()
    for field in ("块段边界", "品位区间", "封边规则", "矿石量", "金属量", "结论编号"):
        assert row[field] == detail[field]


def test_面积超上限登记返回400(client):
    r = client.post("/api/reserve", json={"values": {
        "块段编号": "T1", "矿体名称": "矿", "面积": 2_000_000, "品位": 1,
    }})
    assert r.status_code == 400
    assert "超出上限" in r.json()["detail"]


def test_品位超上限登记返回400(client):
    r = client.post("/api/reserve", json={"values": {
        "块段编号": "T2", "矿体名称": "矿", "面积": 100, "品位": 21,
    }})
    assert r.status_code == 400


def test_估算审批冲突到发布三链路一致(client):
    r = client.post("/api/reserve/2001/estimate", json={"values": {
        "面积": 300000, "厚度": 4, "品位": 1.0, "矿石体重": 2.9,
        "approval_品位": 1.2,
    }})
    assert r.json()["ok"]
    assert "审批模型" in r.json()["message"]
    r = client.post("/api/reserve/2001/actions", json={"values": {"action": "认定结果"}})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get("/api/reserve/consistency").json()["ok"]
    estimate = next(e for e in client.get("/api/reserve/estimates").json()["items"] if e["id"] == 2001)
    map_row = next(m for m in client.get("/api/reserve/map").json()["items"] if m["id"] == 2001)
    assert estimate["品位"] == 1.2 and map_row["品位区间"] == estimate["品位区间"]


def test_并发发布第二个请求被拒绝(client):
    client.post("/api/reserve/2001/estimate", json={"values": {
        "面积": 60000, "厚度": 3.5, "品位": 0.8, "矿石体重": 2.8,
    }})
    acquired = service_module._publish_lock.acquire(blocking=False)
    assert acquired
    try:
        r = client.post("/api/reserve/2001/actions", json={"values": {"action": "认定结果"}})
    finally:
        service_module._publish_lock.release()
    body = r.json()
    assert body["ok"] is False
    assert "并发发布" in body["message"]


def test_重算接口跳过历史项目(client):
    r = client.post("/api/reserve/recalculate", json={"values": {}})
    report = r.json()["entry"]
    assert set(report["skipped_historical"]) == {"RESE-1001", "RESE-1002", "RESE-1003"}
    assert "RESE-2004" in report["republished"]


def test_迁移接口幂等(client):
    first = client.post("/api/reserve/migrate-area-snapshots").json()["entry"]
    assert first["backfilled"] == 0  # bootstrap 已迁移
    assert client.get("/api/reserve/1001").json()["面积快照"] == 50000.0
