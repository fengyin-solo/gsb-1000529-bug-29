"""块段判定的唯一口径。

历史上审批模型与手工公式各自维护一份「块段边界 / 品位区间 / 封边规则」的判定表，
同一块段会拿到两套结论，封边还会按错位列查表（串位）。这里把三套判定收成一张
显式矩阵，并强制所有调用方只经 :func:`classify` 取值，从根上杜绝各算各的。

口径分两个版本：

* ``V2``（现行）：2026-09-30 口径调整后的统一版本，审批模型与手工公式都走本引擎，
  边界带、品位带、封边规则不可能再打架；冲突只可能剩数值（厚度/体重/品位取值）差异，
  按审批优先级裁决。
* ``V1``（历史）：仅用于回放历史项目的旧结论。历史项目维持此版本，不参与新口径重算。
  V1 的审批模型与手工公式阈值不同、封边矩阵列序错位，是本次要收敛掉的旧口径，
  保留它只是为了让历史估算结论可复算、可追溯。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 现行硬上限：块段面积或品位超过上限一律不允许保存。历史版本不再放大上限，
# 历史存量只通过冻结版本保留，不允许再新增超限块段。
MAX_AREA_M2 = 1_000_000.0
MAX_GRADE_PERCENT = 20.0

V1 = "v1"
V2 = "v2"
CURRENT_VERSION = V2

# 口径调整生效时间：早于该时间的历史项目冻结在 V1。
EFFECTIVE_AT = "2026-09-30"

# 封边规则三档（任何版本都只能落到这三个值上）。
SEAL_FULL = "全封边"
SEAL_HALF = "半封边"
SEAL_NONE = "不封边"

# 判定来源优先级：冲突时审批模型裁决。
SOURCE_APPROVAL = "approval"
SOURCE_MANUAL = "manual"
SOURCE_PRIORITY = {SOURCE_APPROVAL: 2, SOURCE_MANUAL: 1}

REQUIRED_INPUTS = ("area", "thickness", "grade", "density")


@dataclass(frozen=True)
class BoundaryBand:
    """块段边界带：按块段面积（㎡）分档，区间为左开右闭 (low, high]。"""

    code: str
    label: str
    low: float  # 不含
    high: float  # 含


@dataclass(frozen=True)
class GradeBand:
    """品位区间：按品位（%）分档，区间为左开右闭 (low, high]。"""

    code: str
    label: str
    low: float  # 不含
    high: float  # 含


@dataclass(frozen=True)
class FormulaVersion:
    """一版口径的全部判定表。

    ``seal_matrix`` 以 ``{边界带 code: {品位带 code: 封边规则}}`` 显式给出，
    用 dict 按键取值，旧代码里按列序号/列错位查表导致的「封边串位」不会再发生。
    """

    version: str
    boundaries: tuple[BoundaryBand, ...]
    grades: tuple[GradeBand, ...]
    seal_matrix: dict[str, dict[str, str]]
    max_area: float
    max_grade: float
    note: str

    def boundary_of(self, area: float) -> BoundaryBand:
        for band in self.boundaries:
            if area > band.low and area <= band.high:
                return band
        raise ValueError(f"块段面积 {area} 超出 {self.version.upper()} 边界带定义域")

    def grade_of(self, grade: float) -> GradeBand:
        for band in self.grades:
            if grade > band.low and grade <= band.high:
                return band
        raise ValueError(f"品位 {grade} 超出 {self.version.upper()} 品位区间定义域")

    def seal_of(self, area: float, grade: float) -> str:
        """封边规则：边界带与品位带各只判定一次，再按键查表，杜绝串位。"""
        boundary = self.boundary_of(area)
        grade_band = self.grade_of(grade)
        try:
            return self.seal_matrix[boundary.code][grade_band.code]
        except KeyError:
            # 口径表配置不全属于实现错误，直接暴露，不允许静默落到默认档。
            raise RuntimeError(
                f"{self.version.upper()} 封边矩阵缺少 {boundary.code}×{grade_band.code} 的判定"
            ) from None


def _seal_matrix(boundary_codes: list[str], grade_codes: list[str], rows: list[list[str]]) -> dict[str, dict[str, str]]:
    """把二维判定表翻译成按键索引的显式矩阵。

    旧口径是「手工对齐列」的二维表，审批模型改过一次列序后手工表没跟上，列错位
    （串位）就是这么来的。这里在加载阶段就把行、列与 code 对齐并做完整性校验。
    """
    if len(rows) != len(boundary_codes):
        raise RuntimeError("封边矩阵行数与边界带不一致")
    matrix: dict[str, dict[str, str]] = {}
    for code, row in zip(boundary_codes, rows):
        if len(row) != len(grade_codes):
            raise RuntimeError(f"封边矩阵在 {code} 行的列数与品位区间不一致")
        matrix[code] = dict(zip(grade_codes, row))
    return matrix


# ---- V2 现行统一口径 -------------------------------------------------------

_V2_BOUNDARIES = (
    BoundaryBand("small", "小块段", 0.0, 100_000.0),
    BoundaryBand("medium", "中块段", 100_000.0, 500_000.0),
    BoundaryBand("large", "大块段", 500_000.0, MAX_AREA_M2),
)
_V2_GRADES = (
    GradeBand("low", "低品位", 0.0, 0.5),
    GradeBand("mid", "中品位", 0.5, 2.0),
    GradeBand("high", "高品位", 2.0, MAX_GRADE_PERCENT),
)
# 边界越大、品位越高，封边要求越强（V2 审批模型与手工公式共用这一张表）。
_V2_SEAL_ROWS = [
    [SEAL_NONE, SEAL_HALF, SEAL_FULL],  # 小块段
    [SEAL_HALF, SEAL_HALF, SEAL_FULL],  # 中块段
    [SEAL_FULL, SEAL_FULL, SEAL_FULL],  # 大块段
]

V2_FORMULA = FormulaVersion(
    version=V2,
    boundaries=_V2_BOUNDARIES,
    grades=_V2_GRADES,
    seal_matrix=_seal_matrix(
        ["small", "medium", "large"], ["low", "mid", "high"], _V2_SEAL_ROWS
    ),
    max_area=MAX_AREA_M2,
    max_grade=MAX_GRADE_PERCENT,
    note="现行统一口径：块段边界、品位区间、封边规则只有一张判定表",
)

# ---- V1 历史口径（仅历史项目冻结使用） ------------------------------------
# 审批模型旧阈值与手工公式旧阈值不同；手工表的封边列序是错的（中/高品位两列对调），
# 即本次要收敛掉的「封边串位」。保留它仅为复算历史结论。
_V1_APPROVAL_BOUNDARIES = (
    BoundaryBand("small", "小块段", 0.0, 80_000.0),
    BoundaryBand("medium", "中块段", 80_000.0, 400_000.0),
    BoundaryBand("large", "大块段", 400_000.0, 2_000_000.0),
)
_V1_APPROVAL_GRADES = (
    GradeBand("low", "低品位", 0.0, 0.6),
    GradeBand("mid", "中品位", 0.6, 3.0),
    GradeBand("high", "高品位", 3.0, 30.0),
)
_V1_APPROVAL_SEAL_ROWS = [
    [SEAL_NONE, SEAL_HALF, SEAL_FULL],
    [SEAL_HALF, SEAL_HALF, SEAL_FULL],
    [SEAL_FULL, SEAL_FULL, SEAL_FULL],
]

_V1_MANUAL_BOUNDARIES = (
    BoundaryBand("small", "小块段", 0.0, 120_000.0),
    BoundaryBand("medium", "中块段", 120_000.0, 600_000.0),
    BoundaryBand("large", "大块段", 600_000.0, 2_000_000.0),
)
_V1_MANUAL_GRADES = (
    GradeBand("low", "低品位", 0.0, 0.4),
    GradeBand("mid", "中品位", 0.4, 2.5),
    GradeBand("high", "高品位", 2.5, 30.0),
)
# 注意末两行的中/高品位列对调，就是旧手工公式的串位列序，按原样冻结。
_V1_MANUAL_SEAL_ROWS = [
    [SEAL_NONE, SEAL_FULL, SEAL_HALF],
    [SEAL_HALF, SEAL_FULL, SEAL_HALF],
    [SEAL_FULL, SEAL_FULL, SEAL_FULL],
]

_V1_CODES = ["small", "medium", "large"]
_V1_GRADE_CODES = ["low", "mid", "high"]

V1_APPROVAL_FORMULA = FormulaVersion(
    version=V1,
    boundaries=_V1_APPROVAL_BOUNDARIES,
    grades=_V1_APPROVAL_GRADES,
    seal_matrix=_seal_matrix(_V1_CODES, _V1_GRADE_CODES, _V1_APPROVAL_SEAL_ROWS),
    max_area=2_000_000.0,
    max_grade=30.0,
    note="历史口径·审批模型（2026-09-30 前冻结）",
)

V1_MANUAL_FORMULA = FormulaVersion(
    version=V1,
    boundaries=_V1_MANUAL_BOUNDARIES,
    grades=_V1_MANUAL_GRADES,
    seal_matrix=_seal_matrix(_V1_CODES, _V1_GRADE_CODES, _V1_MANUAL_SEAL_ROWS),
    max_area=2_000_000.0,
    max_grade=30.0,
    note="历史口径·手工公式（2026-09-30 前冻结，封边列序有错位）",
)

# V2 下审批与手工共用同一张口径表；V1 下两者各自保留。
_FORMULAS: dict[tuple[str, str], FormulaVersion] = {
    (V2, SOURCE_APPROVAL): V2_FORMULA,
    (V2, SOURCE_MANUAL): V2_FORMULA,
    (V1, SOURCE_APPROVAL): V1_APPROVAL_FORMULA,
    (V1, SOURCE_MANUAL): V1_MANUAL_FORMULA,
}

VERSIONS = (V1, V2)
SOURCES = (SOURCE_APPROVAL, SOURCE_MANUAL)


def get_formula(version: str, source: str) -> FormulaVersion:
    """按「公式版本 + 判定来源」取口径表；V2 两个来源返回同一张表。"""
    try:
        return _FORMULAS[(version, source)]
    except KeyError:
        raise ValueError(f"未知的口径版本/来源：{version}/{source}") from None


def to_number(values: dict[str, Any], field: str) -> float:
    """把入参里的数值字段解析成 float；缺失或非数字直接拒绝，不允许静默当 0。"""
    raw = values.get(field)
    if raw is None or str(raw).strip() == "":
        raise ValueError(f"缺少数值字段：{field}")
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"字段 {field} 不是合法数值：{raw!r}") from None


def check_save_limits(area: float, grade: float) -> list[str]:
    """保存前硬校验：面积/品位超过现行上限（或非正数）一律不允许保存。

    历史项目的超限存量由 V1 冻结保留，但任何新的保存动作（登记、重录、重算）
    都必须过现行上限这一关。
    """
    violations: list[str] = []
    if area <= 0:
        violations.append("块段面积必须为正数")
    elif area > MAX_AREA_M2:
        violations.append(f"块段面积 {_fmt(area)} ㎡ 超出上限 {_fmt(MAX_AREA_M2)} ㎡，不允许保存")
    if grade < 0:
        violations.append("品位不允许为负数")
    elif grade > MAX_GRADE_PERCENT:
        violations.append(f"品位 {_fmt(grade)}% 超出上限 {_fmt(MAX_GRADE_PERCENT)}%，不允许保存")
    return violations


def classify(version: str, source: str, area: float, grade: float) -> dict[str, str]:
    """块段边界、品位区间、封边规则的唯一判定入口。"""
    formula = get_formula(version, source)
    boundary = formula.boundary_of(area)
    grade_band = formula.grade_of(grade)
    seal = formula.seal_of(area, grade)
    return {
        "formula_version": version,
        "source": source,
        "boundary_code": boundary.code,
        "boundary_label": boundary.label,
        "grade_code": grade_band.code,
        "grade_label": grade_band.label,
        "seal_rule": seal,
    }


def evaluate(version: str, source: str, values: dict[str, Any]) -> dict[str, Any]:
    """按指定版本/来源完成一次估算判定，并计算矿石量与金属量。

    矿石量 = 面积 × 厚度 × 体重；金属量 = 矿石量 × 品位。
    判定项全部经 :func:`classify` 得到，调用方拿不到第二套口径。
    ``enforce_limits=False`` 仅用于回放 V1 历史（旧上限更宽），保存路径不得使用。
    """
    area = to_number(values, "area")
    thickness = to_number(values, "thickness")
    grade = to_number(values, "grade")
    density = to_number(values, "density")

    formula = get_formula(version, source)
    if area <= 0 or thickness <= 0 or grade < 0 or density <= 0:
        raise ValueError("面积、厚度、矿石体重必须为正数，品位不允许为负数")
    if area > formula.max_area:
        raise ValueError(f"块段面积 {_fmt(area)} ㎡ 超出 {version.upper()} 定义域上限")
    if grade > formula.max_grade:
        raise ValueError(f"品位 {_fmt(grade)}% 超出 {version.upper()} 定义域上限")

    judgment = classify(version, source, area, grade)
    ore_tonnage = round(area * thickness * density, 2)
    metal_tonnage = round(ore_tonnage * grade / 100.0, 2)
    return {
        **judgment,
        "area": area,
        "thickness": thickness,
        "grade": grade,
        "density": density,
        "ore_tonnage": ore_tonnage,
        "metal_tonnage": metal_tonnage,
    }


def arbitrate(results: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """冲突裁决：同一地块段拿到多套结论时，按审批优先级取一套落地。

    ``results`` 形如 ``{"approval": 审批结论, "manual": 手工结论}``。
    审批结论存在即以其为准；只有手工结论时才用手工。判定口径字段（边界带/品位带/
    封边规则）或数值不一致都会记成冲突，但最终只输出一个结论。
    """
    if not results:
        raise ValueError("至少需要一个来源的估算结论")
    ordered = sorted(results, key=lambda src: SOURCE_PRIORITY.get(src, 0), reverse=True)
    winner_src = ordered[0]
    winner = dict(results[winner_src])

    conflicts: list[dict[str, str]] = []
    others = [src for src in ordered[1:] if src in results]
    compare_fields = (
        "boundary_label",
        "grade_label",
        "seal_rule",
        "ore_tonnage",
        "metal_tonnage",
    )
    field_labels = {
        "boundary_label": "块段边界",
        "grade_label": "品位区间",
        "seal_rule": "封边规则",
        "ore_tonnage": "矿石量",
        "metal_tonnage": "金属量",
    }
    for src in others:
        other = results[src]
        for field in compare_fields:
            if winner.get(field) != other.get(field):
                conflicts.append({
                    "field": field_labels[field],
                    "winner_source": winner_src,
                    "winner_value": str(winner.get(field)),
                    "loser_source": src,
                    "loser_value": str(other.get(field)),
                })
    winner["winning_source"] = winner_src
    winner["conflict_count"] = len(conflicts)
    return winner, conflicts


def _fmt(value: float) -> str:
    """人读数值：不带无意义小数，避免 2e+06 这种科学计数法提示。"""
    return f"{value:.4f}".rstrip("0").rstrip(".")
