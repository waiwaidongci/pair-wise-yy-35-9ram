from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_id, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, COLLECT_ROLES, CONFIRM_ROLES, CREATE_ROLES,
                    ENTITY, INVESTIGATION_LEVEL_MSV, PERSON_ENTITY,
                    PERSON_CREATE_ROLES, PERIOD_ENTITY, READING_ENTITY,
                    READING_ROLES, RECORD_ROLES, SEAL_ROLES, SUMMARY_ENTITY,
                    SUMMARY_STATUSES, TITLE, VIEW_ROLES, aggregate_by_source,
                    completion_blockers, escalation_required,
                    investigation_decision, normalize_period,
                    previous_period, priority_score, response_deadline_hours,
                    role_for_transition, total_from_sources,
                    validate_transition)


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
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

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
            from .domain import ConflictError
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

    # ----- 月度归集 -----
    def register_person(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, PERSON_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 50)
        name = require_text(payload.get("name"), "name", 100)
        person = self.repository.create_dose_person(code, name, actor)
        self.repository.append_audit("register_person", PERSON_ENTITY,
                                     person["id"], actor, {"code": code, "name": name})
        return person

    def list_persons(self, role: str) -> list:
        self._view(role)
        return self.repository.list_dose_persons()

    def submit_reading(self, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, READING_ROLES)
        actor = require_text(actor, "actor", 100)
        person_id = require_id(payload.get("person_id"), "person_id")
        period = normalize_period(payload.get("period"))
        source = require_text(payload.get("source"), "source", 100)
        external_ref = require_text(payload.get("external_ref"), "external_ref", 100)
        value = require_number(payload.get("value"), "value")
        person = self.repository.get_dose_person(person_id)
        period_row = self.repository.ensure_dose_period(period, actor)
        if period_row["status"] == "sealed":
            raise ConflictError("周期已封存，读数必须以更正形式进入新版本")
        reading = self.repository.add_dose_reading(
            person_id, period, source, external_ref, value, actor)
        self.repository.append_audit("submit_reading", READING_ENTITY, reading["id"],
                                     actor, {"person_id": person_id, "person_code": person["code"],
                                             "period": period, "source": source, "value": value})
        return reading

    def correct_reading(self, reading_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, READING_ROLES)
        actor = require_text(actor, "actor", 100)
        reading_id = require_id(reading_id, "reading_id")
        value = require_number(payload.get("value"), "value")
        reason = require_text(payload.get("reason"), "reason")
        old = self.repository.get_dose_reading(reading_id)
        # 更正只取代旧值，原读数作为superseded记录保留
        reading = self.repository.correct_dose_reading(reading_id, value, reason, actor)
        self.repository.append_audit("correct_reading", READING_ENTITY, reading["id"],
                                     actor, {"person_id": old["person_id"], "period": old["period"],
                                             "source": old["source"],
                                             "old_reading_id": reading_id,
                                             "old_value": old["value"], "new_value": value,
                                             "reason": reason})
        return reading

    def list_readings(self, person_id: int, period: str, role: str) -> list:
        self._view(role)
        person_id = require_id(person_id, "person_id")
        period = normalize_period(period)
        self.repository.get_dose_person(person_id)
        return self.repository.list_dose_readings(person_id, period)

    def collect_month(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, COLLECT_ROLES)
        actor = require_text(actor, "actor", 100)
        person_id = require_id(payload.get("person_id"), "person_id")
        period = normalize_period(payload.get("period"))
        person = self.repository.get_dose_person(person_id)
        period_row = self.repository.ensure_dose_period(period, actor)
        view = self._dose_view(person_id, period, period_row["status"])
        saved = self.repository.save_dose_summary(
            person_id, period, period_row["status"] == "sealed",
            {"total_msv": view["total_msv"], "sources": view["source_details"],
             "investigation_required": view["investigation_required"],
             "investigation_reasons": view["investigation_reasons"],
             "previous_total_msv": view["previous_period"]["total_msv"],
             "previous_status": view["previous_period"]["status"],
             "previous_version": view["previous_period"]["version"]}, actor)
        self.repository.append_audit("collect_month", SUMMARY_ENTITY, saved["id"], actor, {
            "person_id": person_id, "person_code": person["code"], "period": period,
            "version": saved["version"], "status": saved["status"],
            "total_msv": saved["total_msv"], "sealed_period": period_row["status"] == "sealed",
            "investigation_required": saved["investigation_required"]})
        return self._dose_view(person_id, period, period_row["status"])

    def seal_period(self, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, SEAL_ROLES)
        actor = require_text(actor, "actor", 100)
        period = normalize_period(payload.get("period"))
        period_row = self.repository.get_dose_period(period)
        if period_row is None:
            from .domain import NotFoundError
            raise NotFoundError("周期不存在，尚无读数或归集结果")
        sealed = self.repository.seal_dose_period(period, actor)
        self.repository.append_audit("seal_period", PERIOD_ENTITY, period_row["id"], actor,
                                     {"period": period})
        return sealed

    def confirm_summary(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        person_id = require_id(payload.get("person_id"), "person_id")
        period = normalize_period(payload.get("period"))
        person = self.repository.get_dose_person(person_id)
        confirmed = self.repository.confirm_dose_summary(person_id, period, actor)
        self.repository.append_audit("confirm_summary", SUMMARY_ENTITY, confirmed["id"], actor,
                                     {"person_id": person_id, "person_code": person["code"],
                                      "period": period, "version": confirmed["version"],
                                      "total_msv": confirmed["total_msv"]})
        return self._dose_view(person_id, period)

    def list_periods(self, role: str) -> list:
        self._view(role)
        return self.repository.list_dose_periods()

    def list_month_summaries(self, role: str, period: Optional[str] = None) -> list:
        self._view(role)
        if period is not None:
            period = normalize_period(period)
        return self.repository.list_dose_summaries(period)

    def get_month_view(self, person_id: int, period: str, role: str) -> Dict[str, Any]:
        self._view(role)
        person_id = require_id(person_id, "person_id")
        period = normalize_period(period)
        return self._dose_view(person_id, period)

    def _dose_view(self, person_id: int, period: str,
                   period_status: Optional[str] = None) -> Dict[str, Any]:
        person = self.repository.get_dose_person(person_id)
        period_row = self.repository.get_dose_period(period)
        if period_status is None:
            period_status = period_row["status"] if period_row else "open"
        readings = self.repository.list_dose_readings(person_id, period)
        sources = aggregate_by_source(readings)
        total = total_from_sources(sources)

        prev_period_code = previous_period(period)
        prev = self.repository.previous_dose_summary(person_id, prev_period_code)
        prev_total = prev["total_msv"] if prev else None
        required, reasons = investigation_decision(total, prev_total)
        if prev_total is None:
            delta = None
            ratio = None
        else:
            delta = round(total - prev_total, 6)
            ratio = None if prev_total == 0 else round(total / prev_total, 4)

        summary = self.repository.latest_dose_summary(person_id, period)
        result = {
            "person": {"id": person["id"], "code": person["code"], "name": person["name"]},
            "period": period,
            "period_status": period_status,
            "investigation_level_msv": INVESTIGATION_LEVEL_MSV,
            "source_details": sources,
            "readings": readings,
            "total_msv": total,
            "previous_period": {"period": prev_period_code,
                                "version": prev["version"] if prev else None,
                                "status": prev["status"] if prev else None,
                                "total_msv": prev_total,
                                "delta_msv": delta, "change_ratio": ratio},
            "investigation_required": required,
            "investigation_reasons": reasons,
        }
        if summary is not None:
            result["latest_version"] = summary["version"]
            result["latest_status"] = summary["status"]
            if summary["status"] == SUMMARY_STATUSES[1]:
                # 已确认结果是封存快照：查看时返回快照，不随后续读数变化
                result["source_details"] = summary["sources"]
                result["total_msv"] = summary["total_msv"]
                result["investigation_required"] = summary["investigation_required"]
                result["investigation_reasons"] = summary["investigation_reasons"]
                result["snapshot"] = True
            else:
                result["snapshot"] = False
        else:
            result["latest_version"] = None
            result["latest_status"] = None
            result["snapshot"] = False
        # 最新版为草稿时，附带最近一次已确认基线，便于核对已确认结果不随草稿改变
        result["confirmed_snapshot"] = None
        if result["latest_status"] == "draft":
            confirmed = self.repository.latest_confirmed_summary_for_period(period)
            if confirmed is not None and confirmed["person_id"] == person_id:
                result["confirmed_snapshot"] = {
                    "version": confirmed["version"], "total_msv": confirmed["total_msv"],
                    "sources": confirmed["sources"],
                    "investigation_required": confirmed["investigation_required"],
                    "investigation_reasons": confirmed["investigation_reasons"],
                    "confirmed_by": confirmed["confirmed_by"],
                    "confirmed_at": confirmed["confirmed_at"]}
        return result
