import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "渗流异常", "description": "背水坡出现散浸",
            "severity": "major", "quantity": 5, "threshold": 10,
            "external_ref": "LED-1",
        }, "张巡", "inspector")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def report(self, request_id, base, reading, actor="张巡", role="inspector", **extra):
        payload = {"kind": "seepage", "detail": f"读数{reading}",
                   "request_id": request_id, "base_version": base,
                   "reading": reading}
        payload.update(extra)
        return self.service.add_record(self.item["id"], payload, actor, role)

    def test_idempotent_same_request_keeps_first_result(self):
        first = self.report("REQ-1", 1, 7.0)
        self.assertEqual(first["resolution"], "confirmed")
        self.assertEqual(first["replayed"], False)
        # 同号重复提交，即便读数不同也沿用第一次结果
        replay = self.report("REQ-1", 1, 99.0, actor="李巡")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["resolution"], "confirmed")
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["reading"], 7.0)  # 重放必须返回第一次读数
        current = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["quantity"], 7.0)

    def test_concurrent_later_submit_becomes_conflict_not_overwrite(self):
        # 两人同时基于同一版本上报：先到成立
        first = self.report("REQ-A", 1, 8.0)
        self.assertEqual(first["resolution"], "confirmed")
        # 后到者基准版本停留在1，已过期
        second = self.report("REQ-B", 1, 12.0, actor="李巡")
        self.assertEqual(second["resolution"], "conflict")
        current = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(current["quantity"], 8.0)  # 冲突读数不能盖掉已确认读数
        self.assertEqual(current["version"], 2)
        ledger = self.service.ledger(self.item["id"], "viewer")
        self.assertEqual(ledger["current_version"], 2)
        self.assertEqual(len(ledger["conflicts"]), 1)
        self.assertEqual(ledger["conflicts"][0]["reading"], 12.0)

    def test_stale_conflict_can_resubmit_on_fresh_base(self):
        self.report("REQ-A", 1, 8.0)
        stale = self.report("REQ-B", 1, 12.0)
        self.assertEqual(stale["resolution"], "conflict")
        # 冲突者刷新到版本2后重新上报（新请求号）
        retry = self.report("REQ-B2", 2, 12.0)
        self.assertEqual(retry["resolution"], "confirmed")
        current = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(current["quantity"], 12.0)
        self.assertEqual(current["version"], 3)

    def test_new_reading_invalidates_open_issuance_and_recomputes(self):
        self.report("REQ-1", 1, 5.2)
        issuances = self.repo.list_issuances(self.item["id"])
        self.assertEqual(len(issuances), 1)
        first_issuance = issuances[-1]
        self.assertEqual(first_issuance["state"], "pending")
        self.assertEqual(first_issuance["priority"], 9)
        # 新读数触发：未完成签发失效并重算（优先级9->10，期限缩短）
        self.report("REQ-2", 2, 20.0)
        issuances = self.repo.list_issuances(self.item["id"])
        self.assertEqual(issuances[0]["state"], "invalidated")
        self.assertEqual(issuances[0]["invalidated_by"], "new_reading:REQ-2")
        new_one = issuances[-1]
        self.assertEqual(new_one["state"], "pending")
        self.assertGreater(new_one["priority"], first_issuance["priority"])
        ledger = self.service.ledger(self.item["id"], "viewer")
        self.assertEqual(ledger["invalidated_issuances"][0]["invalidated_by"],
                         "new_reading:REQ-2")

    def test_issued_issuance_keeps_its_basis(self):
        self.report("REQ-1", 1, 7.0)
        issuance_id = self.repo.list_issuances(self.item["id"])[0]["id"]
        self.service.review_issuance(
            self.item["id"], issuance_id, {"base_version": 2},
            "王工", "dam_engineer")
        issued = self.service.issue_issuance(
            self.item["id"], issuance_id,
            {"base_version": 2, "conclusion": "启动应急响应，限4小时到位"},
            "赵总", "emergency_manager")
        self.assertEqual(issued["state"], "issued")
        snapshot_priority = issued["priority"]
        # 后续读数暴涨：已签发结论保留当时依据，只是生成新的待签发
        self.report("REQ-2", 2, 50.0)
        kept = next(i for i in self.repo.list_issuances(self.item["id"])
                    if i["id"] == issuance_id)
        self.assertEqual(kept["state"], "issued")
        self.assertEqual(kept["priority"], snapshot_priority)
        self.assertEqual(kept["conclusion"], "启动应急响应，限4小时到位")
        newest = self.repo.list_issuances(self.item["id"])[-1]
        self.assertEqual(newest["state"], "pending")

    def test_threshold_change_invalidates_and_recomputes(self):
        self.report("REQ-1", 1, 5.2)
        before = self.repo.list_issuances(self.item["id"])[-1]
        self.assertEqual(before["priority"], 9)
        updated = self.service.change_threshold(
            self.item["id"], {"base_version": 2, "threshold": 3},
            "王工", "dam_engineer")
        self.assertEqual(updated["version"], 3)
        after = self.repo.list_issuances(self.item["id"])[-1]
        self.assertEqual(after["state"], "pending")
        self.assertGreater(after["priority"], before["priority"])
        invalidated = self.repo.list_issuances(self.item["id"])[0]
        self.assertEqual(invalidated["state"], "invalidated")
        self.assertIn("threshold_change", invalidated["invalidated_by"])
        # 阈值变化也要基准版本
        with self.assertRaises(ConflictError):
            self.service.change_threshold(
                self.item["id"], {"base_version": 1, "threshold": 2},
                "王工", "dam_engineer")

    def test_review_requires_engineer_and_fresh_base(self):
        self.report("REQ-1", 1, 7.0)
        issuance_id = self.repo.list_issuances(self.item["id"])[0]["id"]
        # 巡检员不能复核
        with self.assertRaises(PermissionDenied):
            self.service.review_issuance(
                self.item["id"], issuance_id, {"base_version": 2},
                "张巡", "inspector")
        # 依据版本过期不能复核
        self.report("REQ-2", 2, 8.0)
        with self.assertRaises(ConflictError):
            self.service.review_issuance(
                self.item["id"], issuance_id, {"base_version": 2},
                "王工", "dam_engineer")

    def test_only_manager_can_issue_after_review(self):
        self.report("REQ-1", 1, 7.0)
        issuance_id = self.repo.list_issuances(self.item["id"])[0]["id"]
        # 未复核不能签发
        with self.assertRaises(ConflictError):
            self.service.issue_issuance(
                self.item["id"], issuance_id,
                {"base_version": 2, "conclusion": "x"}, "赵总",
                "emergency_manager")
        self.service.review_issuance(
            self.item["id"], issuance_id, {"base_version": 2},
            "王工", "dam_engineer")
        # 坝工程师不能签发
        with self.assertRaises(PermissionDenied):
            self.service.issue_issuance(
                self.item["id"], issuance_id,
                {"base_version": 2, "conclusion": "x"}, "王工",
                "dam_engineer")
        issued = self.service.issue_issuance(
            self.item["id"], issuance_id,
            {"base_version": 2, "conclusion": "按预案处置"},
            "赵总", "emergency_manager")
        self.assertEqual(issued["issued_by"], "赵总")

    def test_legacy_record_escalates_then_verified(self):
        # 缺请求号与基准版本 → 待补核，巡检员可登记
        result = self.service.add_record(
            self.item["id"],
            {"kind": "seepage", "detail": "老记录本手抄读数", "status": "open"},
            "张巡", "inspector")
        self.assertEqual(result["resolution"], "pending_verification")
        record_id = result["record"]["id"]
        ledger = self.service.ledger(self.item["id"], "viewer")
        self.assertEqual(len(ledger["pending_verifications"]), 1)
        # 巡检员不能补核
        with self.assertRaises(PermissionDenied):
            self.service.verify_record(
                self.item["id"], record_id,
                {"base_version": 1, "reading": 9.0}, "张巡", "inspector")
        verified = self.service.verify_record(
            self.item["id"], record_id,
            {"base_version": 1, "reading": 9.0}, "王工", "dam_engineer")
        self.assertEqual(verified["resolution"], "confirmed")
        # 补核同样带动签发重算
        states = [i["state"] for i in self.repo.list_issuances(self.item["id"])]
        self.assertIn("pending", states)
        ledger = self.service.ledger(self.item["id"], "viewer")
        self.assertEqual(ledger["pending_verifications"], [])

    def test_viewer_cannot_report(self):
        with self.assertRaises(PermissionDenied):
            self.service.add_record(
                self.item["id"],
                {"kind": "seepage", "detail": "x", "request_id": "R",
                 "base_version": 1, "reading": 1},
                "旁观者", "viewer")

    def test_audit_chain_intact(self):
        self.report("REQ-1", 1, 7.0)
        self.report("REQ-1", 1, 99.0)
        self.report("REQ-2", 2, 8.0)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
