"""块段判定口径引擎的单元测试：统一口径、审批优先级、硬上限、封边串位收敛。"""
from __future__ import annotations

import pytest

from app.services.reserve import criteria


def test_v2_审批与手工共用同一口径():
    for area, grade in [(50000, 0.3), (120000, 1.6), (650000, 3.1)]:
        approval = criteria.classify(criteria.V2, criteria.SOURCE_APPROVAL, area, grade)
        manual = criteria.classify(criteria.V2, criteria.SOURCE_MANUAL, area, grade)
        for field in ("boundary_code", "boundary_label", "grade_code", "grade_label", "seal_rule"):
            assert approval[field] == manual[field]


def test_v2_边界带判定_左开右闭():
    f = criteria.V2_FORMULA
    assert f.boundary_of(100000).code == "small"
    assert f.boundary_of(100000.0001).code == "medium"
    assert f.boundary_of(500000).code == "medium"
    assert f.boundary_of(500000.0001).code == "large"


def test_v2_品位区间判定_左开右闭():
    f = criteria.V2_FORMULA
    assert f.grade_of(0.5).code == "low"
    assert f.grade_of(0.5001).code == "mid"
    assert f.grade_of(2.0).code == "mid"
    assert f.grade_of(2.0001).code == "high"


def test_v2_封边矩阵_大块段一律全封边():
    for grade in (0.1, 1.0, 5.0):
        assert criteria.classify(criteria.V2, criteria.SOURCE_APPROVAL, 800000, grade)["seal_rule"] == "全封边"


def test_封边规则按键查表_不会串位():
    # 旧手工 V1 在 小块段×高品位 上因列错位给出「半封边」，V2 统一后必须是「全封边」。
    v1_manual = criteria.classify(criteria.V1, criteria.SOURCE_MANUAL, 50000, 4.0)
    assert v1_manual["seal_rule"] == "半封边"  # 冻结的旧串位列序，仅历史回放
    v2 = criteria.classify(criteria.V2, criteria.SOURCE_MANUAL, 50000, 4.0)
    assert v2["seal_rule"] == "全封边"


def test_v1_审批与手工阈值不同_审批优先级裁决():
    # 面积 100000：V1 审批阈值 80000 => 中块段；手工阈值 120000 => 小块段。
    approval = criteria.evaluate(criteria.V1, criteria.SOURCE_APPROVAL,
                                 {"area": 100000, "thickness": 4, "grade": 2.8, "density": 2.8})
    manual = criteria.evaluate(criteria.V1, criteria.SOURCE_MANUAL,
                               {"area": 100000, "thickness": 4, "grade": 2.8, "density": 2.8})
    assert approval["boundary_label"] == "中块段"
    assert manual["boundary_label"] == "小块段"
    winner, conflicts = criteria.arbitrate(
        {criteria.SOURCE_APPROVAL: approval, criteria.SOURCE_MANUAL: manual}
    )
    assert winner["winning_source"] == criteria.SOURCE_APPROVAL
    assert winner["boundary_label"] == "中块段"
    assert any(c["field"] == "块段边界" for c in conflicts)


def test_只有手工结论时采用手工():
    manual = criteria.evaluate(criteria.V2, criteria.SOURCE_MANUAL,
                               {"area": 60000, "thickness": 3, "grade": 0.8, "density": 2.8})
    winner, conflicts = criteria.arbitrate({criteria.SOURCE_MANUAL: manual})
    assert winner["winning_source"] == criteria.SOURCE_MANUAL
    assert conflicts == []


def test_数值冲突同样按审批优先级():
    base = {"area": 300000, "thickness": 4, "density": 2.9}
    approval = criteria.evaluate(criteria.V2, criteria.SOURCE_APPROVAL, {**base, "grade": 1.2})
    manual = criteria.evaluate(criteria.V2, criteria.SOURCE_MANUAL, {**base, "grade": 1.0})
    winner, conflicts = criteria.arbitrate(
        {criteria.SOURCE_APPROVAL: approval, criteria.SOURCE_MANUAL: manual}
    )
    assert winner["grade"] == 1.2
    assert winner["grade_label"] == "中品位"
    # 口径相同（品位区间都在中品位），差异只落在数值上：金属量随品位变化。
    assert any(c["field"] == "金属量" for c in conflicts)
    assert all(c["winner_source"] == criteria.SOURCE_APPROVAL for c in conflicts)


def test_硬上限_面积或品位超限拒绝保存():
    assert criteria.check_save_limits(1_000_001, 5)
    assert criteria.check_save_limits(100, 20.1)
    assert criteria.check_save_limits(-1, 5)
    assert criteria.check_save_limits(100, -0.1)
    assert criteria.check_save_limits(1_000_000, 20) == []


def test_evaluate_v2拒绝超限():
    with pytest.raises(ValueError):
        criteria.evaluate(criteria.V2, criteria.SOURCE_MANUAL,
                          {"area": 1_200_000, "thickness": 4, "grade": 1, "density": 2.8})
    with pytest.raises(ValueError):
        criteria.evaluate(criteria.V2, criteria.SOURCE_APPROVAL,
                          {"area": 100, "thickness": 4, "grade": 25, "density": 2.8})


def test_v1历史上限更宽_冻结可回放():
    result = criteria.evaluate(criteria.V1, criteria.SOURCE_APPROVAL,
                               {"area": 1_200_000, "thickness": 8.5, "grade": 1.2, "density": 2.7})
    assert result["boundary_label"] == "大块段"


def test_矿石量金属量计算():
    result = criteria.evaluate(criteria.V2, criteria.SOURCE_MANUAL,
                               {"area": 100000, "thickness": 5, "grade": 2, "density": 2.8})
    assert result["ore_tonnage"] == 1_400_000.0
    assert result["metal_tonnage"] == 28_000.0


def test_封边矩阵加载时校验完整性():
    with pytest.raises(RuntimeError):
        criteria._seal_matrix(["a", "b"], ["x"], [["x"], ["y"], ["z"]])
    with pytest.raises(RuntimeError):
        criteria._seal_matrix(["a", "b"], ["x", "y"], [["x", "y"]])
