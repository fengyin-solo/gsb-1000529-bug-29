"""估算结论的三条投影链路：块段台账、估算清单、储量图。

三条链路都从同一个落地结论（:func:`arbitrate` 的赢家）派生，任何一条都不允许
自己再算一遍。发布时在同一把发布锁内写三投影，并立即做一致性校验；任一步失败，
调用方按快照整体回滚，保证「三条链路结果必须一致」。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.reserve import criteria

# 三条链路必须一致的口径字段（台账/储量图投影使用中文键）。
CANONICAL_FIELDS = (
    "块段边界",
    "品位区间",
    "封边规则",
    "矿石量",
    "金属量",
)


def ledger_projection(block: dict[str, Any], conclusion: dict[str, Any]) -> dict[str, Any]:
    """块段台账投影：块段列表与估算详情共用这一份口径字段。"""
    return {
        "id": block["id"],
        "块段编号": block["块段编号"],
        "矿体名称": block["矿体名称"],
        "项目名称": block.get("项目名称", ""),
        "formula_version": block.get("formula_version", criteria.CURRENT_VERSION),
        "面积": conclusion["area"],
        "面积快照": conclusion["area"],
        "厚度": conclusion["thickness"],
        "品位": conclusion["grade"],
        "矿石体重": conclusion["density"],
        "资源类别": block.get("资源类别", ""),
        "块段边界": conclusion["boundary_label"],
        "品位区间": conclusion["grade_label"],
        "封边规则": conclusion["seal_rule"],
        "矿石量": conclusion["ore_tonnage"],
        "金属量": conclusion["metal_tonnage"],
        "winning_source": conclusion.get("winning_source", criteria.SOURCE_MANUAL),
        "结论编号": conclusion.get("conclusion_id") or conclusion.get("结论编号", ""),
        "估算时间": conclusion.get("estimated_at") or conclusion.get("估算时间", ""),
        "块段状态": block.get("status", "待估算"),
    }


def map_projection(block: dict[str, Any], conclusion: dict[str, Any]) -> dict[str, Any]:
    """储量图投影：图面只展示落地结论需要的口径与着色信息。"""
    return {
        "id": block["id"],
        "块段编号": block["块段编号"],
        "矿体名称": block["矿体名称"],
        "项目名称": block.get("项目名称", ""),
        "formula_version": block.get("formula_version", criteria.CURRENT_VERSION),
        "面积": conclusion["area"],
        "块段边界": conclusion["boundary_label"],
        "品位区间": conclusion["grade_label"],
        "封边规则": conclusion["seal_rule"],
        "矿石量": conclusion["ore_tonnage"],
        "金属量": conclusion["metal_tonnage"],
        "winning_source": conclusion.get("winning_source", criteria.SOURCE_MANUAL),
        "结论编号": conclusion.get("conclusion_id") or conclusion.get("结论编号", ""),
        "fill": _map_fill(conclusion),
        "stroke": _map_stroke(conclusion["seal_rule"]),
        "块段状态": block.get("status", "待估算"),
    }


def _map_fill(conclusion: dict[str, Any]) -> str:
    return {
        "低品位": "#dbeafe",
        "中品位": "#93c5fd",
        "高品位": "#2563eb",
    }.get(conclusion["grade_label"], "#e5e7eb")


def _map_stroke(seal_rule: str) -> str:
    # 封边规则直接决定图面线型：全封边实线、半封边虚线、不封边点线。
    return {
        "全封边": "solid",
        "半封边": "dashed",
        "不封边": "dotted",
    }.get(seal_rule, "none")


def build_projections(block: dict[str, Any], conclusion: dict[str, Any]) -> dict[str, Any]:
    """由同一个结论一次性生成三条链路的投影，保证字段同源。"""
    return {
        "ledger": ledger_projection(block, conclusion),
        "map": map_projection(block, conclusion),
    }


def consistency_report(
    blocks: list[dict[str, Any]],
    estimates: list[dict[str, Any]],
    maps: list[dict[str, Any]],
) -> dict[str, Any]:
    """对三条链路做一致性巡检，返回每个已发布块段的比对结果。

    * 台账/储量图的口径字段必须与最近一次落地结论完全一致；
    * 估算清单必须能按结论编号找到对应记录；
    * 三者公式版本、面积、面积快照必须对得上。
    """
    latest_estimate: dict[str, dict[str, Any]] = {}
    for estimate in estimates:
        cid = str(estimate.get("结论编号", ""))
        if not cid:
            continue
        prev = latest_estimate.get(cid)
        if prev is None or str(estimate.get("估算时间", "")) >= str(prev.get("估算时间", "")):
            latest_estimate[cid] = estimate

    rows: list[dict[str, Any]] = []
    inconsistent = 0
    published = [b for b in blocks if b.get("结论编号")]
    for block in published:
        block_id = int(block["id"])
        cid = str(block["结论编号"])
        map_row = next((m for m in maps
                        if int(m["id"]) == block_id and not m.get("_draft")), None)
        estimate = latest_estimate.get(cid)
        diffs: list[str] = []

        if map_row is None:
            diffs.append("储量图缺投影")
        if estimate is None:
            diffs.append("估算清单缺结论")

        # 储量图与台账逐条比同名字段（台账自身不参与）。
        if map_row is not None:
            for field in CANONICAL_FIELDS:
                if map_row.get(field) != block.get(field):
                    diffs.append(f"储量图.{field}={map_row.get(field)} ≠ 台账={block.get(field)}")
            if str(map_row.get("结论编号", "")) != cid:
                diffs.append(f"储量图.结论编号={map_row.get('结论编号')} ≠ {cid}")

        if estimate is not None:
            # 估算清单的中文口径字段与台账核对（area/grade 等英文键只是清单内部计算入参）。
            for cn in ("面积", "厚度", "品位", "矿石体重", *CANONICAL_FIELDS):
                if estimate.get(cn) is not None and estimate.get(cn) != block.get(cn):
                    diffs.append(f"估算清单.{cn}={estimate.get(cn)} ≠ 台账={block.get(cn)}")
            if str(estimate.get("formula_version", "")) != str(block.get("formula_version", "")):
                diffs.append("估算清单公式版本与台账不一致")
            if estimate.get("面积快照") != block.get("面积快照"):
                diffs.append("估算清单面积快照与台账不一致")

        rows.append({
            "id": block_id,
            "块段编号": block.get("块段编号"),
            "结论编号": cid,
            "ok": not diffs,
            "diffs": diffs,
        })
        if diffs:
            inconsistent += 1

    return {
        "ok": inconsistent == 0,
        "published": len(published),
        "inconsistent": inconsistent,
        "rows": rows,
    }


def snapshot_tables(
    store_rows: dict[str, list[dict[str, Any]]],
    table_names: tuple[str, ...],
) -> dict[str, list[dict[str, Any]]]:
    """事务前快照：发布失败时按它把涉及的表整体还原。"""
    return {name: deepcopy(store_rows.get(name, [])) for name in table_names}


def restore_tables(
    store_rows: dict[str, list[dict[str, Any]]],
    snapshot: dict[str, list[dict[str, Any]]],
) -> None:
    """按快照还原。

    必须就地改原 list（``clear/extend``），且尽量就地还原原 dict：发布前取出的
    ``block`` 等引用都指向原 list 里的行，重新绑定会让后续写入/回滚状态判断落到
    已脱离仓库的对象上（表现为「发布成功但台账没更新」或回滚后对象失联）。
    """
    for name, rows in snapshot.items():
        target = store_rows[name]
        # 就地还原仍存在的行，保持对象身份不变。
        for index, snapshot_row in enumerate(rows):
            if index < len(target):
                target[index].clear()
                target[index].update(deepcopy(snapshot_row))
            else:
                target.append(deepcopy(snapshot_row))
        del target[len(rows):]
