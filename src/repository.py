from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (LEDGER_STATUSES, NON_ISSUED_SIGNOFF_STATUSES, SIGNOFF_STATUSES,
                    STATES)


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
        self._migrate()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
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
                    request_no TEXT,
                    baseline_version INTEGER,
                    measure REAL,
                    ledger_status TEXT NOT NULL DEFAULT 'confirmed'
                        CHECK(ledger_status IN ('confirmed','conflict','pending_supplement')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_records_request_no
                    ON records(request_no) WHERE request_no IS NOT NULL;
                CREATE TABLE IF NOT EXISTS signoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL
                        CHECK(status IN ('draft','reviewed','issued','invalid')),
                    basis_severity TEXT NOT NULL,
                    basis_quantity REAL NOT NULL,
                    basis_threshold REAL NOT NULL,
                    basis_priority INTEGER NOT NULL,
                    basis_deadline_hours INTEGER NOT NULL,
                    basis_escalation INTEGER NOT NULL DEFAULT 0,
                    invalidation_source_type TEXT,
                    invalidation_source_id INTEGER,
                    invalidation_detail TEXT,
                    review_note TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    issued_by TEXT,
                    issued_at TEXT
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

    def _add_column_if_missing(self, table: str, column: str, decl: str) -> None:
        cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _migrate(self) -> None:
        """旧库补齐版本台账列；缺请求号或基准版本的旧记录升级为待补核。"""
        with self.conn:
            self._add_column_if_missing("records", "request_no", "TEXT")
            self._add_column_if_missing("records", "baseline_version", "INTEGER")
            self._add_column_if_missing("records", "measure", "REAL")
            self._add_column_if_missing(
                "records", "ledger_status",
                "TEXT NOT NULL DEFAULT 'confirmed' CHECK(ledger_status IN ('confirmed','conflict','pending_supplement'))")
            self.conn.execute(
                "UPDATE records SET ledger_status='pending_supplement' "
                "WHERE request_no IS NULL OR baseline_version IS NULL")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    # ---- items ----
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
            raise NotFoundError("项目不存在")
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
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def change_threshold(self, item_id: int, new_threshold: float,
                         expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET threshold=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (new_threshold, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def confirm_reading(self, item_id: int, expected_version: int,
                        measure: Optional[float]) -> Dict[str, Any]:
        """确认读数：版本号 +1，若带测值则同步更新缺陷量值。"""
        now = utc_now()
        with self._lock, self.conn:
            if measure is not None:
                cur = self.conn.execute(
                    """UPDATE items SET quantity=?, version=version+1, updated_at=?
                       WHERE id=? AND version=?""",
                    (measure, now, item_id, expected_version),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE items SET version=version+1, updated_at=?
                       WHERE id=? AND version=?""",
                    (now, item_id, expected_version),
                )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    # ---- records ----
    def find_record_by_request_no(self, request_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM records WHERE request_no=?", (request_no,)
            ).fetchone()
        return dict(row) if row else None

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   request_no: Optional[str] = None,
                   baseline_version: Optional[int] = None,
                   ledger_status: str = "confirmed",
                   measure: Optional[float] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       request_no, baseline_version, measure, ledger_status,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, request_no,
                     baseline_version, measure, ledger_status, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        return self.get_record(record_id)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("记录不存在")
        return dict(row)

    def update_record_ledger(self, record_id: int, request_no: str,
                             baseline_version: int,
                             ledger_status: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE records SET request_no=?, baseline_version=?, ledger_status=?
                   WHERE id=?""",
                (request_no, baseline_version, ledger_status, record_id),
            )
        return self.get_record(record_id)

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

    # ---- signoffs ----
    def create_signoff(self, item_id: int, basis: Dict[str, Any],
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO signoffs(item_id, status, basis_severity, basis_quantity,
                   basis_threshold, basis_priority, basis_deadline_hours, basis_escalation,
                   created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item_id, "draft", basis["severity"], basis["quantity"], basis["threshold"],
                 basis["priority"], basis["deadline_hours"],
                 1 if basis["escalation_required"] else 0, actor, now),
            )
            signoff_id = int(cur.lastrowid)
        return self.get_signoff(signoff_id)

    def get_signoff(self, signoff_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM signoffs WHERE id=?", (signoff_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("签发不存在")
        return dict(row)

    def list_signoffs(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM signoffs WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_signoff(self, signoff_id: int, **fields: Any) -> Dict[str, Any]:
        allowed = {"status", "review_note", "reviewed_by", "reviewed_at",
                   "issued_by", "issued_at"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return self.get_signoff(signoff_id)
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE signoffs SET {cols} WHERE id=?",
                (*sets.values(), signoff_id),
            )
        return self.get_signoff(signoff_id)

    def invalidate_signoffs(self, item_id: int, source_type: str,
                            source_id: Optional[int], detail: Dict[str, Any]) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"""UPDATE signoffs SET status='invalid', invalidation_source_type=?,
                   invalidation_source_id=?, invalidation_detail=?
                   WHERE item_id=? AND status IN
                   ({','.join('?' for _ in NON_ISSUED_SIGNOFF_STATUSES)})""",
                (source_type, source_id,
                 json.dumps(detail, ensure_ascii=False, sort_keys=True),
                 item_id, *NON_ISSUED_SIGNOFF_STATUSES),
            )
            return int(cur.rowcount)

    # ---- audit ----
    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
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
            event_id = int(cur.lastrowid)
        event["id"] = event_id
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
