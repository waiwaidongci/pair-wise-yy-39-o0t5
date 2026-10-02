from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (ENTITY, ID_PREFIX, ISSUANCE_STATES, OPEN_ISSUANCE_STATES,
                    RESOLUTIONS, STATES, escalation_required, priority_score,
                    response_deadline_hours)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._migrate_legacy_records()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        resolutions = ",".join("'" + s + "'" for s in RESOLUTIONS)
        issuance_states = ",".join("'" + s + "'" for s in ISSUANCE_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    request_id TEXT,
                    base_version INTEGER,
                    resolution TEXT DEFAULT 'legacy'
                        CHECK(resolution IN ('legacy',{resolutions})),
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    reading REAL,
                    resolution TEXT NOT NULL CHECK(resolution IN ({resolutions})),
                    payload_json TEXT NOT NULL,
                    duplicate_of INTEGER REFERENCES submissions(id),
                    confirmed_record_id INTEGER REFERENCES records(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS issuances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    state TEXT NOT NULL CHECK(state IN ({issuance_states})),
                    priority INTEGER NOT NULL,
                    deadline_hours INTEGER NOT NULL,
                    base_version INTEGER NOT NULL,
                    escalation_required INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    threshold REAL NOT NULL,
                    basis TEXT NOT NULL DEFAULT 'created',
                    invalidated_by TEXT,
                    invalidated_at TEXT,
                    conclusion TEXT,
                    issued_by TEXT,
                    issued_at TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    def _migrate_legacy_records(self) -> None:
        """旧记录缺请求号或基准版本：统一升级为待补核。"""
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE records SET resolution='pending_verification'
                   WHERE request_id IS NULL OR base_version IS NULL"""
            )

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _audit_in_tx(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> int:
        """在已持锁的事务内追加审计事件，避免嵌套事务破坏原子性。"""
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur = self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )
        return int(cur.lastrowid)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺陷不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("缺陷不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    # ---- 版本台账：上报（幂等 + 先到成立 + 后到冲突留痕） ----

    def submit_report(self, item_id: int, request_id: str, base_version: int,
                      kind: str, detail: str, reading: Optional[float],
                      payload: dict, external_ref: Optional[str],
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            item_row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("缺陷不存在")
            item = self._item(item_row)

            # 同号重复提交：沿用第一次结果，绝不二次生效
            dup = self.conn.execute(
                "SELECT * FROM submissions WHERE item_id=? AND request_id=?",
                (item_id, request_id),
            ).fetchone()
            if dup is not None:
                self._audit_in_tx("report_replay", ENTITY, item_id, actor, {
                    "request_id": request_id, "resolution": dup["resolution"],
                    "original_submission_id": dup["id"],
                })
                result = dict(dup)
                result["payload"] = json.loads(result.pop("payload_json"))
                result["replayed"] = True
                result["message"] = "同号重复提交，沿用第一次结果"
                return result

            if base_version == item["version"]:
                # 先到：成立，确认读数写入当前版本
                new_quantity = reading if reading is not None else item["quantity"]
                self.conn.execute(
                    """UPDATE items SET quantity=?, version=version+1, updated_at=?
                       WHERE id=?""",
                    (new_quantity, now, item_id),
                )
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at, request_id, base_version, resolution)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, "open", external_ref, actor, now,
                     request_id, base_version, "confirmed"),
                )
                record_id = int(cur.lastrowid)
                cur = self.conn.execute(
                    """INSERT INTO submissions(item_id, request_id, base_version, kind,
                       detail, reading, resolution, payload_json, confirmed_record_id,
                       created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, request_id, base_version, kind, detail, reading,
                     "confirmed", json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     record_id, actor, now),
                )
                submission_id = int(cur.lastrowid)
                invalidated = self._invalidate_open_issuances_in_tx(
                    item_id, f"new_reading:{request_id}", now)
                self._spawn_issuance_in_tx(self._item(
                    self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()),
                    item_id, now, basis=f"report:{request_id}")
                self._audit_in_tx("report_confirmed", ENTITY, item_id, actor, {
                    "request_id": request_id, "base_version": base_version,
                    "record_id": record_id, "reading": reading,
                    "invalidated_issuances": invalidated,
                })
            else:
                # 后到：基准版本过期，内容留作冲突，不能盖掉已确认读数
                cur = self.conn.execute(
                    """INSERT INTO submissions(item_id, request_id, base_version, kind,
                       detail, reading, resolution, payload_json, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, request_id, base_version, kind, detail, reading,
                     "conflict", json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     actor, now),
                )
                submission_id = int(cur.lastrowid)
                self._audit_in_tx("report_conflict", ENTITY, item_id, actor, {
                    "request_id": request_id, "base_version": base_version,
                    "current_version": item["version"], "submission_id": submission_id,
                })
            row = self.conn.execute("SELECT * FROM submissions WHERE id=?",
                                    (submission_id,)).fetchone()
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["replayed"] = False
        return result

    # ---- 旧记录补核 ----

    def add_legacy_record(self, item_id: int, kind: str, detail: str, status: str,
                          external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at, resolution)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now,
                     "pending_verification"),
                )
                record_id = int(cur.lastrowid)
                self._audit_in_tx("record_legacy", ENTITY, item_id, actor, {
                    "record_id": record_id, "kind": kind,
                })
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def verify_record(self, item_id: int, record_id: int, base_version: int,
                      reading: Optional[float], actor: str) -> Dict[str, Any]:
        """坝工程师补核旧记录：基准匹配则成立并带动签发重算。"""
        now = utc_now()
        with self._lock, self.conn:
            item = self._item(self.conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
            rec = self.conn.execute(
                "SELECT * FROM records WHERE id=? AND item_id=?", (record_id, item_id)
            ).fetchone()
            if rec is None:
                raise NotFoundError("巡检记录不存在")
            rec = dict(rec)
            if rec["resolution"] != "pending_verification":
                raise ConflictError(f"该记录已处置为{rec['resolution']}，无需补核")
            if base_version != item["version"]:
                raise ConflictError("基准版本已过期，请刷新后补核")
            new_quantity = reading if reading is not None else item["quantity"]
            self.conn.execute(
                "UPDATE items SET quantity=?, version=version+1, updated_at=? WHERE id=?",
                (new_quantity, now, item_id),
            )
            self.conn.execute(
                "UPDATE records SET resolution='confirmed' WHERE id=?", (record_id,))
            invalidated = self._invalidate_open_issuances_in_tx(
                item_id, f"verification:record:{record_id}", now)
            self._spawn_issuance_in_tx(self._item(
                self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()),
                item_id, now, basis=f"verification:record:{record_id}")
            self._audit_in_tx("record_verified", ENTITY, item_id, actor, {
                "record_id": record_id, "base_version": base_version,
                "reading": reading, "invalidated_issuances": invalidated,
            })
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 应急签发台账 ----

    @staticmethod
    def _issuance_snapshot(item: Dict[str, Any], basis: str,
                           open_records: int = 0) -> Dict[str, Any]:
        """按当时依据冻结优先级与期限，签发后不受后续读数影响。"""
        return {
            "item_id": item["id"], "state": "pending",
            "priority": priority_score(item["severity"], item["quantity"],
                                       item["threshold"], open_records),
            "deadline_hours": response_deadline_hours(
                item["severity"], item["quantity"], item["threshold"]),
            "base_version": item["version"],
            "escalation_required": 1 if escalation_required(
                item["severity"], item["quantity"], item["threshold"]) else 0,
            "severity": item["severity"], "quantity": item["quantity"],
            "threshold": item["threshold"], "basis": basis,
            "created_at": utc_now(),
        }

    def _spawn_issuance_in_tx(self, item: Dict[str, Any], item_id: int,
                              now: str, basis: str) -> int:
        open_records = int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
            (item_id,)).fetchone()["n"])
        snap = self._issuance_snapshot(item, basis, open_records)
        cur = self.conn.execute(
            """INSERT INTO issuances(item_id, state, priority, deadline_hours, base_version,
               escalation_required, severity, quantity, threshold, basis, created_at)
               VALUES(:item_id,:state,:priority,:deadline_hours,:base_version,
               :escalation_required,:severity,:quantity,:threshold,:basis,:created_at)""",
            snap,
        )
        return int(cur.lastrowid)

    def _invalidate_open_issuances_in_tx(self, item_id: int, source: str,
                                         now: str) -> List[int]:
        """新读数或控制阈值变化：未完成签发立即失效；已签发保留当时依据。"""
        rows = self.conn.execute(
            """SELECT id FROM issuances WHERE item_id=? AND state IN (
               'pending','reviewed')""", (item_id,)).fetchall()
        ids = [int(r["id"]) for r in rows]
        if ids:
            self.conn.execute(
                """UPDATE issuances SET state='invalidated', invalidated_by=?,
                   invalidated_at=? WHERE id IN (%s)""" % ",".join(str(i) for i in ids),
                (source, now),
            )
        return ids

    def list_issuances(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM issuances WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["escalation_required"] = bool(d["escalation_required"])
            result.append(d)
        return result

    def review_issuance(self, item_id: int, issuance_id: int, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM issuances WHERE id=? AND item_id=?",
                                    (issuance_id, item_id)).fetchone()
            if row is None:
                raise NotFoundError("签发单不存在")
            issuance = dict(row)
            if issuance["state"] != "pending":
                raise ConflictError(f"签发单当前状态为{issuance['state']}，不能复核")
            if expected_version != issuance["base_version"]:
                raise ConflictError("签发依据已过期，请基于最新版本重新发起")
            self.conn.execute(
                "UPDATE issuances SET state='reviewed', reviewed_by=?, reviewed_at=? WHERE id=?",
                (actor, now, issuance_id),
            )
            self._audit_in_tx("issuance_review", ENTITY, item_id, actor, {
                "issuance_id": issuance_id, "base_version": expected_version,
            })
            row = self.conn.execute("SELECT * FROM issuances WHERE id=?",
                                    (issuance_id,)).fetchone()
        d = dict(row); d["escalation_required"] = bool(d["escalation_required"])
        return d

    def issue_issuance(self, item_id: int, issuance_id: int, expected_version: int,
                       conclusion: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM issuances WHERE id=? AND item_id=?",
                                    (issuance_id, item_id)).fetchone()
            if row is None:
                raise NotFoundError("签发单不存在")
            issuance = dict(row)
            if issuance["state"] == "issued":
                raise ConflictError("签发单已签发")
            if issuance["state"] != "reviewed":
                raise ConflictError(f"签发单当前状态为{issuance['state']}，须先经坝工程师复核")
            if expected_version != issuance["base_version"]:
                raise ConflictError("签发依据已过期，请基于最新版本重新发起")
            self.conn.execute(
                """UPDATE issuances SET state='issued', conclusion=?, issued_by=?,
                   issued_at=? WHERE id=?""",
                (conclusion, actor, now, issuance_id),
            )
            self._audit_in_tx("issuance_issued", ENTITY, item_id, actor, {
                "issuance_id": issuance_id, "base_version": expected_version,
                "priority": issuance["priority"], "deadline_hours": issuance["deadline_hours"],
            })
            row = self.conn.execute("SELECT * FROM issuances WHERE id=?",
                                    (issuance_id,)).fetchone()
        d = dict(row); d["escalation_required"] = bool(d["escalation_required"])
        return d

    def change_threshold(self, item_id: int, new_threshold: float,
                         base_version: int, actor: str) -> Dict[str, Any]:
        """控制阈值变化：未完成签发立即失效并重算优先级和期限。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("缺陷不存在")
            item = self._item(row)
            if base_version != item["version"]:
                raise ConflictError("版本冲突，请刷新后重试")
            if abs(new_threshold - item["threshold"]) < 1e-9:
                raise ValidationError("新阈值与当前阈值相同")
            self.conn.execute(
                "UPDATE items SET threshold=?, version=version+1, updated_at=? WHERE id=?",
                (new_threshold, now, item_id),
            )
            invalidated = self._invalidate_open_issuances_in_tx(
                item_id, f"threshold_change:{item['threshold']}->{new_threshold}", now)
            self._spawn_issuance_in_tx(self._item(
                self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()),
                item_id, now,
                basis=f"threshold_change:{item['threshold']}->{new_threshold}")
            self._audit_in_tx("threshold_change", ENTITY, item_id, actor, {
                "old_threshold": item["threshold"], "new_threshold": new_threshold,
                "base_version": base_version, "invalidated_issuances": invalidated,
            })
        return self.get_item(item_id)

    def list_submissions(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM submissions WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["payload"] = json.loads(d.pop("payload_json"))
            result.append(d)
        return result

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            event_id = self._audit_in_tx(action, entity_type, entity_id, actor, detail)
        with self._lock:
            row = self.conn.execute("SELECT * FROM audit_events WHERE id=?",
                                    (event_id,)).fetchone()
        event = dict(row)
        event["detail"] = json.loads(event["detail"])
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
