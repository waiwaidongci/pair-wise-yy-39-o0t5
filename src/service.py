from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, normalize_severity, require_number,
                      require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, REPORT_ROLES, REVIEW_ROLES,
                    SIGN_ROLES, THRESHOLD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        """巡检员上报读数。

        上报带请求号与基准版本：同号重复提交沿用第一次结果；基准版本与当前
        版本一致才确认读数并推进版本、使未完成签发失效；否则内容留作冲突，
        不盖掉已确认读数。缺请求号或基准版本的记录升级为待补核。
        """
        ensure_role(role, REPORT_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        request_no = payload.get("request_no")
        if request_no is not None:
            request_no = require_text(request_no, "request_no", 100)
        baseline_version = payload.get("baseline_version")
        if baseline_version is not None:
            if isinstance(baseline_version, bool) or not isinstance(baseline_version, int) \
                    or baseline_version < 1:
                raise ValueError("baseline_version必须是正整数")
        measure = payload.get("measure")
        if measure is not None:
            measure = require_number(measure, "measure")

        # 同号重复提交：沿用第一次结果
        if request_no is not None:
            existing = self.repository.find_record_by_request_no(request_no)
            if existing is not None:
                return existing

        item = self.repository.get_item(item_id)
        if request_no is None or baseline_version is None:
            ledger_status = "pending_supplement"
        elif baseline_version != item["version"]:
            ledger_status = "conflict"
        else:
            ledger_status = "confirmed"

        record = self.repository.add_record(
            item_id, kind, detail, status, external_ref, actor,
            request_no, baseline_version, ledger_status, measure)

        if ledger_status == "confirmed":
            self.repository.confirm_reading(item_id, baseline_version, measure)
            self.repository.invalidate_signoffs(item_id, "reading", record["id"], {
                "record_id": record["id"], "kind": kind, "measure": measure,
                "request_no": request_no,
            })
        self.repository.append_audit("reading", "record", record["id"], actor, {
            "kind": kind, "status": status, "request_no": request_no,
            "baseline_version": baseline_version, "ledger_status": ledger_status,
            "measure": measure,
        })
        return record

    def change_threshold(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        """控制阈值变更：乐观锁校验后推进版本，未完成签发立即失效。"""
        ensure_role(role, THRESHOLD_ROLES)
        actor = require_text(actor, "actor", 100)
        new_threshold = require_number(payload.get("threshold"), "threshold", 0.000001)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        updated = self.repository.change_threshold(item_id, new_threshold, expected_version)
        self.repository.invalidate_signoffs(item_id, "threshold", None, {
            "from": item["threshold"], "to": new_threshold,
        })
        self.repository.append_audit("threshold", ENTITY, item_id, actor, {
            "from": item["threshold"], "to": new_threshold, "version": updated["version"],
        })
        return self.enrich(updated)

    def supplement_record(self, item_id: int, record_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        """坝工程师对待补核记录补请求号与基准版本。"""
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        request_no = require_text(payload.get("request_no"), "request_no", 100)
        baseline_version = payload.get("baseline_version")
        if isinstance(baseline_version, bool) or not isinstance(baseline_version, int) \
                or baseline_version < 1:
            raise ValueError("baseline_version必须是正整数")
        record = self.repository.get_record(record_id)
        if record["item_id"] != item_id:
            raise NotFoundError("记录不存在")
        item = self.repository.get_item(item_id)
        if baseline_version != item["version"]:
            ledger_status = "conflict"
        else:
            ledger_status = "confirmed"
        updated = self.repository.update_record_ledger(
            record_id, request_no, baseline_version, ledger_status)
        if ledger_status == "confirmed":
            self.repository.confirm_reading(item_id, baseline_version, record["measure"])
            self.repository.invalidate_signoffs(item_id, "reading", record_id, {
                "record_id": record_id, "kind": record["kind"],
                "measure": record["measure"], "request_no": request_no,
                "supplemented": True,
            })
        self.repository.append_audit("supplement", "record", record_id, actor, {
            "request_no": request_no, "baseline_version": baseline_version,
            "ledger_status": ledger_status,
        })
        return updated

    def create_signoff(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        """应急负责人起草应急签发，冻结当时依据。"""
        ensure_role(role, SIGN_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        basis = {
            "severity": item["severity"], "quantity": item["quantity"],
            "threshold": item["threshold"],
            "priority": priority_score(item["severity"], item["quantity"], item["threshold"]),
            "deadline_hours": response_deadline_hours(
                item["severity"], item["quantity"], item["threshold"]),
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        signoff = self.repository.create_signoff(item_id, basis, actor)
        self.repository.append_audit("signoff_create", "signoff", signoff["id"], actor, {
            "item_id": item_id, "basis": basis,
        })
        return signoff

    def review_signoff(self, item_id: int, signoff_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """坝工程师复核签发草稿。"""
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        signoff = self.repository.get_signoff(signoff_id)
        if signoff["item_id"] != item_id:
            raise NotFoundError("签发不存在")
        if signoff["status"] != "draft":
            raise ConflictError(f"当前状态{signoff['status']}不可复核")
        updated = self.repository.update_signoff(
            signoff_id, status="reviewed", review_note=note,
            reviewed_by=actor, reviewed_at=utc_now())
        self.repository.append_audit("signoff_review", "signoff", signoff_id, actor, {
            "note": note,
        })
        return updated

    def issue_signoff(self, item_id: int, signoff_id: int, actor: str,
                      role: str) -> Dict[str, Any]:
        """应急负责人签发：已签发的保留当时依据，不再受新读数影响。"""
        ensure_role(role, SIGN_ROLES)
        actor = require_text(actor, "actor", 100)
        signoff = self.repository.get_signoff(signoff_id)
        if signoff["item_id"] != item_id:
            raise NotFoundError("签发不存在")
        if signoff["status"] not in ("draft", "reviewed"):
            raise ConflictError(f"当前状态{signoff['status']}不可签发")
        updated = self.repository.update_signoff(
            signoff_id, status="issued", issued_by=actor, issued_at=utc_now())
        self.repository.append_audit("signoff_issue", "signoff", signoff_id, actor, {
            "basis": {
                "severity": signoff["basis_severity"],
                "quantity": signoff["basis_quantity"],
                "threshold": signoff["basis_threshold"],
                "priority": signoff["basis_priority"],
                "deadline_hours": signoff["basis_deadline_hours"],
            },
        })
        return updated

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def list_signoffs(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_signoffs(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["current_version"] = item["version"]
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        records = self.repository.list_records(item["id"])
        signoffs = self.repository.list_signoffs(item["id"])
        result["ledger"] = {
            "confirmed": sum(1 for r in records if r["ledger_status"] == "confirmed"),
            "conflict": sum(1 for r in records if r["ledger_status"] == "conflict"),
            "pending_supplement": sum(
                1 for r in records if r["ledger_status"] == "pending_supplement"),
            "signoffs": {
                "draft": sum(1 for s in signoffs if s["status"] == "draft"),
                "reviewed": sum(1 for s in signoffs if s["status"] == "reviewed"),
                "issued": sum(1 for s in signoffs if s["status"] == "issued"),
                "invalid": sum(1 for s in signoffs if s["status"] == "invalid"),
            },
            "latest_invalidation": next(
                ({
                    "source_type": s["invalidation_source_type"],
                    "source_id": s["invalidation_source_id"],
                    "detail": s["invalidation_detail"],
                } for s in sorted(signoffs, key=lambda x: x["id"], reverse=True)
                  if s["status"] == "invalid" and s["invalidation_source_type"]),
                None),
        }
        return result
