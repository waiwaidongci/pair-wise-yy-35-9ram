from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (PERIOD_STATUSES, ID_PREFIX, STATES, SUMMARY_STATUSES)


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

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({item_statuses})),
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
                    UNIQUE(item_id, external_ref)
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
                CREATE TABLE IF NOT EXISTS dose_persons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dose_periods (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ({period_statuses})),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sealed_by TEXT,
                    sealed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS dose_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL REFERENCES dose_persons(id),
                    period TEXT NOT NULL,
                    source TEXT NOT NULL,
                    external_ref TEXT NOT NULL,
                    value REAL NOT NULL CHECK(value >= 0),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','superseded')),
                    superseded_by INTEGER REFERENCES dose_readings(id),
                    reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_dose_readings_ref
                    ON dose_readings(period, source, external_ref)
                    WHERE status='active';
                CREATE INDEX IF NOT EXISTS ix_dose_readings_person_period
                    ON dose_readings(person_id, period);
                CREATE TABLE IF NOT EXISTS dose_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL REFERENCES dose_persons(id),
                    period TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({summary_statuses})),
                    total_msv REAL NOT NULL,
                    sources TEXT NOT NULL,
                    investigation_required INTEGER NOT NULL,
                    investigation_reasons TEXT NOT NULL,
                    previous_total_msv REAL,
                    previous_status TEXT,
                    previous_version INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_by TEXT,
                    confirmed_at TEXT,
                    UNIQUE(person_id, period, version)
                );
            """.format(item_statuses=statuses,
                       period_statuses=",".join("'" + s + "'" for s in PERIOD_STATUSES),
                       summary_statuses=",".join("'" + s + "'" for s in SUMMARY_STATUSES)))

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

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

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
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

    # ----- 月度归集：人员 -----
    def create_dose_person(self, code: str, name: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dose_persons(code, name, created_by, created_at)
                       VALUES(?,?,?,?)""",
                    (code, name, actor, now))
                person_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("人员编号已存在") from exc
        return self.get_dose_person(person_id)

    def get_dose_person(self, person_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_persons WHERE id=?", (person_id,)).fetchone()
        if row is None:
            raise NotFoundError("人员不存在")
        return dict(row)

    def list_dose_persons(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_persons ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    # ----- 月度归集：周期 -----
    def ensure_dose_period(self, period: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO dose_periods(period, status, created_by, created_at)
                   VALUES(?, 'open', ?, ?)
                   ON CONFLICT(period) DO NOTHING""",
                (period, actor, now))
            del cur
        return self.get_dose_period(period)

    def get_dose_period(self, period: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_periods WHERE period=?", (period,)).fetchone()
        return dict(row) if row else None

    def list_dose_periods(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_periods ORDER BY period DESC").fetchall()
        return [dict(row) for row in rows]

    def seal_dose_period(self, period: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE dose_periods SET status='sealed', sealed_by=?, sealed_at=?
                   WHERE period=? AND status='open'""",
                (actor, now, period))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM dose_periods WHERE period=?", (period,)).fetchone()
                if exists is None:
                    raise NotFoundError("周期不存在")
                raise ConflictError("周期已封存")
            # 封存即确认该周期所有草稿结果；已确认结果保持不变
            self.conn.execute(
                """UPDATE dose_summaries SET status='confirmed', confirmed_by=?, confirmed_at=?
                   WHERE period=? AND status='draft'""",
                (actor, now, period))
        return self.get_dose_period(period)

    # ----- 月度归集：读数 -----
    def add_dose_reading(self, person_id: int, period: str, source: str,
                         external_ref: str, value: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dose_readings(person_id, period, source, external_ref,
                       value, status, created_by, created_at)
                       VALUES(?,?,?,?,?, 'active', ?,?)""",
                    (person_id, period, source, external_ref, value, actor, now))
                reading_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一周期同一来源的记录编号已存在，可能为重复上报") from exc
        return self.get_dose_reading(reading_id)

    def correct_dose_reading(self, reading_id: int, value: float, reason: str,
                             actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            old = self.conn.execute(
                "SELECT * FROM dose_readings WHERE id=?", (reading_id,)).fetchone()
            if old is None:
                raise NotFoundError("读数不存在")
            if old["status"] == "superseded":
                raise ConflictError("该读数已被更正，不能再次更正")
            # 先释放唯一键位（部分索引只约束active），再插入新读数
            self.conn.execute(
                "UPDATE dose_readings SET status='superseded' WHERE id=?",
                (reading_id,))
            cur = self.conn.execute(
                """INSERT INTO dose_readings(person_id, period, source, external_ref,
                   value, status, reason, created_by, created_at)
                   VALUES(?,?,?,?,?, 'active', ?,?,?)""",
                (old["person_id"], old["period"], old["source"], old["external_ref"],
                 value, reason, actor, now))
            new_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE dose_readings SET superseded_by=? WHERE id=?",
                (new_id, reading_id))
        return self.get_dose_reading(new_id)

    def get_dose_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def list_dose_readings(self, person_id: int, period: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM dose_readings WHERE person_id=? AND period=?
                   ORDER BY id""", (person_id, period)).fetchall()
        return [dict(row) for row in rows]

    # ----- 月度归集：归集结果（版本） -----
    def latest_dose_summary(self, person_id: int, period: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM dose_summaries WHERE person_id=? AND period=?
                   ORDER BY version DESC LIMIT 1""", (person_id, period)).fetchone()
        return self._summary(row) if row else None

    def previous_dose_summary(self, person_id: int, prev_period: str) -> Optional[Dict[str, Any]]:
        # 优先取已确认版本，其次取最新草稿
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM dose_summaries WHERE person_id=? AND period=?
                   ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END, version DESC
                   LIMIT 1""", (person_id, prev_period)).fetchone()
        return self._summary(row) if row else None

    @staticmethod
    def _summary(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["sources"] = json.loads(item["sources"])
        item["investigation_reasons"] = json.loads(item["investigation_reasons"])
        item["investigation_required"] = bool(item["investigation_required"])
        return item

    def list_dose_summaries(self, period: Optional[str] = None) -> List[Dict[str, Any]]:
        if period is None:
            sql = "SELECT * FROM dose_summaries ORDER BY period DESC, person_id, version DESC"
            params: tuple = ()
        else:
            sql = "SELECT * FROM dose_summaries WHERE period=? ORDER BY person_id, version DESC"
            params = (period,)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._summary(row) for row in rows]

    def save_dose_summary(self, person_id: int, period: str, sealed: bool,
                          data: dict, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            draft = self.conn.execute(
                """SELECT * FROM dose_summaries WHERE person_id=? AND period=?
                   AND status='draft' ORDER BY version DESC LIMIT 1""",
                (person_id, period)).fetchone()
            if sealed or draft is None:
                if sealed:
                    row = self.conn.execute(
                        "SELECT COALESCE(MAX(version),0)+1 AS v FROM dose_summaries WHERE person_id=? AND period=?",
                        (person_id, period)).fetchone()
                    version = int(row["v"])
                else:
                    version = 1
                self.conn.execute(
                    """INSERT INTO dose_summaries(person_id, period, version, status, total_msv,
                       sources, investigation_required, investigation_reasons,
                       previous_total_msv, previous_status, previous_version,
                       created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (person_id, period, version, "draft", data["total_msv"],
                     json.dumps(data["sources"], ensure_ascii=False, sort_keys=True),
                     1 if data["investigation_required"] else 0,
                     json.dumps(data["investigation_reasons"], ensure_ascii=False),
                     data["previous_total_msv"], data["previous_status"],
                     data["previous_version"], actor, now))
            else:
                version = draft["version"]
                self.conn.execute(
                    """UPDATE dose_summaries SET total_msv=?, sources=?,
                       investigation_required=?, investigation_reasons=?,
                       previous_total_msv=?, previous_status=?, previous_version=?
                       WHERE id=?""",
                    (data["total_msv"], json.dumps(data["sources"], ensure_ascii=False, sort_keys=True),
                     1 if data["investigation_required"] else 0,
                     json.dumps(data["investigation_reasons"], ensure_ascii=False),
                     data["previous_total_msv"], data["previous_status"],
                     data["previous_version"], draft["id"]))
        return self.latest_dose_summary(person_id, period)

    def confirm_dose_summary(self, person_id: int, period: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE dose_summaries SET status='confirmed', confirmed_by=?, confirmed_at=?
                   WHERE person_id=? AND period=? AND status='draft'""",
                (actor, now, person_id, period))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM dose_summaries WHERE person_id=? AND period=?",
                    (person_id, period)).fetchone()
                if exists is None:
                    raise NotFoundError("归集结果不存在")
                raise ConflictError("归集结果已确认，不能重复确认")
        return self.latest_dose_summary(person_id, period)

    def latest_confirmed_summary_for_period(self, period: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM dose_summaries WHERE period=? AND status='confirmed'
                   ORDER BY version DESC LIMIT 1""", (period,)).fetchone()
        return self._summary(row) if row else None
