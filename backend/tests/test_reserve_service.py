"""储量域服务测试：历史冻结、快照迁移、三链路一致、发布并发与回滚、重算。"""
from __future__ import annotations

import importlib

from app.services.reserve import criteria

service_module = importlib.import_module("app.services.reserve.service")


def _block(svc, entry_id: int):
    return svc._find(entry_id)


# ---------------------------------------------------------------- 引导与迁移
def test_引导后历史v1结论冻结且三链路一致(svc):
    report = svc.consistency()
    assert report["ok"] is True, report
    for row in report["rows"]:
        assert row["ok"], row


def test_缺面积快照的历史块段按估算时间从沿革补齐(svc):
    block = _block(svc, 1001)
    assert block["formula_version"] == criteria.V1
    # 估算时间 2026-08-12，沿革里不晚于它的最近一条是 2026-08-10 的 50000。
    assert block["面积快照"] == 50000.0
    assert block["迁移来源"] == "面积沿革"
    # 正式结论里的快照与台账一致。
    estimates = [e for e in svc.list_estimates() if e["id"] == 1001]
    assert estimates and all(e["面积快照"] == 50000.0 for e in estimates)


def test_面积快照迁移幂等(svc):
    first = svc.migrate()
    second = svc.migrate()
    assert second["backfilled"] == 0
    assert _block(svc, 1001)["面积快照"] == 50000.0


def test_超新口径上限的历史块段随v1冻结保留(svc):
    block = _block(svc, 1003)
    assert block["面积"] == 1_200_000.0  # 超过 V2 上限
    assert block["formula_version"] == criteria.V1
    # 已发布的历史结论仍可在三链路查到。
    assert any(m["id"] == 1003 for m in svc.list_map())


def test_历史v1维持旧封边结论_不按v2重算(svc):
    map_row = next(m for m in svc.list_map() if m["id"] == 1001)
    # 50000㎡×2.8% 在 V1 审批口径下：小块段、中品位、半封边。
    assert map_row["formula_version"] == criteria.V1
    assert map_row["块段边界"] == "小块段"
    assert map_row["封边规则"] == "半封边"
    report = svc.recalculate(dry_run=True)
    assert "RESE-1001" in report["skipped_historical"]


# ---------------------------------------------------------------- 保存上限
def test_登记面积超上限不允许保存(svc):
    entry, missing, error = svc.create_entry(
        {"块段编号": "X1", "矿体名称": "矿", "面积": 1_000_001, "品位": 1}
    )
    assert entry is None and missing == []
    assert "超出上限" in error


def test_登记品位超上限不允许保存(svc):
    entry, _, error = svc.create_entry(
        {"块段编号": "X2", "矿体名称": "矿", "面积": 100, "品位": 20.5}
    )
    assert entry is None
    assert "品位" in error


def test_完成估算时面积超限拒绝(svc):
    _, message = svc.complete_estimate(2001, {
        "面积": 2_000_000, "厚度": 3, "品位": 1, "矿石体重": 2.8,
    })
    assert "超出上限" in message


def test_新登记一律v2版本(svc):
    entry, _, error = svc.create_entry(
        {"块段编号": "X3", "矿体名称": "矿", "面积": 12000, "厚度": 2, "品位": 0.6}
    )
    assert error is None and entry["formula_version"] == criteria.V2


# ---------------------------------------------------------------- 估算与发布
def test_完成估算只落草稿_正式链路查不到(svc):
    view, message = svc.complete_estimate(2001, {
        "面积": 60000, "厚度": 3.5, "品位": 0.8, "矿石体重": 2.8,
    })
    assert view["块段边界"] == "小块段"
    assert view["品位区间"] == "mid" or view["品位区间"] == "中品位"
    assert svc.list_drafts()
    assert not any(e["id"] == 2001 for e in svc.list_estimates())
    assert not any(m["id"] == 2001 for m in svc.list_map())


def test_审批与手工数值冲突_审批结论落地(svc):
    view, message = svc.complete_estimate(2001, {
        "面积": 300000, "厚度": 4, "品位": 1.0, "矿石体重": 2.9,
        "approval_品位": 1.2,
    })
    assert "冲突" in message
    draft_winner = next(e for e in svc.list_drafts()
                        if e.get("_winning") and e["id"] == 2001)
    assert draft_winner["grade"] == 1.2
    assert draft_winner["winning_source"] == criteria.SOURCE_APPROVAL


def test_发布后三链路结论一致(svc):
    svc.complete_estimate(2001, {
        "面积": 60000, "厚度": 3.5, "品位": 0.8, "矿石体重": 2.8,
    })
    view, message = svc.publish(2001)
    assert view is not None, message
    ledger = view
    estimate = next(e for e in svc.list_estimates() if e["id"] == 2001)
    map_row = next(m for m in svc.list_map() if m["id"] == 2001)
    for field in ("块段边界", "品位区间", "封边规则", "矿石量", "金属量", "结论编号"):
        assert ledger[field] == estimate[field] == map_row[field]
    assert svc.consistency()["ok"]


def test_无草稿不能发布(svc):
    view, message = svc.publish(1001)  # 历史块段草稿已转正
    assert view is None
    assert "尚无估算草稿" in message


# ---------------------------------------------------------------- 并发发布
def test_并发发布只允许一个结论落地(svc):
    svc.complete_estimate(2001, {
        "面积": 60000, "厚度": 3.5, "品位": 0.8, "矿石体重": 2.8,
    })
    svc.complete_estimate(2002, {
        "面积": 120000, "厚度": 5, "品位": 1.6, "矿石体重": 2.85,
    })
    # 模拟已有一个发布正在进行（持锁未提交）：第二个发布必须立即被拒绝。
    acquired = service_module._publish_lock.acquire(blocking=False)
    assert acquired
    try:
        view, message = svc.publish(2002)
    finally:
        service_module._publish_lock.release()
    assert view is None
    assert "并发发布" in message
    # 被拒绝后块段仍是草稿，没有任何正式投影落地。
    assert svc._find(2002)["status"] == "已估算"
    assert not any(e["id"] == 2002 for e in svc.list_estimates())
    assert not any(m["id"] == 2002 for m in svc.list_map())

    # 锁释放后，只允许这一个结论落地。
    landed, msg = svc.publish(2002)
    assert landed is not None, msg
    assert svc.consistency()["ok"]


def test_发布校验失败回滚草稿与三投影(svc, monkeypatch):
    svc.complete_estimate(2001, {
        "面积": 60000, "厚度": 3.5, "品位": 0.8, "矿石体重": 2.8,
    })
    before = len([e for e in svc.list_drafts() if e["id"] == 2001])

    projections = service_module.projections
    # 人为让一致性校验失败：在写图元时把图面投影的封边规则改成与台账不一致。
    real_map = projections.map_projection

    def tampered(block, conclusion):
        row = real_map(block, conclusion)
        row["封边规则"] = "被篡改"
        return row

    monkeypatch.setattr(service_module.projections, "map_projection", tampered)
    view, message = svc.publish(2001)

    assert view is None
    assert "回滚" in message
    # 回滚后：状态未认定、草稿仍在、正式图元没有。
    assert svc._find(2001)["status"] == "已估算"
    assert len([e for e in svc.list_drafts() if e["id"] == 2001]) >= before
    assert not any(m["id"] == 2001 for m in svc.list_map())
    assert not any(e["id"] == 2001 for e in svc.list_estimates())


# ---------------------------------------------------------------- 重算
def test_重算只动v2_历史跳过(svc):
    report = svc.recalculate(reason="test")
    assert set(report["skipped_historical"]) == {"RESE-1001", "RESE-1002", "RESE-1003"}
    ids = {item["块段编号"] for item in report["recalculated"]}
    assert {"RESE-2002", "RESE-2003", "RESE-2005"} <= ids
    assert "RESE-2004" in report["republished"]
    assert svc.consistency()["ok"]


def test_重算后草稿结论按v2更新_审批仍优先(svc):
    svc.recalculate()
    winner = next(e for e in svc.list_drafts() if e["id"] == 2005 and e.get("_winning"))
    assert winner["winning_source"] == criteria.SOURCE_APPROVAL
    assert winner["grade"] == 1.2


def test_台账列表与估算详情口径一致(svc):
    items, total = svc.list_entries(page=1, size=100)
    assert total == 8
    detail = svc.get_entry(2004)
    row = next(i for i in items if i["id"] == 2004)
    for field in ("块段边界", "品位区间", "封边规则", "矿石量", "金属量", "结论编号"):
        assert row[field] == detail[field]
