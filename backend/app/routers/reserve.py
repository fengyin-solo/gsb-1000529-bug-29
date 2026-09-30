"""储量估算接口：维护矿体块段及统一口径后的估算投影。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.reserve import ReserveService

router = APIRouter(prefix="/api/reserve", tags=["储量估算"])

service = ReserveService()

LIST_FIELDS = ["块段编号", "矿体名称", "面积", "厚度", "品位", "矿石体重", "资源类别", "块段状态"]
STATUSES = ["待估算", "已估算", "待评审", "已认定"]
FORMULA_VERSIONS = ["v1.0-manual", "v2.0-unified"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按块段编号检索"),
    status: str | None = Query(default=None, description="待估算、已估算、待评审、已认定"),
    formula_version: str | None = Query(default=None, alias="formulaVersion", description="v1.0-manual 或 v2.0-unified"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按块段编号、状态与公式版本过滤储量台账；没有数据时返回空页。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        formula_version=formula_version,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/estimates")
def list_estimates() -> dict[str, Any]:
    """读取估算清单。清单中的校验码必须与台账、储量图一致。"""
    items = service.list_estimates()
    return {"module": "reserve_estimates", "total": len(items), "items": items}


@router.get("/map")
def list_map_projections() -> dict[str, Any]:
    """读取储量图投影。图面封边、品位区间和面积快照来自同一份发布结论。"""
    items = service.list_map_projections()
    return {"module": "reserve_map", "total": len(items), "items": items}


@router.post("/draft")
def save_draft(payload: EntryPayload) -> ActionResult:
    """按统一口径保存估算草稿；不更新正式台账结论和图面投影。"""
    entry_id = _int_payload(payload.values, "id", "块段ID")
    if entry_id is None:
        return ActionResult(ok=False, message="缺少块段ID，无法保存估算草稿")
    entry, message = service.save_draft(entry_id, payload.values)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/publish")
def publish_conclusion(payload: EntryPayload) -> ActionResult:
    """发布一个估算结论；并发时只允许一个版本落地，失败整体回滚。"""
    entry_id = _int_payload(payload.values, "id", "块段ID")
    if entry_id is None:
        return ActionResult(ok=False, message="缺少块段ID，无法发布储量结论")
    expected_revision = payload.values.get("expectedRevision") or payload.values.get("expected_revision")
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError):
            return ActionResult(ok=False, message="期望草稿版本必须是整数")
    entry, message = service.publish_conclusion(entry_id, expected_revision=expected_revision)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出储量估算台账及同版本的估算清单、储量图。"""
    items, total = service.list_entries(page=1, size=10000)
    return {
        "module": "reserve",
        "total": total,
        "items": items,
        "estimates": service.list_estimates(),
        "map": service.list_map_projections(),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条矿体块段明细；不存在时返回业务失败结果。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"矿体块段 {entry_id} 不存在或已归档")
    estimate = service.get_estimate(entry_id)
    map_projection = service.get_map_projection(entry_id)
    return {"entry": entry, "estimate": estimate, "map": map_projection}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条矿体块段；面积或品位超过上限时不允许保存。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        if all(field in service.REQUIRED_FIELDS for field in errors):
            message = f"缺少必填字段：{'、'.join(errors)}"
        else:
            message = "；".join(errors)
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="矿体块段已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行估算、评审和认定动作；完成估算会原子重算并同步三项投影。"""
    values = dict(payload.values)
    action = str(values.pop("action", "") or "").strip()
    entry, message = service.run_action(entry_id, action, values)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


def _int_payload(values: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None
