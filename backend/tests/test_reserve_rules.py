"""储量估算统一口径的关键规则测试。"""
from __future__ import annotations

import copy
import unittest

from app.services import reserve as reserve_service
from app.services.reserve import CURRENT_FORMULA_VERSION, LEGACY_FORMULA_VERSION, ReserveService


class ReserveRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        ReserveService.reset_store()
        self.service = ReserveService()

    def test_historical_migration_fills_snapshot_and_keeps_v1(self) -> None:
        entry = self.service.get_entry(2)
        estimate = self.service.get_estimate(2)
        map_projection = self.service.get_map_projection(2)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["formulaVersion"], LEGACY_FORMULA_VERSION)
        self.assertEqual(entry["面积快照"], 18600.0)
        self.assertTrue(entry["snapshotMigratedAt"].startswith("2026-08-15"))
        self.assertEqual(entry["封边规则"], "禁止封边")
        self.assertEqual(
            entry["conclusionChecksum"],
            estimate["conclusionChecksum"],
        )
        self.assertEqual(
            estimate["conclusionChecksum"],
            map_projection["conclusionChecksum"],
        )

    def test_new_estimate_recalculates_all_three_projections(self) -> None:
        entry, message = self.service.run_action(
            1,
            "完成估算",
            {"厚度": 4.2, "品位": 1.2, "矿石体重": 2.7, "块段边界": "工程边界"},
        )

        self.assertIsNotNone(entry)
        self.assertIn("三项投影", message)
        self.assertEqual(entry["status"], "已估算")
        self.assertEqual(entry["formulaVersion"], CURRENT_FORMULA_VERSION)
        self.assertEqual(entry["封边规则"], "工程封边")
        self.assertEqual(entry["conclusionChecksum"], self.service.get_estimate(1)["conclusionChecksum"])
        self.assertEqual(entry["conclusionChecksum"], self.service.get_map_projection(1)["conclusionChecksum"])

    def test_approval_model_overrides_manual_formula_and_edge_is_derived(self) -> None:
        payload = {
            "厚度": 4.2,
            "品位": 1.2,
            "矿石体重": 2.7,
            "manualFormula": {"块段边界": "外推边界", "封边规则": "无限外推封边"},
            "approvalModel": {"块段边界": "自然边界", "封边规则": "串位封边"},
        }
        entry, _ = self.service.run_action(1, "完成估算", payload)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["块段边界"], "自然边界")
        self.assertEqual(entry["封边规则"], "自然封边")
        self.assertEqual(entry["ruleSources"]["boundary"], "审批模型")
        self.assertTrue(
            any(item["field"] == "封边规则" and item["requested"] == "串位封边" for item in entry["ruleConflicts"])
        )

    def test_area_and_grade_upper_limits_block_save(self) -> None:
        entry, errors = self.service.create_entry({
            "块段编号": "RESE-NEW",
            "矿体名称": "超限面积",
            "面积": 1_000_001,
        })
        self.assertIsNone(entry)
        self.assertTrue(any("面积" in error for error in errors))

        entry, errors = self.service.create_entry({
            "块段编号": "RESE-NEW",
            "矿体名称": "超限品位",
            "面积": 12_000,
            "品位": 5.1,
        })
        self.assertIsNone(entry)
        self.assertTrue(any("品位" in error for error in errors))

        entry, message = self.service.run_action(
            1,
            "完成估算",
            {"厚度": 4.2, "品位": 1.2, "矿石体重": 2.7, "面积": 1_000_001},
        )
        self.assertIsNone(entry)
        self.assertIn("面积", message)

        entry, message = self.service.run_action(
            1,
            "完成估算",
            {"厚度": 4.2, "品位": 5.1, "矿石体重": 2.7, "面积": 12_000},
        )
        self.assertIsNone(entry)
        self.assertIn("品位", message)
        self.assertIsNone(self.service.get_estimate(1))

    def test_historical_project_cannot_switch_to_current_formula(self) -> None:
        entry, message = self.service.run_action(2, "完成估算", {"formulaVersion": CURRENT_FORMULA_VERSION})
        self.assertIsNone(entry)
        self.assertIn("历史项目", message)
        self.assertEqual(self.service.get_entry(2)["formulaVersion"], LEGACY_FORMULA_VERSION)

    def test_historical_recalculation_keeps_existing_area_snapshot(self) -> None:
        entry, _ = self.service.run_action(2, "完成估算", {"品位": 0.46})
        self.assertIsNotNone(entry)
        self.assertEqual(entry["面积"], 18600.0)
        self.assertEqual(entry["面积快照"], 18600.0)
        self.assertEqual(entry["品位区间"], "低品位")
        self.assertEqual(self.service.get_estimate(2)["面积快照"], 18600.0)
        self.assertEqual(self.service.get_map_projection(2)["面积快照"], 18600.0)

    def test_failed_publish_rolls_back_all_three_projections(self) -> None:
        self.service.run_action(
            1,
            "完成估算",
            {"厚度": 4.2, "品位": 1.2, "矿石体重": 2.7},
        )
        before = copy.deepcopy(self.service.get_entry(1))
        original_upsert = self.service._upsert_projection

        def failing_upsert(module: str, block_id: int, row: dict) -> None:
            if module == reserve_service.MAP_MODULE:
                raise RuntimeError("map unavailable")
            original_upsert(module, block_id, row)

        self.service._upsert_projection = failing_upsert
        with self.assertRaises(RuntimeError):
            self.service.save_draft(1, {"publish": True, "品位": 1.3})
        self.service._upsert_projection = original_upsert

        self.assertEqual(self.service.get_entry(1)["conclusionChecksum"], before["conclusionChecksum"])
        self.assertEqual(self.service.get_estimate(1)["conclusionChecksum"], before["conclusionChecksum"])
        self.assertEqual(self.service.get_map_projection(1)["conclusionChecksum"], before["conclusionChecksum"])
        self.assertEqual(self.service.get_entry(1)["品位"], before["品位"])

    def test_concurrent_publish_only_allows_one_writer_for_block(self) -> None:
        self.service.save_draft(
            1,
            {"厚度": 4.2, "品位": 1.2, "矿石体重": 2.7},
        )
        lock = self.service._block_lock(1)
        lock.acquire(blocking=False)
        try:
            entry, message = self.service.publish_conclusion(1)
            self.assertIsNone(entry)
            self.assertIn("正在发布", message)
        finally:
            lock.release()

        entry, message = self.service.publish_conclusion(1)
        self.assertIsNotNone(entry)
        self.assertIn("已发布", message)


if __name__ == "__main__":
    unittest.main()
