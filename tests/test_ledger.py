import tempfile, unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "ledger item", "description": "version ledger",
             "severity": "major", "quantity": 5, "threshold": 10},
            "creator", "inspector")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _read(self, request_no, baseline, measure=None, actor="recorder"):
        payload = {"kind": "seepage", "detail": "reading", "status": "open",
                   "request_no": request_no, "baseline_version": baseline}
        if measure is not None:
            payload["measure"] = measure
        return self.service.add_record(self.item["id"], payload, actor, "inspector")

    def test_idempotent_same_request_no_reuses_first_result(self):
        first = self._read("REQ-1", 1)
        second = self._read("REQ-1", 1)
        self.assertEqual(first["id"], second["id"])
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual(len(records), 1)

    def test_concurrent_submission_first_confirms_later_conflicts(self):
        first = self._read("REQ-A", 1, measure=6)
        self.assertEqual(first["ledger_status"], "confirmed")
        # 版本已推进到 2；第二个上报仍以 v1 为基准 → 冲突，不盖读数
        second = self._read("REQ-B", 1, measure=9)
        self.assertEqual(second["ledger_status"], "conflict")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["current_version"], 2)
        # 量值只被先到读数更新
        self.assertEqual(item["quantity"], 6)
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual(
            [r["ledger_status"] for r in records], ["confirmed", "conflict"])

    def test_threshold_change_invalidates_draft_issued_keeps_basis(self):
        draft = self.service.create_signoff(self.item["id"], "boss", "emergency_manager")
        self.assertEqual(draft["status"], "draft")
        item = self.service.get_item(self.item["id"], "viewer")
        updated = self.service.change_threshold(
            self.item["id"], {"threshold": 20, "expected_version": item["version"]},
            "eng", "dam_engineer")
        self.assertEqual(updated["current_version"], item["version"] + 1)
        signoffs = self.service.list_signoffs(self.item["id"], "viewer")
        self.assertEqual(signoffs[0]["status"], "invalid")
        self.assertEqual(signoffs[0]["invalidation_source_type"], "threshold")
        self.assertIsNotNone(signoffs[0]["invalidation_detail"])

        # 已签发的签发保留当时依据，新阈值不再影响
        draft2 = self.service.create_signoff(self.item["id"], "boss", "emergency_manager")
        issued = self.service.issue_signoff(
            self.item["id"], draft2["id"], "boss", "emergency_manager")
        self.assertEqual(issued["status"], "issued")
        self.service.change_threshold(
            self.item["id"], {"threshold": 30, "expected_version": updated["version"]},
            "eng", "dam_engineer")
        after = self.service.list_signoffs(self.item["id"], "viewer")
        issued_row = next(s for s in after if s["id"] == issued["id"])
        self.assertEqual(issued_row["status"], "issued")
        self.assertEqual(issued_row["basis_threshold"], 20)

    def test_new_reading_invalidates_unfinished_signoff(self):
        self.service.create_signoff(self.item["id"], "boss", "emergency_manager")
        self._read("REQ-NEW", 1, measure=12)
        signoffs = self.service.list_signoffs(self.item["id"], "viewer")
        self.assertEqual(signoffs[0]["status"], "invalid")
        self.assertEqual(signoffs[0]["invalidation_source_type"], "reading")

    def test_legacy_record_without_request_no_or_baseline_is_pending_supplement(self):
        legacy = self.service.add_record(
            self.item["id"], {"kind": "位移", "detail": "old", "status": "open"},
            "recorder", "inspector")
        self.assertEqual(legacy["ledger_status"], "pending_supplement")
        # 补核：以当前版本为基准 → 确认
        item = self.service.get_item(self.item["id"], "viewer")
        done = self.service.supplement_record(
            self.item["id"], legacy["id"],
            {"request_no": "REQ-OLD", "baseline_version": item["version"]},
            "eng", "dam_engineer")
        self.assertEqual(done["ledger_status"], "confirmed")

    def test_supplement_with_stale_baseline_becomes_conflict(self):
        legacy = self.service.add_record(
            self.item["id"], {"kind": "位移", "detail": "old", "status": "open"},
            "recorder", "inspector")
        # 先有一条读数把版本推到 2
        self._read("REQ-X", 1)
        done = self.service.supplement_record(
            self.item["id"], legacy["id"],
            {"request_no": "REQ-OLD", "baseline_version": 1},
            "eng", "dam_engineer")
        self.assertEqual(done["ledger_status"], "conflict")

    def test_rbac(self):
        with self.assertRaises(PermissionDenied):
            self.service.add_record(
                self.item["id"], {"kind": "x", "detail": "y", "request_no": "R",
                                  "baseline_version": 1},
                "viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_signoff(self.item["id"], "inspector", "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.issue_signoff(self.item["id"], 1, "eng", "dam_engineer")
        with self.assertRaises(PermissionDenied):
            self.service.review_signoff(self.item["id"], 1, {}, "boss", "emergency_manager")
        with self.assertRaises(PermissionDenied):
            self.service.change_threshold(
                self.item["id"], {"threshold": 20, "expected_version": 1},
                "viewer", "viewer")

    def test_full_flow_report_review_issue_then_invalidate(self):
        reading = self._read("REQ-FULL", 1, measure=11)
        self.assertEqual(reading["ledger_status"], "confirmed")
        draft = self.service.create_signoff(self.item["id"], "boss", "emergency_manager")
        self.service.review_signoff(
            self.item["id"], draft["id"], {"note": "同意"}, "eng", "dam_engineer")
        issued = self.service.issue_signoff(
            self.item["id"], draft["id"], "boss", "emergency_manager")
        self.assertEqual(issued["status"], "issued")
        # 新读数让未完成签发失效；已签发保留
        draft2 = self.service.create_signoff(self.item["id"], "boss", "emergency_manager")
        self._read("REQ-FULL-2", 2, measure=15)
        rows = self.service.list_signoffs(self.item["id"], "viewer")
        self.assertEqual(next(s for s in rows if s["id"] == draft2["id"])["status"], "invalid")
        self.assertEqual(next(s for s in rows if s["id"] == issued["id"])["status"], "issued")

    def test_conflict_error_on_stale_threshold_version(self):
        with self.assertRaises(ConflictError):
            self.service.change_threshold(
                self.item["id"], {"threshold": 20, "expected_version": 99},
                "eng", "dam_engineer")


if __name__ == "__main__":
    unittest.main()
