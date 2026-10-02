from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, ISSUE_ROLES,
                    RECORD_ROLES, REPORT_ROLES, REVIEW_ROLES,
                    THRESHOLD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    @staticmethod
    def _require_version(payload: Dict[str, Any], field: str) -> int:
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{field}必须是正整数")
        return value

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
            "threshold": threshold, "version": item["version"],
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        """巡检上报：带请求号+基准版本走版本台账；缺任一字段升级为待补核。"""
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        request_id = payload.get("request_id")
        base_version = payload.get("base_version")

        if request_id is None and base_version is None:
            # 旧记录：巡检员/坝工程师均可登记，进入待补核
            ensure_role(role, RECORD_ROLES)
            record = self.repository.add_legacy_record(
                item_id, kind, detail, status, external_ref, actor)
            return {"resolution": "pending_verification", "record": record,
                    "message": "缺少请求号或基准版本，已升级为待补核，需坝工程师补核"}

        ensure_role(role, REPORT_ROLES)
        request_id = require_text(request_id, "request_id", 100)
        base_version = self._require_version(payload, "base_version")
        reading = None
        if payload.get("reading") is not None:
            reading = require_number(payload.get("reading"), "reading")
        # 先确认缺陷存在且角色可查看
        self.repository.get_item(item_id)
        result = self.repository.submit_report(
            item_id, request_id, base_version, kind, detail, reading,
            payload, external_ref, actor)
        return result

    def verify_record(self, item_id: int, record_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        """坝工程师对旧记录补核：读数与基准版本确认后成立。"""
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        base_version = self._require_version(payload, "base_version")
        reading = None
        if payload.get("reading") is not None:
            reading = require_number(payload.get("reading"), "reading")
        record = self.repository.verify_record(
            item_id, record_id, base_version, reading, actor)
        return record

    def change_threshold(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, THRESHOLD_ROLES)
        actor = require_text(actor, "actor", 100)
        base_version = self._require_version(payload, "base_version")
        new_threshold = require_number(payload.get("threshold"), "threshold", 0.000001)
        item = self.repository.change_threshold(
            item_id, new_threshold, base_version, actor)
        return self.enrich(item)

    def review_issuance(self, item_id: int, issuance_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        base_version = self._require_version(payload, "base_version")
        return self.repository.review_issuance(
            item_id, issuance_id, base_version, actor)

    def issue_issuance(self, item_id: int, issuance_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ISSUE_ROLES)
        actor = require_text(actor, "actor", 100)
        base_version = self._require_version(payload, "base_version")
        conclusion = require_text(payload.get("conclusion"), "conclusion")
        return self.repository.issue_issuance(
            item_id, issuance_id, base_version, conclusion, actor)

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

    def ledger(self, item_id: int, role: str) -> Dict[str, Any]:
        """版本台账：当前版本、失效来源、冲突内容与签发依据一屏展示。"""
        self._view(role)
        item = self.enrich(self.repository.get_item(item_id))
        records = self.repository.list_records(item_id)
        submissions = self.repository.list_submissions(item_id)
        issuances = self.repository.list_issuances(item_id)
        conflicts = [s for s in submissions if s["resolution"] == "conflict"]
        pending_verifications = [
            r for r in records if r.get("resolution") == "pending_verification"]
        invalidated = [{
            "issuance_id": i["id"], "state": i["state"],
            "invalidated_by": i["invalidated_by"],
            "invalidated_at": i["invalidated_at"],
            "basis": i["basis"], "base_version": i["base_version"],
        } for i in issuances if i["state"] == "invalidated"]
        current_issuance = next(
            (i for i in issuances if i["state"] in ("pending", "reviewed", "issued")), None)
        return {
            "item": item,
            "current_version": item["version"],
            "records": records,
            "submissions": submissions,
            "conflicts": conflicts,
            "pending_verifications": pending_verifications,
            "issuances": issuances,
            "current_issuance": current_issuance,
            "invalidated_issuances": invalidated,
        }

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
