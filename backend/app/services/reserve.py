"""储量估算业务规则。

块段边界、品位区间、封边规则只在本模块内裁决：

* 审批模型与手工公式冲突时，审批模型优先；
* 封边规则由最终边界与品位区间按同一张映射表得出，避免按行串位；
* 面积、品位超过上限时，草稿和正式结论都不能落库；
* 历史项目沿用 v1 公式版本，口径调整后的非历史项目统一使用 v2；
* 结论一次写入块段台账、估算清单、储量图三处，任一失败整体回滚。
"""
from __future__ import annotations

import copy
import hashlib
import threading
from datetime import date, datetime
from typing import Any, Callable

from app.store import store

MODULE = "reserve"
ESTIMATE_MODULE = "reserve_estimates"
MAP_MODULE = "reserve_map"

REQUIRED_FIELDS = ["块段编号", "矿体名称", "面积"]
STATUS_ORDER = ["待估算", "已估算", "待评审", "已认定"]
ACTION_RULES = {"完成估算": "已估算", "提交评审": "待评审", "认定结果": "已认定"}
NEGATIVE_ACTIONS = []

LEGACY_FORMULA_VERSION = "v1.0-manual"
CURRENT_FORMULA_VERSION = "v2.0-unified"
HISTORICAL_CUTOFF = date(2026, 9, 30)

# 业务上限。面积单位为平方米，品位单位为百分比。
DEFAULT_AREA_LIMIT = 1_000_000.0
DEFAULT_GRADE_LIMIT = 5.0

BOUNDARIES = ["自然边界", "工程边界", "外推边界", "审批边界"]
GRADE_BUCKETS: dict[str, list[tuple[str, float, float | None]]] = {
    LEGACY_FORMULA_VERSION: [
        ("表外品位", 0.0, 0.3),
        ("低品位", 0.3, 0.5),
        ("工业品位", 0.5, 3.0),
        ("高品位", 3.0, 8.0),
    ],
    CURRENT_FORMULA_VERSION: [
        ("表外品位", 0.0, 0.3),
        ("低品位", 0.3, 0.5),
        ("工业品位", 0.5, 3.0),
        ("高品位", 3.0, 5.0),
    ],
}
EDGE_RULES = {
    ("自然边界", "low"): "自然封边",
    ("自然边界", "economic"): "自然封边",
    ("工程边界", "low"): "有限外推封边",
    ("工程边界", "economic"): "工程封边",
    ("外推边界", "low"): "禁止封边",
    ("外推边界", "economic"): "无限外推封边",
    ("审批边界", "low"): "审批封边",
    ("审批边界", "economic"): "审批封边",
}

FIELD_ALIASES = {
    "area": ["area", "blockArea", "block_area", "面积"],
    "thickness": ["thickness", "blockThickness", "block_thickness", "厚度"],
    "grade": ["grade", "gradePercent", "grade_percent", "品位"],
    "density": ["density", "tonnageFactor", "tonnage_factor", "矿石体重", "体重"],
    "boundary": ["blockBoundary", "block_boundary", "boundary", "块段边界", "边界"],
    "interval": ["gradeInterval", "grade_interval", "gradeRange", "grade_range", "gradeBand", "interval", "品位区间"],
    "edge": ["edgeRule", "edge_rule", "sealingRule", "sealing_rule", "edge", "封边规则", "封边"],
}
RULE_SOURCES = [
    ("审批模型", 30, ["approvalModel", "approval_model", "approvedModel", "审批模型", "approval", "审批结论", "审批结果", "审批"]),
    ("估算模型", 20, ["estimationModel", "estimation_model", "估算模型", "model"]),
    ("手工公式", 10, ["manualFormula", "manual_formula", "手工公式", "manual", "手工"]),
]
LEDGER_RULE_FIELDS = {
    "area": "面积",
    "thickness": "厚度",
    "grade": "品位",
    "density": "矿石体重",
    "boundary": "块段边界",
    "interval": "品位区间",
    "edge": "封边规则",
}

class ReserveRuleError(ValueError):
    """规则校验失败；消息可直接返回给前端。"""


class ReserveService:
    @classmethod
    def reset_store(cls) -> None:
        """用种子数据重建内存仓库，主要供接口测试隔离使用。"""
        store.reset()

    def __init__(self) -> None:
        self._block_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._migrated = False
        self._bootstrap_migration()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        formula_version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            self._migrate_entry(row, in_transaction=False)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("块段编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if formula_version:
            rows = [row for row in rows if row.get("formulaVersion") == formula_version]
        if page < 1:
            page = 1
        if size < 1:
            size = 20
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._migrate_entry(entry)
        return entry

    def list_estimates(self) -> list[dict[str, Any]]:
        for row in store.rows(MODULE):
            self._migrate_entry(row, in_transaction=False)
        return list(store.rows(ESTIMATE_MODULE))

    def get_estimate(self, block_id: int) -> dict[str, Any] | None:
        self.get_entry(block_id)
        return self._find_projection(ESTIMATE_MODULE, block_id)

    def list_map_projections(self) -> list[dict[str, Any]]:
        for row in store.rows(MODULE):
            self._migrate_entry(row, in_transaction=False)
        return list(store.rows(MAP_MODULE))

    def get_map_projection(self, block_id: int) -> dict[str, Any] | None:
        self.get_entry(block_id)
        return self._find_projection(MAP_MODULE, block_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing

        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["块段状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["formulaVersion"] = None
        entry["revision"] = 0
        entry["publishedRevision"] = 0

        for field in ("厚度", "品位", "矿石体重", "资源类别", "块段边界"):
            if values.get(field) is not None:
                entry[field] = values.get(field)

        try:
            area = self._number(entry.get("面积"), "面积")
            grade = self._optional_number(entry.get("品位"), "品位")
            if grade is not None:
                self._validate_limits(area, grade, values)
            elif area <= 0 or area > self._limit_value(values, {}, ["areaUpperLimit", "面积上限"], DEFAULT_AREA_LIMIT):
                raise ReserveRuleError(
                    f"块段面积必须大于 0 且不超过 {self._limit_value(values, {}, ['areaUpperLimit', '面积上限'], DEFAULT_AREA_LIMIT)}"
                )
            entry["面积"] = area
            if grade is not None:
                entry["品位"] = grade
        except ReserveRuleError as exc:
            return None, [str(exc)]

        rows.append(entry)
        return entry, []

    def save_draft(self, entry_id: int, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        try:
            return self._with_block_transaction(
                entry_id,
                lambda entry: self._save_estimate(entry, values, publish=False),
            )
        except ReserveRuleError as exc:
            return None, str(exc)

    def publish_conclusion(
        self,
        entry_id: int,
        *,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"矿体块段 {entry_id} 不存在或已归档"
        self._migrate_entry(entry)
        observed_published_revision = entry.get("publishedRevision")
        observed_draft_revision = (entry.get("estimateDraft") or {}).get("revision")
        lock = self._block_lock(entry_id)

        if not lock.acquire(blocking=False):
            return None, "该块段已有储量结论正在发布，请等待本次发布完成"

        try:
            def work(current: dict[str, Any]) -> tuple[dict[str, Any], str]:
                if current.get("publishedRevision") != observed_published_revision:
                    raise ReserveRuleError("块段结论已被其他发布更新，请刷新后重试")
                current_draft_revision = (current.get("estimateDraft") or {}).get("revision")
                if observed_draft_revision is not None and current_draft_revision != observed_draft_revision:
                    raise ReserveRuleError("估算草稿已变化，请基于最新草稿重新发布")
                return self._publish_draft(current, expected_revision)

            try:
                return self._with_block_transaction_locked(entry_id, work)
            except ReserveRuleError as exc:
                return None, str(exc)
        finally:
            lock.release()

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"矿体块段 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于储量估算可执行范围"

        self._migrate_entry(entry, in_transaction=False)
        if action == "完成估算":
            values = values or {}
            values.setdefault("publish", True)
            return self.save_draft(entry_id, values)

        target = ACTION_RULES[action]
        current_index = STATUS_ORDER.index(entry["status"]) if entry.get("status") in STATUS_ORDER else -1
        target_index = STATUS_ORDER.index(target)
        if action == "提交评审" and entry.get("status") != "已估算":
            return None, "只有已估算块段可以提交评审"
        if action == "认定结果" and entry.get("status") != "待评审":
            return None, "只有待评审块段可以认定结果"
        if target_index <= current_index:
            return None, f"块段当前为「{entry.get('status')}」，不能重复执行「{action}」"

        entry["status"] = target
        entry["块段状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        self._sync_projection_status(entry)
        return entry, f"矿体块段已{action}"

    # ---- 迁移与统一口径 ----

    def _bootstrap_migration(self) -> None:
        if self._migrated:
            return
        for row in store.rows(MODULE):
            self._migrate_entry(row, in_transaction=False)
        self._migrated = True

    def _migrate_entry(self, entry: dict[str, Any], *, in_transaction: bool = False) -> None:
        """补齐历史面积快照，并让已有结论的三条投影保持一致。"""
        marker = f"{entry.get('formulaVersion')}:{entry.get('publishedRevision')}"
        if entry.get("ruleMigrationVersion") == "2.0" and entry.get("ruleMigrationMarker") == marker:
            return
        status = entry.get("status")
        if status not in {"已估算", "待评审", "已认定"}:
            entry.setdefault("revision", 0)
            entry.setdefault("publishedRevision", 0)
            entry["ruleMigrationVersion"] = "2.0"
            entry["ruleMigrationMarker"] = marker
            return

        try:
            area = self._number(entry.get("面积"), "面积")
            thickness = self._optional_number(entry.get("厚度"), "厚度")
            grade = self._optional_number(entry.get("品位"), "品位")
            density = self._optional_number(entry.get("矿石体重"), "矿石体重")
        except ReserveRuleError:
            return
        if thickness is None or grade is None or density is None:
            return

        historical = self._is_historical(entry)
        requested_version = str(entry.get("formulaVersion") or "").strip()
        version = requested_version if requested_version in GRADE_BUCKETS else (
            LEGACY_FORMULA_VERSION if historical else CURRENT_FORMULA_VERSION
        )
        entry["formulaVersion"] = version
        entry.setdefault("revision", int(entry.get("publishedRevision") or 1))
        entry.setdefault("publishedRevision", entry["revision"])

        area_snapshot = self._optional_number(entry.get("面积快照"), "面积快照")
        snapshot_migrated = False
        if area_snapshot is None:
            area_snapshot = area
            entry["面积快照"] = area_snapshot
            entry["areaSnapshot"] = area_snapshot
            entry["snapshotMigratedAt"] = str(entry.get("estimatedAt") or self._now())
            snapshot_migrated = True

        try:
            resolved = self._resolve_rules(entry, entry, version)
            result = self._build_result(entry, resolved, version, revision=int(entry["publishedRevision"] or 1))
            result["面积快照"] = area_snapshot
            result["areaSnapshot"] = area_snapshot
            if snapshot_migrated:
                result["snapshotMigrated"] = True
            self._write_projections(entry, result, published=True, set_status=False)
            entry["ruleMigrationVersion"] = "2.0"
            entry["ruleMigrationMarker"] = marker
            draft = self._draft_from_result(result)
            entry["estimateDraft"] = draft
        except ReserveRuleError:
            # 示例或旧数据缺关键字段时不阻断模块启动；执行重算时会重新返回明确错误。
            return

    def _save_estimate(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        *,
        publish: bool,
    ) -> tuple[dict[str, Any], str]:
        should_publish = bool(publish or values.get("publish") is True or values.get("发布") is True)
        historical = self._is_historical(entry, values)
        version = self._formula_version(entry, values, historical)
        resolved = self._resolve_rules(values, entry, version)
        revision = int(entry.get("revision") or 0) + 1

        result = self._build_result(entry, resolved, version, revision=revision)
        snapshot_area = self._optional_number(entry.get("面积快照"), "面积快照")
        if snapshot_area is None:
            snapshot_area = resolved["area"]
        result["面积快照"] = snapshot_area
        result["areaSnapshot"] = snapshot_area
        self._apply_inputs_to_entry(entry, resolved)
        entry["面积快照"] = snapshot_area
        entry["areaSnapshot"] = snapshot_area
        entry["formulaVersion"] = version
        entry["historicalProject"] = historical
        entry["revision"] = revision
        entry["estimateDraft"] = self._draft_from_result(result)
        entry["lastRuleConflicts"] = result["ruleConflicts"]

        if should_publish:
            if not historical or snapshot_area is None:
                result["面积快照"] = resolved["area"]
                result["areaSnapshot"] = resolved["area"]
                entry["面积快照"] = resolved["area"]
                entry["areaSnapshot"] = resolved["area"]
            self._publish_result(entry, result)
            return entry, "储量估算已按统一口径重算，三项投影已同步一致"

        entry["pending"] = entry.get("status") != STATUS_ORDER[-1]
        entry["abnormal"] = entry.get("abnormal", False)
        return entry, "估算草稿已保存；正式发布后才会更新三项投影"

    def _publish_draft(
        self,
        entry: dict[str, Any],
        expected_revision: int | None,
    ) -> tuple[dict[str, Any], str]:
        draft = entry.get("estimateDraft")
        if not isinstance(draft, dict):
            raise ReserveRuleError("尚未保存估算草稿，不能发布空结论")
        revision = int(draft.get("revision") or 0)
        if expected_revision is not None and revision != expected_revision:
            raise ReserveRuleError("草稿版本已变化，请基于最新版本重新发布")

        # 发布前再走一次上限与规则校验，防止草稿保存后配置被改小。
        version = str(draft.get("formulaVersion") or entry.get("formulaVersion") or CURRENT_FORMULA_VERSION)
        resolved = self._resolve_rules(draft, entry, version)
        result = self._build_result(entry, resolved, version, revision=revision)
        self._publish_result(entry, result)
        return entry, "储量结论已发布，块段台账、估算清单与储量图保持一致"

    def _publish_result(self, entry: dict[str, Any], result: dict[str, Any]) -> None:
        if entry.get("status") == "已认定":
            raise ReserveRuleError("已认定结论不能直接重算发布，请先发起认定变更")
        self._write_projections(entry, result, published=True, set_status=True)
        entry["publishedRevision"] = result["revision"]
        entry["publishedAt"] = result["publishedAt"]
        entry.setdefault("estimatedAt", result["estimatedAt"])
        entry["estimateDraft"] = self._draft_from_result(result)

    def _resolve_rules(
        self,
        values: dict[str, Any],
        entry: dict[str, Any],
        version: str,
    ) -> dict[str, Any]:
        selected, conflicts, sources = self._select_rule_inputs(values, entry)
        area_limit = self._limit_value(values, entry, ["areaUpperLimit", "面积上限"], DEFAULT_AREA_LIMIT)
        legacy_grade_limit = GRADE_BUCKETS[LEGACY_FORMULA_VERSION][-1][2] or DEFAULT_GRADE_LIMIT
        configured_grade_limit = self._limit_value(
            values,
            entry,
            ["gradeUpperLimit", "品位上限"],
            legacy_grade_limit if version == LEGACY_FORMULA_VERSION else DEFAULT_GRADE_LIMIT,
        )
        grade_limit = min(configured_grade_limit, DEFAULT_GRADE_LIMIT)

        area = self._number(selected["area"], "面积")
        thickness = self._number(selected["thickness"], "厚度")
        grade = self._number(selected["grade"], "品位")
        density = self._number(selected["density"], "矿石体重")
        self._validate_limits(area, grade, values={}, area_limit=area_limit, grade_limit=grade_limit)
        for name, value in (("厚度", thickness), ("矿石体重", density)):
            if value <= 0:
                raise ReserveRuleError(f"{name}必须大于 0")

        boundary = str(selected["boundary"] or "工程边界").strip()
        if boundary not in BOUNDARIES:
            raise ReserveRuleError(f"不支持的块段边界「{boundary}」")

        interval = str(selected["interval"] or self._grade_interval(grade, version)).strip()
        self._validate_grade_interval(grade, interval, version)

        canonical_edge = self._edge_rule(boundary, interval)
        declared_edge = selected["edge"]
        if declared_edge and str(declared_edge).strip() != canonical_edge:
            edge_source = sources.get("edge")
            conflicts.append({
                "field": "封边规则",
                "requested": str(declared_edge),
                "resolved": canonical_edge,
                "source": edge_source or "统一口径",
                "reason": "封边规则按最终块段边界与品位区间重新判定",
            })

        return {
            "area": area,
            "thickness": thickness,
            "grade": grade,
            "density": density,
            "boundary": boundary,
            "interval": interval,
            "edge": canonical_edge,
            "conflicts": conflicts,
            "sources": sources,
            "area_limit": area_limit,
            "grade_limit": grade_limit,
        }

    def _select_rule_inputs(
        self,
        values: dict[str, Any],
        entry: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
        candidates: dict[str, list[tuple[int, str, Any]]] = {field: [] for field in FIELD_ALIASES}
        source_docs: list[tuple[str, int, dict[str, Any]]] = []

        flat_source = self._source_name(
            values.get("ruleSource")
            or values.get("规则来源")
            or ("审批模型" if values.get("approvalWins") is True or values.get("审批优先") is True else None)
        )
        flat_priority = dict((name, priority) for name, priority, _ in RULE_SOURCES).get(flat_source, 15)

        for field, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                if alias in values and values.get(alias) is not None:
                    source_name = flat_source or "手工录入"
                    candidates[field].append((flat_priority, source_name, values[alias]))
                    break

        seen_source_names: set[str] = set()
        for source_name, priority, keys in RULE_SOURCES:
            for key in keys:
                source_value = values.get(key)
                if isinstance(source_value, dict) and source_name not in seen_source_names:
                    source_docs.append((source_name, priority, source_value))
                    seen_source_names.add(source_name)
                    break

        for source_name, priority, source_value in source_docs:
            for field, aliases in FIELD_ALIASES.items():
                for alias in aliases:
                    if alias in source_value and source_value.get(alias) is not None:
                        candidates[field].append((priority, source_name, source_value[alias]))
                        break

        selected: dict[str, Any] = {}
        sources: dict[str, str] = {}
        conflicts: list[dict[str, Any]] = []
        for field in FIELD_ALIASES:
            ordered = sorted(candidates[field], key=lambda item: item[0], reverse=True)
            if ordered:
                priority, source_name, winner = ordered[0]
                selected[field] = winner
                sources[field] = source_name
                for candidate_priority, candidate_name, candidate_value in ordered[1:]:
                    if str(candidate_value) == str(winner):
                        continue
                    conflicts.append({
                        "field": LEDGER_RULE_FIELDS[field],
                        "prioritySource": source_name,
                        "conflictingSource": candidate_name,
                        "requested": candidate_value,
                        "resolved": winner,
                        "samePriority": candidate_priority == priority,
                    })
            else:
                selected[field] = self._entry_value(entry, field)
                if selected[field] is not None:
                    sources[field] = "块段台账"

        return selected, conflicts, sources

    def _build_result(
        self,
        entry: dict[str, Any],
        resolved: dict[str, Any],
        version: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        area = resolved["area"]
        thickness = resolved["thickness"]
        grade = resolved["grade"]
        density = resolved["density"]
        boundary = resolved["boundary"]
        interval = resolved["interval"]
        edge = resolved["edge"]

        volume = self._round(area * thickness)
        tonnage = self._round(volume * density)
        metal = self._round(tonnage * grade / 100)
        now = self._now()
        if entry.get("publishedRevision"):
            estimated_at = str(entry.get("estimatedAt") or entry.get("估算时间") or now)
            published_at = str(entry.get("publishedAt") or entry.get("发布时间") or estimated_at)
        else:
            estimated_at = str(entry.get("estimatedAt") or entry.get("估算时间") or now)
            published_at = now
        checksum = self._checksum([
            str(entry.get("id")),
            version,
            str(area),
            str(thickness),
            str(grade),
            str(density),
            boundary,
            interval,
            edge,
            str(revision),
        ])

        return {
            "revision": revision,
            "块段编号": entry.get("块段编号"),
            "矿体名称": entry.get("矿体名称"),
            "面积": area,
            "面积快照": area,
            "areaSnapshot": area,
            "厚度": thickness,
            "品位": grade,
            "矿石体重": density,
            "资源类别": entry.get("资源类别"),
            "块段边界": boundary,
            "品位区间": interval,
            "封边规则": edge,
            "formulaVersion": version,
            "公式版本": version,
            "approvalPriority": bool(resolved["sources"]),
            "ruleSources": resolved["sources"],
            "ruleConflicts": resolved["conflicts"],
            "矿石量": volume,
            "储量": tonnage,
            "金属量": metal,
            "estimatedAt": estimated_at,
            "估算时间": estimated_at,
            "publishedAt": published_at,
            "发布时间": published_at,
            "conclusionChecksum": checksum,
            "校验码": checksum,
        }

    def _write_projections(
        self,
        entry: dict[str, Any],
        result: dict[str, Any],
        *,
        published: bool,
        set_status: bool,
    ) -> None:
        if set_status:
            entry["status"] = "已估算"
            entry["块段状态"] = "已估算"
            entry["pending"] = True
            entry["abnormal"] = False
            entry["estimatedAt"] = result["estimatedAt"]
            entry["publishedAt"] = result["publishedAt"]

        for key, value in result.items():
            if key in {"revision", "estimatedAt", "publishedAt"}:
                continue
            entry[key] = value
        entry["projectionRevision"] = result["revision"] if published else entry.get("projectionRevision")
        entry["conclusionStatus"] = "已发布" if published else entry.get("conclusionStatus", "草稿")

        estimate = {
            "id": int(entry["id"]),
            "块段ID": int(entry["id"]),
            "估算编号": f"EST-{int(entry['id']):04d}-{int(result['revision']):03d}",
            "块段编号": result["块段编号"],
            "矿体名称": result["矿体名称"],
            "面积": result["面积"],
            "面积快照": result["面积快照"],
            "厚度": result["厚度"],
            "品位": result["品位"],
            "矿石体重": result["矿石体重"],
            "块段边界": result["块段边界"],
            "品位区间": result["品位区间"],
            "封边规则": result["封边规则"],
            "公式版本": result["公式版本"],
            "矿石量": result["矿石量"],
            "储量": result["储量"],
            "金属量": result["金属量"],
            "估算时间": result["估算时间"],
            "发布时间": result["发布时间"],
            "revision": result["revision"],
            "conclusionStatus": "已发布" if published else "草稿",
            "conclusionChecksum": result["conclusionChecksum"],
            "snapshotMigrated": result.get("snapshotMigrated", False),
            "ruleConflicts": copy.deepcopy(result["ruleConflicts"]),
        }
        map_projection = {
            "id": int(entry["id"]),
            "块段ID": int(entry["id"]),
            "图元编号": f"MAP-{int(entry['id']):04d}",
            "块段编号": result["块段编号"],
            "矿体名称": result["矿体名称"],
            "面积快照": result["面积快照"],
            "块段边界": result["块段边界"],
            "品位区间": result["品位区间"],
            "封边规则": result["封边规则"],
            "储量": result["储量"],
            "公式版本": result["公式版本"],
            "图面状态": entry.get("status"),
            "投影版本": result["revision"] if published else entry.get("投影版本", result["revision"]),
            "发布时间": result["发布时间"],
            "conclusionChecksum": result["conclusionChecksum"],
        }
        self._upsert_projection(ESTIMATE_MODULE, int(entry["id"]), estimate)
        self._upsert_projection(MAP_MODULE, int(entry["id"]), map_projection)

        # 回写后立即用同一校验码核对三条链路。
        estimate_row = self._find_projection(ESTIMATE_MODULE, int(entry["id"]))
        map_row = self._find_projection(MAP_MODULE, int(entry["id"]))
        ledger_checksum = entry.get("conclusionChecksum")
        estimate_checksum = estimate_row.get("conclusionChecksum") if estimate_row else None
        map_checksum = map_row.get("conclusionChecksum") if map_row else None
        if not (ledger_checksum == estimate_checksum == map_checksum):
            raise ReserveRuleError("三项投影校验码不一致，本次结论未落地")

    def _sync_projection_status(self, entry: dict[str, Any]) -> None:
        for module, prefix in ((ESTIMATE_MODULE, "EST"), (MAP_MODULE, "MAP")):
            row = self._find_projection(module, int(entry["id"]))
            if row is None:
                continue
            if module == ESTIMATE_MODULE:
                row["conclusionStatus"] = entry["status"]
            else:
                row["图面状态"] = entry["status"]
            row["公式版本"] = entry.get("formulaVersion") or row.get("公式版本")
            row["conclusionChecksum"] = entry.get("conclusionChecksum") or row.get("conclusionChecksum")

    def _with_block_transaction(
        self,
        entry_id: int,
        operation: Callable[[dict[str, Any]], tuple[dict[str, Any], str]],
    ) -> tuple[dict[str, Any], str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"矿体块段 {entry_id} 不存在或已归档"

        with self._block_lock(entry_id):
            return self._with_block_transaction_locked(entry_id, operation)

    def _with_block_transaction_locked(
        self,
        entry_id: int,
        operation: Callable[[dict[str, Any]], tuple[dict[str, Any], str]],
    ) -> tuple[dict[str, Any], str]:
        affected = [MODULE, ESTIMATE_MODULE, MAP_MODULE]
        snapshots = {name: copy.deepcopy(store.rows(name)) for name in affected}
        try:
            current = store.find(MODULE, entry_id)
            if current is None:
                raise ReserveRuleError(f"矿体块段 {entry_id} 不存在或已归档")
            return operation(current)
        except Exception:
            for name in affected:
                store.rows(name)[:] = copy.deepcopy(snapshots[name])
            raise

    def _block_lock(self, entry_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._block_locks.get(entry_id)
            if lock is None:
                lock = threading.Lock()
                self._block_locks[entry_id] = lock
            return lock

    def _upsert_projection(self, module: str, block_id: int, row: dict[str, Any]) -> None:
        rows = store.rows(module)
        for index, existing in enumerate(rows):
            if existing.get("块段ID") == block_id or existing.get("id") == block_id:
                rows[index] = row
                return
        rows.append(row)

    def _find_projection(self, module: str, block_id: int) -> dict[str, Any] | None:
        for row in store.rows(module):
            if row.get("块段ID") == block_id or row.get("id") == block_id:
                return row
        return None

    def _apply_inputs_to_entry(self, entry: dict[str, Any], resolved: dict[str, Any]) -> None:
        entry.update({
            "面积": resolved["area"],
            "厚度": resolved["thickness"],
            "品位": resolved["grade"],
            "矿石体重": resolved["density"],
            "块段边界": resolved["boundary"],
            "品位区间": resolved["interval"],
            "封边规则": resolved["edge"],
        })

    def _draft_from_result(self, result: dict[str, Any]) -> dict[str, Any]:
        keep = [
            "revision", "面积", "厚度", "品位", "矿石体重", "块段边界", "品位区间",
            "封边规则", "formulaVersion", "矿石量", "储量", "金属量",
            "estimatedAt", "publishedAt", "conclusionChecksum", "ruleConflicts",
            "ruleSources",
        ]
        return {key: copy.deepcopy(result[key]) for key in keep if key in result}

    def _formula_version(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        historical: bool,
    ) -> str:
        requested = str(values.get("formulaVersion") or values.get("公式版本") or "").strip()
        entry_version = str(entry.get("formulaVersion") or "").strip()
        known_legacy = {"v1", "legacy", LEGACY_FORMULA_VERSION, "手工公式", "历史版本"}
        known_current = {"v2", "unified", CURRENT_FORMULA_VERSION, "统一口径"}
        explicit = "formulaVersion" in values or "公式版本" in values
        if requested in known_legacy:
            if not historical:
                raise ReserveRuleError("非历史项目必须使用统一后的 v2 公式版本")
            return LEGACY_FORMULA_VERSION
        if requested in known_current:
            if historical:
                raise ReserveRuleError("历史项目必须维持此前 v1 公式版本")
            return CURRENT_FORMULA_VERSION
        if explicit and requested:
            raise ReserveRuleError(f"未知公式版本「{requested}」")
        if historical:
            entry_version = str(entry.get("formulaVersion") or "").strip()
            return entry_version if entry_version in GRADE_BUCKETS else LEGACY_FORMULA_VERSION
        entry_version = str(entry.get("formulaVersion") or "").strip()
        return entry_version if entry_version in GRADE_BUCKETS else CURRENT_FORMULA_VERSION

    def _is_historical(self, entry: dict[str, Any], values: dict[str, Any] | None = None) -> bool:
        values = values or {}
        if values.get("historicalProject") is not None:
            return bool(values["historicalProject"])
        if values.get("历史项目") is not None:
            return bool(values["历史项目"])
        if entry.get("historicalProject") is not None:
            return bool(entry["historicalProject"])
        if entry.get("legacyProject") is not None:
            return bool(entry["legacyProject"])
        estimated_at = self._parse_date(entry.get("estimatedAt") or entry.get("估算时间"))
        return estimated_at is not None and estimated_at < HISTORICAL_CUTOFF

    def _validate_limits(
        self,
        area: float,
        grade: float,
        values: dict[str, Any],
        *,
        area_limit: float | None = None,
        grade_limit: float | None = None,
    ) -> None:
        area_limit = area_limit if area_limit is not None else self._limit_value(values, {}, ["areaUpperLimit", "面积上限"], DEFAULT_AREA_LIMIT)
        grade_limit = grade_limit if grade_limit is not None else self._limit_value(values, {}, ["gradeUpperLimit", "品位上限"], DEFAULT_GRADE_LIMIT)
        if area <= 0:
            raise ReserveRuleError("块段面积必须大于 0")
        if grade < 0:
            raise ReserveRuleError("品位不能为负数")
        if area > area_limit:
            raise ReserveRuleError(f"块段面积 {area} 超出上限 {area_limit}，不允许保存")
        if grade > grade_limit:
            raise ReserveRuleError(f"块段品位 {grade}% 超出上限 {grade_limit}%，不允许保存")

    def _limit_value(
        self,
        values: dict[str, Any],
        entry: dict[str, Any],
        keys: list[str],
        default: float,
    ) -> float:
        for key in keys:
            if values.get(key) is not None:
                return self._number(values[key], key)
            if entry.get(key) is not None:
                return self._number(entry[key], key)
        return default

    def _grade_interval(self, grade: float, version: str) -> str:
        for label, lower, upper in GRADE_BUCKETS[version]:
            if upper is None:
                continue
            if grade >= lower and (grade < upper or (label == "高品位" and grade == upper)):
                return label
        upper = GRADE_BUCKETS[version][-1][2]
        raise ReserveRuleError(f"品位 {grade}% 不在 {version} 允许区间内，上限为 {upper}%")

    def _validate_grade_interval(self, grade: float, interval: str, version: str) -> None:
        allowed = [label for label, _, _ in GRADE_BUCKETS[version]]
        if interval not in allowed:
            raise ReserveRuleError(f"公式版本 {version} 不支持品位区间「{interval}」")
        actual = self._grade_interval(grade, version)
        if actual != interval:
            raise ReserveRuleError(f"品位 {grade}% 应归属「{actual}」，不能保存为「{interval}」")

    def _edge_rule(self, boundary: str, interval: str) -> str:
        group = "economic" if interval in {"工业品位", "高品位"} else "low"
        edge = EDGE_RULES.get((boundary, group))
        if edge is None:
            raise ReserveRuleError(f"无法根据边界「{boundary}」和品位区间「{interval}」判定封边规则")
        return edge

    def _entry_value(self, entry: dict[str, Any], field: str) -> Any:
        for alias in FIELD_ALIASES[field]:
            if entry.get(alias) is not None:
                return entry[alias]
        return None

    def _source_name(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        for name, _, aliases in RULE_SOURCES:
            if text == name or text in aliases:
                return name
        return None

    def _optional_number(self, value: Any, field: str) -> float | None:
        if value is None or str(value).strip() == "":
            return None
        return self._number(value, field)

    def _number(self, value: Any, field: str) -> float:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ReserveRuleError(f"{field}不能为空")
        if isinstance(value, bool):
            raise ReserveRuleError(f"{field}必须是数值")
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            text = str(value).strip().replace(",", "").rstrip("%㎡米t吨 ")
            try:
                number = float(text)
            except ValueError as exc:
                raise ReserveRuleError(f"{field}必须是数值，当前为「{value}」") from exc
        return number

    def _parse_date(self, value: Any) -> date | None:
        if not value:
            return None
        text = str(value).strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    def _checksum(self, parts: list[str]) -> str:
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    def _round(self, value: float) -> float:
        return round(value + 0.0, 6)

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")
