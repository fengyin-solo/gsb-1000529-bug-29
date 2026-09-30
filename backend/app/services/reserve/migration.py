"""历史数据迁移：口径调整后的存量处理。

两条规则（对应用户要求）：

* 历史项目维持此前公式版本（V1），不重算，只做补齐；
* 历史块段缺「面积快照」的，按估算时间迁移补齐——取面积沿革里不晚于估算时间的
  最近一条；沿革也没有时回退为当前面积，并在 ``迁移来源`` 上写明，便于审计。

迁移是幂等的：已有面积快照的块段不动，重复执行结果一致。
"""
from __future__ import annotations

from typing import Any

from app.services.reserve import criteria

# 迁移标记：补齐后的块段会带上这个时间，重复执行时据此跳过。
MIGRATED_AT = "2026-09-30"


def _history_at_or_before(block: dict[str, Any], estimated_at: str) -> float | None:
    """在面积沿革里取不晚于估算时间的最近一条面积。"""
    history = block.get("面积沿革") or []
    picked: tuple[str, float] | None = None
    for item in history:
        recorded_at = str(item.get("at", ""))
        if recorded_at <= estimated_at and (picked is None or recorded_at > picked[0]):
            picked = (recorded_at, float(item["area"]))
    return None if picked is None else picked[1]


def migrate_area_snapshots(rows: list[dict[str, Any]]) -> dict[str, int]:
    """给缺面积快照的历史块段按估算时间补齐，返回迁移统计。

    只处理 V1（历史）块段；V2 块段本就由新口径写入快照，不在迁移范围内。
    """
    backfilled = 0
    skipped = 0
    for block in rows:
        if block.get("formula_version") != criteria.V1:
            continue
        if block.get("面积快照") is not None:
            continue
        estimated_at = str(block.get("估算时间") or "")
        if not estimated_at:
            # 连估算时间都没有的历史脏数据不猜，留给人工处理，避免补错快照。
            skipped += 1
            block.setdefault("迁移备注", []).append("缺估算时间，面积快照需人工补齐")
            continue

        snapshot = _history_at_or_before(block, estimated_at)
        source = "面积沿革"
        if snapshot is None:
            snapshot = float(block["面积"])
            source = "当前面积回退"
        block["面积快照"] = snapshot
        block["面积快照迁移时间"] = MIGRATED_AT
        block["迁移来源"] = source
        backfilled += 1

    return {"backfilled": backfilled, "skipped": skipped}
