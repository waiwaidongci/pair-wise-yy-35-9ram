from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


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
                CREATE TABLE IF NOT EXISTS dose_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    source TEXT NOT NULL,
                    dose_msv REAL NOT NULL CHECK(dose_msv >= 0),
                    external_ref TEXT NOT NULL,
                    supersedes_id INTEGER REFERENCES dose_readings(id),
                    status TEXT NOT NULL DEFAULT 'effective'
                        CHECK(status IN ('effective','superseded')),
                    reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(person_id, period, source, external_ref)
                );
                CREATE INDEX IF NOT EXISTS ix_dose_readings_person_period
                    ON dose_readings(person_id, period);
                CREATE TABLE IF NOT EXISTS dose_periods (
                    person_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','sealed')),
                    sealed_by TEXT,
                    sealed_at TEXT,
                    PRIMARY KEY(person_id, period)
                );
                CREATE TABLE IF NOT EXISTS dose_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    total_msv REAL NOT NULL,
                    breakdown TEXT NOT NULL,
                    reading_count INTEGER NOT NULL,
                    prev_period TEXT,
                    prev_total_msv REAL,
                    delta_msv REAL,
                    investigation_required INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'provisional'
                        CHECK(status IN ('provisional','confirmed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(person_id, period, version)
                );
            """)

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

    @staticmethod
    def _summary(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["breakdown"] = json.loads(item["breakdown"])
        item["investigation_required"] = bool(item["investigation_required"])
        return item

    def _ensure_dose_period(self, person_id: str, period: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM dose_periods WHERE person_id=? AND period=?",
            (person_id, period),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO dose_periods(person_id, period, status) VALUES(?,?, 'open')",
                (person_id, period),
            )
            row = self.conn.execute(
                "SELECT * FROM dose_periods WHERE person_id=? AND period=?",
                (person_id, period),
            ).fetchone()
        return dict(row)

    def get_dose_period(self, person_id: str, period: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_periods WHERE person_id=? AND period=?",
                (person_id, period),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_dose_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_readings WHERE id=?", (reading_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def add_dose_reading(self, person_id: str, period: str, source: str,
                         dose_msv: float, external_ref: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                period_row = self._ensure_dose_period(person_id, period)
                if period_row["status"] == "sealed":
                    raise ConflictError("周期已封存，新读数请走更正流程")
                cur = self.conn.execute(
                    """INSERT INTO dose_readings(person_id, period, source, dose_msv,
                       external_ref, supersedes_id, status, reason, created_by, created_at)
                       VALUES(?,?,?,?,?,NULL,'effective',NULL,?,?)""",
                    (person_id, period, source, dose_msv, external_ref, actor, now),
                )
                reading_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("重复上报：该来源标识已存在") from exc
        return self.get_dose_reading(reading_id)

    def correct_dose_reading(self, reading_id: int, dose_msv: float, external_ref: str,
                             reason: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                row = self.conn.execute(
                    "SELECT * FROM dose_readings WHERE id=?", (reading_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("读数不存在")
                if row["status"] != "effective":
                    raise ConflictError("该读数已被更正，请对最新读数发起更正")
                cur = self.conn.execute(
                    """INSERT INTO dose_readings(person_id, period, source, dose_msv,
                       external_ref, supersedes_id, status, reason, created_by, created_at)
                       VALUES(?,?,?,?,?,?,'effective',?,?,?)""",
                    (row["person_id"], row["period"], row["source"], dose_msv,
                     external_ref, reading_id, reason, actor, now),
                )
                new_id = int(cur.lastrowid)
                self.conn.execute(
                    "UPDATE dose_readings SET status='superseded' WHERE id=?", (reading_id,)
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("重复上报：该来源标识已存在") from exc
        return self.get_dose_reading(new_id)

    def list_dose_readings(self, person_id: str, period: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_readings WHERE person_id=? AND period=? ORDER BY id",
                (person_id, period),
            ).fetchall()
        return [dict(row) for row in rows]

    def effective_dose_readings(self, person_id: str, period: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM dose_readings
                   WHERE person_id=? AND period=? AND status='effective' ORDER BY id""",
                (person_id, period),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dose_summary(self, person_id: str, period: str,
                         version: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            if version is None:
                row = self.conn.execute(
                    """SELECT * FROM dose_summaries WHERE person_id=? AND period=?
                       ORDER BY version DESC LIMIT 1""",
                    (person_id, period),
                ).fetchone()
            else:
                row = self.conn.execute(
                    """SELECT * FROM dose_summaries
                       WHERE person_id=? AND period=? AND version=?""",
                    (person_id, period, version),
                ).fetchone()
        if row is None:
            raise NotFoundError("归集结果不存在")
        return self._summary(row)

    def latest_dose_summary(self, person_id: str, period: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM dose_summaries WHERE person_id=? AND period=?
                   ORDER BY version DESC LIMIT 1""",
                (person_id, period),
            ).fetchone()
        return self._summary(row) if row is not None else None

    def confirmed_dose_summary(self, person_id: str, period: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM dose_summaries
                   WHERE person_id=? AND period=? AND status='confirmed'""",
                (person_id, period),
            ).fetchone()
        return self._summary(row) if row is not None else None

    def save_dose_summary(self, person_id: str, period: str, data: Dict[str, Any],
                          actor: str) -> Dict[str, Any]:
        now = utc_now()
        breakdown = json.dumps(data["breakdown"], ensure_ascii=False, sort_keys=True)
        required = 1 if data["investigation_required"] else 0
        with self._lock, self.conn:
            period_row = self._ensure_dose_period(person_id, period)
            if period_row["status"] == "sealed":
                row = self.conn.execute(
                    """SELECT MAX(version) AS v FROM dose_summaries
                       WHERE person_id=? AND period=?""",
                    (person_id, period),
                ).fetchone()
                version = int(row["v"] or 0) + 1
                self.conn.execute(
                    """INSERT INTO dose_summaries(person_id, period, version, total_msv,
                       breakdown, reading_count, prev_period, prev_total_msv, delta_msv,
                       investigation_required, status, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'provisional',?,?)""",
                    (person_id, period, version, data["total_msv"], breakdown,
                     data["reading_count"], data["prev_period"], data["prev_total_msv"],
                     data["delta_msv"], required, actor, now),
                )
            else:
                existing = self.conn.execute(
                    """SELECT id FROM dose_summaries
                       WHERE person_id=? AND period=? AND status='provisional'""",
                    (person_id, period),
                ).fetchone()
                if existing is not None:
                    self.conn.execute(
                        """UPDATE dose_summaries SET total_msv=?, breakdown=?,
                           reading_count=?, prev_period=?, prev_total_msv=?, delta_msv=?,
                           investigation_required=?, created_by=?, created_at=?
                           WHERE id=?""",
                        (data["total_msv"], breakdown, data["reading_count"],
                         data["prev_period"], data["prev_total_msv"], data["delta_msv"],
                         required, actor, now, existing["id"]),
                    )
                else:
                    self.conn.execute(
                        """INSERT INTO dose_summaries(person_id, period, version, total_msv,
                           breakdown, reading_count, prev_period, prev_total_msv, delta_msv,
                           investigation_required, status, created_by, created_at)
                           VALUES(?,?,1,?,?,?,?,?,?,?,'provisional',?,?)""",
                        (person_id, period, data["total_msv"], breakdown,
                         data["reading_count"], data["prev_period"],
                         data["prev_total_msv"], data["delta_msv"], required, actor, now),
                    )
        return self.get_dose_summary(person_id, period)

    def seal_dose_period(self, person_id: str, period: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            period_row = self._ensure_dose_period(person_id, period)
            if period_row["status"] == "sealed":
                raise ConflictError("周期已封存")
            summary = self.conn.execute(
                """SELECT id FROM dose_summaries
                   WHERE person_id=? AND period=? AND status='provisional'
                   ORDER BY version DESC LIMIT 1""",
                (person_id, period),
            ).fetchone()
            if summary is None:
                raise ConflictError("尚无归集结果，请先归集再封存")
            self.conn.execute(
                """UPDATE dose_periods SET status='sealed', sealed_by=?, sealed_at=?
                   WHERE person_id=? AND period=?""",
                (actor, now, person_id, period),
            )
            self.conn.execute(
                "UPDATE dose_summaries SET status='confirmed' WHERE id=?",
                (summary["id"],),
            )
            summary_id = int(summary["id"])
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_summaries WHERE id=?", (summary_id,)
            ).fetchone()
        return self._summary(row)

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
