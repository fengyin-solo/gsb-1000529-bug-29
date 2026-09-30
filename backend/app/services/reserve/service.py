"""储量估算服务：块段台账、估算清单、储量图三条链路的统一入口。

关键约束（与需求逐条对应）：

* 块段边界 / 品位区间 / 封边规则只经 ``criteria.classify`` 判定，审批模型与手工
  公式在 V2 共用一张口径表；数值结论冲突时按审批优先级裁决；
* 保存前过 ``criteria.check_save_limits``，面积或品位超现行上限一律拒存；
* 历史项目（V1）维持旧公式版本，重算时跳过；
* 估算完成只落草稿投影；「认定结果」即发布，在全局发布锁内把同一结论同步写回
  块段台账、估算清单、储量图，发布失败整体回滚（含草稿与三投影）；
* 并发发布只允许一个结论落地，第二个发布拿到锁冲突直接失败。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import date
from typing import Any

from app.services.reserve import criteria, projections, seed_data
from app.services.reserve.migration import MIGRATED_AT, migrate_area_snapshots
from app.store import store

BLOCKS = "reserve"
ESTIMATES = "reserve_estimate"
MAPS = "reserve_map"
PUBLISH_TABLES = (BLOCKS, ESTIMATES, MAPS)

REQUIRED_FIELDS = ["块段编号", "矿体名称", "面积"]
STATUS_ORDER = ["待估算", "已估算", "待评审", "已认定"]
# 完成估算 -> 草稿（不入正式链路）；提交评审/认定结果走状态流转；认定即发布。
ACTION_STATUS = {"完成估算": "已估算", "提交评审": "待评审"}
PUBLISH_ACTION = "认定结果"

SOURCE_LABELS = {criteria.SOURCE_APPROVAL: "审批模型", criteria.SOURCE_MANUAL: "手工公式"}

_BOOTSTRAP_FLAG = "_reserve_domain_bootstrapped"
_publish_lock = threading.Lock()


class PublishConflict(RuntimeError):
    """已有发布正在进行：并发发布只允许一个结论落地。"""


class ReserveService:
    # ------------------------------------------------------------------ 初始化
    def bootstrap(self) -> None:
        """把占位样例替换为储量域数据，并完成历史迁移、历史结论回放、新口径重算。

        幂等：store 重建后才需要执行，已引导过直接跳过。
        """
        rows = store.rows(BLOCKS)
        if getattr(self, "_done", False) or any(b.get(_BOOTSTRAP_FLAG) for b in rows):
            return

        if rows and all(str(b.get("矿体名称", "")).startswith("储量估算样例") for b in rows):
            rows.clear()

        blocks = seed_data.raw_blocks()
        for block in blocks:
            draft_inputs = block.pop("_draft_inputs", None)
            rows.append(block)
            if draft_inputs is not None:
                self._write_draft(block, draft_inputs, str(block.get("估算时间") or ""))

        # 1) 历史块段缺面积快照的按估算时间迁移补齐（只动 V1，幂等）。
        migration = migrate_area_snapshots(rows)
        # 2) 历史项目按 V1 原口径回放结论并直接发布（冻结，不参与新口径重算）。
        replayed = self._replay_historical()
        # 3) 口径调整后已估算的 V2 数据按新口径重算，草稿结论同步刷新。
        recalc = self.recalculate(reason="口径调整启动重算", dry_run=False)

        rows.append({_BOOTSTRAP_FLAG: True, "id": 0, "块段编号": "", "矿体名称": "", "面积": 0,
                     "status": "", "pending": False, "abnormal": False})
        self._bootstrap_summary = {"migration": migration, "replayed": replayed, "recalc": recalc}
        self._done = True

    # ------------------------------------------------------------- 查询（台账）
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [b for b in store.rows(BLOCKS) if not b.get(_BOOTSTRAP_FLAG)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("块段编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._ledger_view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        block = self._find(entry_id)
        if block is None:
            return None
        view = self._ledger_view(block)
        view["估算详情"] = self.estimate_detail(entry_id)
        return view

    def list_estimates(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(ESTIMATES) if not row.get("_draft")]

    def list_drafts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(ESTIMATES) if row.get("_draft")]

    def list_map(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(MAPS) if not row.get("_draft")]

    def estimate_detail(self, entry_id: int) -> dict[str, Any]:
        """估算详情：两个来源的原结论、冲突清单、最终落地结论同屏可对。"""
        block = self._find(entry_id)
        if block is None:
            return {}
        records = [e for e in store.rows(ESTIMATES) if int(e.get("id", -1)) == entry_id]
        latest = records[-1] if records else None
        published = [e for e in records if not e.get("_draft")]
        drafts = [e for e in records if e.get("_draft")]
        return {
            "formula_version": block.get("formula_version", criteria.CURRENT_VERSION),
            "当前结论": dict(block) if block.get("结论编号") else None,
            "最近估算": dict(latest) if latest else None,
            "正式结论": [dict(e) for e in published],
            "草稿结论": [dict(e) for e in drafts],
        }

    def consistency(self) -> dict[str, Any]:
        return projections.consistency_report(
            [b for b in store.rows(BLOCKS) if not b.get(_BOOTSTRAP_FLAG)],
            store.rows(ESTIMATES),
            store.rows(MAPS),
        )

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str | None]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing, None

        try:
            area = criteria.to_number(values, "面积")
            grade = criteria.to_number(values, "品位") if str(values.get("品位") or "").strip() else 0.0
        except ValueError as exc:
            return None, [], str(exc)
        violations = criteria.check_save_limits(area, grade)
        if violations:
            return None, [], "；".join(violations)

        rows = store.rows(BLOCKS)
        real = [b for b in rows if not b.get(_BOOTSTRAP_FLAG)]
        entry: dict[str, Any] = {"id": max((int(b.get("id", 0)) for b in real), default=0) + 1}
        for field in REQUIRED_FIELDS:
            entry[field] = values.get(field)
        entry["面积"] = area
        for field in ("厚度", "品位", "矿石体重", "资源类别", "项目名称"):
            if str(values.get(field) or "").strip():
                try:
                    entry[field] = criteria.to_number(values, field) if field in ("厚度", "品位", "矿石体重") else values.get(field)
                except ValueError:
                    entry[field] = values.get(field)
        if "品位" not in entry:
            entry["品位"] = 0.0
        # 新登记一律现行口径；历史版本只由存量迁移保留，不再新增。
        entry["formula_version"] = criteria.CURRENT_VERSION
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, [], None

    # ------------------------------------------------------------- 完成估算
    def complete_estimate(
        self, entry_id: int, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        block = self._find(entry_id)
        if block is None:
            return None, f"矿体块段 {entry_id} 不存在或已归档"
        if block["status"] not in ("待估算", "已估算"):
            return None, f"块段状态为「{block['status']}」，不允许重新完成估算"

        inputs = self._collect_inputs(block, payload)
        try:
            result, source_results, conflicts = self._run_sources(block, inputs)
        except ValueError as exc:
            return None, str(exc)

        estimated_at = str(payload.get("估算时间") or _today())
        self._write_draft(block, inputs, estimated_at, source_results=source_results, conflicts=conflicts)
        block["status"] = "已估算"
        block["估算时间"] = estimated_at
        block["pending"] = True
        return self._ledger_view(block), self._estimate_message(result, conflicts, draft=True)

    # ------------------------------------------------------------------ 流转
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        block = self._find(entry_id)
        if block is None:
            return None, f"矿体块段 {entry_id} 不存在或已归档"
        if action == PUBLISH_ACTION:
            return self.publish(entry_id)
        if action not in ACTION_STATUS:
            return None, f"动作「{action}」不属于储量估算可执行范围"
        target = ACTION_STATUS[action]
        if action == "提交评审" and block["status"] not in ("已估算", "待评审"):
            return None, f"块段状态为「{block['status']}」，需先完成估算才能提交评审"
        block["status"] = target
        block["pending"] = target != STATUS_ORDER[-1]
        return self._ledger_view(block), f"矿体块段已{action}"

    # ------------------------------------------------------------------ 发布
    def publish(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """认定结果：把草稿结论发布到三条链路。

        全局发布锁 + 事务快照：拿不到锁说明有并发发布，直接拒绝；写入后做三链路
        一致性校验，任何异常/不一致都把草稿与三项投影整体回滚。
        """
        block = self._find(entry_id)
        if block is None:
            return None, f"矿体块段 {entry_id} 不存在或已归档"

        drafts = [e for e in store.rows(ESTIMATES)
                  if int(e.get("id", -1)) == entry_id and e.get("_draft")]
        if not drafts:
            return None, "该块段尚无估算草稿，请先完成估算再认定结果"

        if not _publish_lock.acquire(blocking=False):
            return None, "已有其他发布正在进行，本次认定未落地（并发发布只允许一个结论）"

        tables = {name: store.rows(name) for name in PUBLISH_TABLES}
        snapshot = projections.snapshot_tables(tables, PUBLISH_TABLES)
        try:
            conclusion = self._conclusion_from_draft(block, drafts[-1])
            conclusion_id = self._next_conclusion_id()
            conclusion["结论编号"] = conclusion_id
            estimated_at = str(drafts[-1].get("估算时间") or _today())
            conclusion["estimated_at"] = estimated_at

            # 1) 块段台账：落地结论字段写回台账行本身。
            self._apply_conclusion_to_ledger(block, conclusion, estimated_at)
            # 2) 估算清单：草稿转正，并追加一条带冲突明细的正式记录。
            self._promote_draft(entry_id, conclusion_id, conclusion)
            # 3) 储量图：同结论派生图面投影（先删该块段旧图元再写新的）。
            map_rows = tables[MAPS]
            map_rows[:] = [m for m in map_rows if int(m.get("id", -1)) != entry_id]
            map_rows.append(projections.map_projection(block, conclusion))

            report = projections.consistency_report(
                [b for b in tables[BLOCKS] if not b.get(_BOOTSTRAP_FLAG)],
                tables[ESTIMATES],
                tables[MAPS],
            )
            if not report["ok"]:
                bad = next((r for r in report["rows"] if not r["ok"]), None)
                raise RuntimeError(f"发布后三链路一致性校验失败：{bad['diffs'] if bad else ''}")

            block["status"] = "已认定"
            block["pending"] = False
            block["abnormal"] = False
        except Exception as exc:  # 回滚草稿与三项投影
            projections.restore_tables(tables, snapshot)
            return None, f"认定发布失败，已回滚草稿与三项投影：{exc}"
        finally:
            _publish_lock.release()

        return self._ledger_view(block), f"结论 {conclusion_id} 已发布，块段台账/估算清单/储量图三链路一致"

    # ------------------------------------------------------------- 重算（管理）
    def recalculate(self, *, reason: str = "口径调整重算", dry_run: bool = False) -> dict[str, Any]:
        """口径调整后已估算数据按新口径重算。

        * V1 历史块段：维持旧公式版本，跳过；
        * V2 草稿块段（已估算/待评审）：按保存的来源入参重跑 V2 引擎并刷新草稿投影；
        * V2 已认定块段：发布结论本就是 V2 引擎产物，重新发布一次以同步三投影；
          dry_run 下只报告不落库。
        """
        recalculated: list[dict[str, Any]] = []
        skipped_history: list[str] = []
        republished: list[str] = []

        for block in [b for b in store.rows(BLOCKS) if not b.get(_BOOTSTRAP_FLAG)]:
            if block.get("formula_version") == criteria.V1:
                skipped_history.append(str(block.get("块段编号")))
                continue
            drafts = [e for e in store.rows(ESTIMATES)
                      if int(e.get("id", -1)) == int(block["id"]) and e.get("_draft")]
            if drafts:
                latest = drafts[-1]
                inputs = latest["_inputs"]
                result, source_results, conflicts = self._run_sources(block, inputs)
                item = {"id": int(block["id"]), "块段编号": block["块段编号"],
                        "reason": reason, "conflicts": len(conflicts)}
                if not dry_run:
                    self._write_draft(
                        block, inputs, str(latest.get("估算时间") or _today()),
                        source_results=source_results, conflicts=conflicts,
                    )
                recalculated.append(item)
            elif block["status"] == "已认定":
                # 已认定的 V2 块段（含口径调整前已认定、尚无新结论投影的）：用台账
                # 入参按 V2 重新出结论并同步三投影（走发布事务）。
                if not dry_run:
                    inputs = {criteria.SOURCE_MANUAL: {
                        "area": float(block["面积"]), "thickness": float(block["厚度"]),
                        "grade": float(block["品位"]), "density": float(block["矿石体重"]),
                    }}
                    self._write_draft(block, inputs, str(block.get("估算时间") or _today()))
                    view, message = self.publish(int(block["id"]))
                    if view is None:
                        raise RuntimeError(message)
                    republished.append(str(block["块段编号"]))
                else:
                    republished.append(str(block["块段编号"]))

        return {
            "reason": reason,
            "dry_run": dry_run,
            "recalculated": recalculated,
            "republished": republished,
            "skipped_historical": skipped_history,
        }

    def migrate(self) -> dict[str, Any]:
        """显式触发历史块段面积快照迁移（幂等）。"""
        return migrate_area_snapshots([b for b in store.rows(BLOCKS) if not b.get(_BOOTSTRAP_FLAG)])

    # ================================================================ 内部方法
    def _find(self, entry_id: int) -> dict[str, Any] | None:
        for block in store.rows(BLOCKS):
            if not block.get(_BOOTSTRAP_FLAG) and int(block.get("id", -1)) == entry_id:
                return block
        return None

    def _collect_inputs(self, block: dict[str, Any], payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """收拢两个来源的入参：手工公式必填，审批模型可选（缺省时只有手工结论）。

        取值优先级（避免来源串味）：

        * ``manual_*`` / ``approval_*`` 前缀字段只属于对应来源；
        * 无前缀的一组数视为手工公式入参（现行口径下两者同表）；
        * 审批模型没显式给的字段才回退台账登记值。
        """
        def prefixed(prefix: str) -> dict[str, Any]:
            return {
                key: payload.get(f"{prefix}_{cn}")
                for key, cn in (("area", "面积"), ("thickness", "厚度"), ("grade", "品位"), ("density", "矿石体重"))
                if payload.get(f"{prefix}_{cn}") is not None and str(payload.get(f"{prefix}_{cn}")) != ""
            }

        manual = prefixed("manual")
        if not manual:
            manual = {
                key: payload.get(cn)
                for key, cn in (("area", "面积"), ("thickness", "厚度"), ("grade", "品位"), ("density", "矿石体重"))
                if payload.get(cn) is not None and str(payload.get(cn)) != ""
            }
        approval = prefixed("approval")
        if approval:
            # 审批模型只覆盖显式给出的字段（通常就是品位/厚度取值差异），其余沿用
            # 本次手工入参——不能回退台账旧值，否则两来源拿不到同一套底数无法对裁。
            approval = {**manual, **approval}
        else:
            approval = {
                "area": block.get("面积"),
                "thickness": payload.get("厚度", block.get("厚度")),
                "grade": payload.get("品位", block.get("品位")),
                "density": payload.get("矿石体重", block.get("矿石体重")),
            }
        return {criteria.SOURCE_MANUAL: manual, criteria.SOURCE_APPROVAL: approval}

    def _run_sources(
        self, block: dict[str, Any], inputs: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, str]]]:
        version = str(block.get("formula_version", criteria.CURRENT_VERSION))
        source_results: dict[str, dict[str, Any]] = {}
        for source in (criteria.SOURCE_MANUAL, criteria.SOURCE_APPROVAL):
            values = inputs.get(source) or {}
            # 注意品位允许为 0，不能用 ``or ""`` 把 0 当缺失。
            if any(values.get(f) is None or str(values.get(f)).strip() == ""
                   for f in criteria.REQUIRED_INPUTS):
                continue
            parsed = {f: criteria.to_number(values, f) for f in criteria.REQUIRED_INPUTS}
            # 现行硬上限对任何新估算生效；V1 回放走 evaluate 内部的版本上限。
            if version == criteria.CURRENT_VERSION:
                violations = criteria.check_save_limits(parsed["area"], parsed["grade"])
                if violations:
                    raise ValueError("；".join(violations))
            source_results[source] = criteria.evaluate(version, source, parsed)
        if not source_results:
            raise ValueError("缺少估算入参：面积、厚度、品位、矿石体重需至少提供一套完整数值")
        winner, conflicts = criteria.arbitrate(source_results)
        return winner, source_results, conflicts

    def _write_draft(
        self,
        block: dict[str, Any],
        inputs: dict[str, dict[str, Any]],
        estimated_at: str,
        *,
        source_results: dict[str, dict[str, Any]] | None = None,
        conflicts: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """完成估算：只写草稿（估算清单草稿行 + 储量图草稿图元），不动正式链路。"""
        if source_results is None:
            _, source_results, conflicts = self._run_sources(block, inputs)
        winner, conflicts = criteria.arbitrate(source_results)
        winner["estimated_at"] = estimated_at

        estimate_rows = store.rows(ESTIMATES)
        map_rows = store.rows(MAPS)
        # 清掉同块段旧草稿，保证一次估算只有一份待发布结论（种子草稿被本次估算替换）。
        estimate_rows[:] = [e for e in estimate_rows
                            if not (int(e.get("id", -1)) == int(block["id"]) and e.get("_draft"))]
        map_rows[:] = [m for m in map_rows
                       if not (int(m.get("id", -1)) == int(block["id"]) and m.get("_draft"))]

        for source, result in source_results.items():
            estimate_rows.append(
                self._estimate_row(block, result, estimated_at, draft=True, inputs=inputs)
            )
        winner_row = self._estimate_row(block, winner, estimated_at, draft=True, inputs=inputs,
                                        conflicts=conflicts)
        winner_row["_winning"] = True
        estimate_rows.append(winner_row)
        map_rows.append(projections.map_projection(block, winner) | {"_draft": True})
        return winner

    def _replay_historical(self) -> int:
        """历史项目按各自 V1 旧口径回放审批+手工结论并发布（冻结保留）。"""
        count = 0
        for block in [b for b in store.rows(BLOCKS) if b.get("formula_version") == criteria.V1]:
            common = {
                "area": float(block["面积"]),
                "thickness": float(block["厚度"]),
                "grade": float(block["品位"]),
                "density": float(block["矿石体重"]),
            }
            inputs = {criteria.SOURCE_APPROVAL: dict(common), criteria.SOURCE_MANUAL: dict(common)}
            # V1 两来源阈值/封边列序不同，回放时必然可能冲突，按审批优先级裁决。
            self._write_draft(block, inputs, str(block.get("估算时间") or "2026-08-01"))
            view, message = self.publish(int(block["id"]))
            if view is None:  # pragma: no cover - 种子历史数据必须能回放
                raise RuntimeError(f"历史块段 {block.get('块段编号')} 回放失败：{message}")
            count += 1
        return count

    def _conclusion_from_draft(self, block: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        _, source_results, _ = self._run_sources(block, draft["_inputs"])
        winner, conflicts = criteria.arbitrate(source_results)
        winner["_conflicts"] = conflicts
        return winner

    def _apply_conclusion_to_ledger(
        self, block: dict[str, Any], conclusion: dict[str, Any], estimated_at: str
    ) -> None:
        view = projections.ledger_projection(block, conclusion)
        for field in ("面积", "厚度", "品位", "矿石体重", "块段边界", "品位区间",
                      "封边规则", "矿石量", "金属量", "winning_source", "结论编号", "估算时间"):
            block[field] = view[field]
        if block.get("formula_version") == criteria.V1 and block.get("面积快照") is not None:
            # 历史块段的面积快照由迁移按估算时间补齐，回放/重算不得用当前面积覆盖它。
            pass
        else:
            block["面积快照"] = view["面积快照"]
        block["formula_version"] = view["formula_version"]

    def _promote_draft(self, entry_id: int, conclusion_id: str, conclusion: dict[str, Any]) -> None:
        estimate_rows = store.rows(ESTIMATES)
        kept = [e for e in estimate_rows if not (int(e.get("id", -1)) == entry_id and e.get("_draft"))]
        estimate_rows[:] = kept
        block = self._find(entry_id)
        assert block is not None
        estimate_rows.append(
            self._estimate_row(
                block, conclusion, str(conclusion.get("estimated_at") or _today()),
                draft=False, inputs={conclusion["winning_source"]: {
                    "area": conclusion["area"], "thickness": conclusion["thickness"],
                    "grade": conclusion["grade"], "density": conclusion["density"],
                }},
                conflicts=conclusion.get("_conflicts", []),
                conclusion_id=conclusion_id,
            )
        )

    def _estimate_row(
        self,
        block: dict[str, Any],
        result: dict[str, Any],
        estimated_at: str,
        *,
        draft: bool,
        inputs: dict[str, dict[str, Any]],
        conflicts: list[dict[str, str]] | None = None,
        conclusion_id: str = "",
    ) -> dict[str, Any]:
        source = str(result.get("winning_source") or result.get("source") or criteria.SOURCE_MANUAL)
        return {
            "id": int(block["id"]),
            "结论编号": conclusion_id,
            "块段编号": block["块段编号"],
            "矿体名称": block["矿体名称"],
            "项目名称": block.get("项目名称", ""),
            "formula_version": result["formula_version"],
            "来源": SOURCE_LABELS.get(source, source),
            "winning_source": source,
            "估算时间": estimated_at,
            "area": result["area"],
            "thickness": result["thickness"],
            "grade": result["grade"],
            "density": result["density"],
            # 中文键与块段台账保持同名，三链路按同一套字段比对。
            "面积": result["area"],
            "厚度": result["thickness"],
            "品位": result["grade"],
            "矿石体重": result["density"],
            "面积快照": block.get("面积快照", result["area"]),
            "块段边界": result["boundary_label"],
            "品位区间": result["grade_label"],
            "封边规则": result["seal_rule"],
            "矿石量": result["ore_tonnage"],
            "金属量": result["metal_tonnage"],
            "冲突明细": conflicts or [],
            "状态": "草稿" if draft else "已认定",
            "_draft": draft,
            "_inputs": deepcopy(inputs),
        }

    def _next_conclusion_id(self) -> str:
        existing = [e.get("结论编号", "") for e in store.rows(ESTIMATES) if not e.get("_draft")]
        seq = 0
        for cid in existing:
            try:
                seq = max(seq, int(str(cid).split("-")[-1]))
            except ValueError:
                continue
        return f"CONC-{seq + 1:04d}"

    def _ledger_view(self, block: dict[str, Any]) -> dict[str, Any]:
        """台账行视图。

        已发布块段读落地结论；未发布但已估算的块段读最近草稿结论，让台账列表与
        估算详情永远同源（旧实现台账只存登记值，列表与详情对不上）。草稿值带
        「草稿」后缀，正式发布后被同字段覆盖，不会出现两套数。
        """
        view = {k: v for k, v in block.items() if not k.startswith("_") and k not in ("面积沿革",)}
        view.setdefault("块段边界", "")
        view.setdefault("品位区间", "")
        view.setdefault("封边规则", "")
        view.setdefault("矿石量", "")
        view.setdefault("金属量", "")
        view.setdefault("winning_source", "")
        view.setdefault("结论编号", "")
        view.setdefault("面积快照", block.get("面积"))
        view.setdefault("formula_version", criteria.CURRENT_VERSION)

        if not block.get("结论编号") and block.get("status") in ("已估算", "待评审"):
            drafts = [e for e in store.rows(ESTIMATES)
                      if int(e.get("id", -1)) == int(block["id"]) and e.get("_draft")]
            if drafts:
                latest = drafts[-1]
                for field in ("块段边界", "品位区间", "封边规则", "矿石量", "金属量", "winning_source"):
                    view[field] = latest[field]
                view["结论状态"] = "草稿"
        return view

    def _estimate_message(
        self, result: dict[str, Any], conflicts: list[dict[str, str]], *, draft: bool
    ) -> str:
        stage = "草稿已保存" if draft else "结论已发布"
        if conflicts:
            summary = "、".join(sorted({c["field"] for c in conflicts}))
            winner = SOURCE_LABELS.get(str(result["winning_source"]), result["winning_source"])
            return f"估算完成（{stage}）：{summary}存在冲突，已按审批优先级采用「{winner}」结论"
        return f"估算完成（{stage}）：{result['boundary_label']}/{result['grade_label']}/{result['seal_rule']}"


def _today() -> str:
    return date.today().isoformat()


service = ReserveService()
