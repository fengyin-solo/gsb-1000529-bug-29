"""储量估算接口：块段台账、估算清单、储量图三条链路共用同一套口径。

* 块段边界 / 品位区间 / 封边规则由后端统一引擎判定，前端不自己算；
* 面积或品位超上限的保存请求返回 400；
* 「完成估算」只落草稿，「认定结果」在发布锁内同步三链路，失败整体回滚；
* 管理接口支持按新口径重算与历史面积快照迁移。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.reserve import ReserveService

router = APIRouter(prefix="/api/reserve", tags=["储量估算"])

service = ReserveService()
service.bootstrap()

LIST_FIELDS = ["块段编号", "矿体名称", "面积", "厚度", "品位", "矿石体重", "资源类别", "块段状态"]
STATUSES = ["待估算", "已估算", "待评审", "已认定"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按块段编号检索"),
    status: str | None = Query(default=None, description="待估算、已估算、待评审、已认定"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """块段台账列表：字段与估算详情、储量图来自同一份落地结论。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/estimates")
def list_estimates() -> dict[str, Any]:
    """估算清单：所有正式发布的结论。"""
    return {"module": "reserve", "items": service.list_estimates()}


@router.get("/drafts")
def list_drafts() -> dict[str, Any]:
    """草稿结论：完成估算但尚未认定发布的数据。"""
    return {"module": "reserve", "items": service.list_drafts()}


@router.get("/map")
def reserve_map() -> dict[str, Any]:
    """储量图投影：图元口径与台账逐条对应。"""
    return {"module": "reserve", "items": service.list_map()}


@router.get("/consistency")
def consistency() -> dict[str, Any]:
    """三链路一致性巡检结果（块段台账 × 估算清单 × 储量图）。"""
    return service.consistency()


@router.post("/recalculate")
def recalculate(payload: EntryPayload) -> ActionResult:
    """口径调整后已估算数据按新口径重算；历史项目（V1）跳过。"""
    dry_run = bool(payload.values.get("dry_run"))
    report = service.recalculate(reason="口径调整手动重算", dry_run=dry_run)
    return ActionResult(ok=True, message="重算完成", entry=report)


@router.post("/migrate-area-snapshots")
def migrate_area_snapshots() -> ActionResult:
    """历史块段缺面积快照的按估算时间迁移补齐（幂等）。"""
    report = service.migrate()
    return ActionResult(ok=True, message=f"面积快照迁移完成：补齐 {report['backfilled']} 条，跳过 {report['skipped']} 条", entry=report)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出储量估算清单：正式结论与三链路一致性一并带出。"""
    items, total = service.list_entries(page=1, size=10000)
    return {
        "module": "reserve",
        "total": total,
        "items": items,
        "estimates": service.list_estimates(),
        "consistency": service.consistency(),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条矿体块段明细及估算详情；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"矿体块段 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条矿体块段；面积/品位超上限或缺必填字段时说明原因，绝不静默落库。"""
    entry, missing, error = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if error:
        raise HTTPException(status_code=400, detail=error)
    return ActionResult(ok=True, message="矿体块段已登记", entry=entry)


@router.post("/{entry_id}/estimate", response_model=ActionResult)
def complete_estimate(entry_id: int, payload: EntryPayload) -> ActionResult:
    """完成估算：审批模型与手工公式同引擎判定，草稿保存，冲突按审批优先级裁决。"""
    entry, message = service.complete_estimate(entry_id, payload.values)
    if entry is None:
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交评审 / 认定结果；认定即发布，三链路不一致时后端回滚并返回失败。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
