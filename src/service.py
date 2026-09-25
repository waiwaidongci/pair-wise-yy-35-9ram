from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DOSE_AGGREGATE_ROLES,
                    DOSE_CORRECT_ROLES, DOSE_READING_ROLES, DOSE_SEAL_ROLES,
                    ENTITY, MONTHLY_INVESTIGATION_LEVEL_MSV, RECORD_ROLES,
                    TITLE, VIEW_ROLES, aggregate_by_source, completion_blockers,
                    dose_conclusion, escalation_required, investigation_required,
                    previous_period, priority_score, response_deadline_hours,
                    role_for_transition, validate_period, validate_transition)


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

    def submit_reading(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DOSE_READING_ROLES)
        actor = require_text(actor, "actor", 100)
        person_id = require_text(payload.get("person_id"), "person_id", 100)
        period = validate_period(payload.get("period"))
        source = require_text(payload.get("source"), "source", 100)
        dose_msv = require_number(payload.get("dose_msv"), "dose_msv")
        external_ref = require_text(payload.get("external_ref"), "external_ref", 100)
        reading = self.repository.add_dose_reading(person_id, period, source, dose_msv,
                                                   external_ref, actor)
        self.repository.append_audit("dose_reading", "剂量读数", reading["id"], actor, {
            "person_id": person_id, "period": period, "source": source,
            "dose_msv": dose_msv,
        })
        return reading

    def correct_reading(self, reading_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DOSE_CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        dose_msv = require_number(payload.get("dose_msv"), "dose_msv")
        external_ref = require_text(payload.get("external_ref"), "external_ref", 100)
        reason = payload.get("reason")
        if reason is not None:
            reason = require_text(reason, "reason", 500)
        reading = self.repository.correct_dose_reading(reading_id, dose_msv, external_ref,
                                                       reason, actor)
        self.repository.append_audit("dose_correction", "剂量读数", reading["id"], actor, {
            "person_id": reading["person_id"], "period": reading["period"],
            "supersedes_id": reading_id, "dose_msv": dose_msv,
        })
        return reading

    def aggregate_period(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DOSE_AGGREGATE_ROLES)
        actor = require_text(actor, "actor", 100)
        person_id = require_text(payload.get("person_id"), "person_id", 100)
        period = validate_period(payload.get("period"))
        readings = self.repository.effective_dose_readings(person_id, period)
        breakdown, total = aggregate_by_source(readings)
        prev_period = previous_period(period)
        previous = (self.repository.confirmed_dose_summary(person_id, prev_period)
                    or self.repository.latest_dose_summary(person_id, prev_period))
        prev_total = previous["total_msv"] if previous is not None else None
        delta = round(total - prev_total, 4) if prev_total is not None else None
        summary = self.repository.save_dose_summary(person_id, period, {
            "total_msv": total, "breakdown": breakdown, "reading_count": len(readings),
            "prev_period": prev_period, "prev_total_msv": prev_total, "delta_msv": delta,
            "investigation_required": investigation_required(total),
        }, actor)
        self.repository.append_audit("dose_aggregate", "剂量归集", summary["id"], actor, {
            "person_id": person_id, "period": period, "version": summary["version"],
            "total_msv": total, "investigation_required": summary["investigation_required"],
        })
        return self._summary_view(summary)

    def seal_period(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DOSE_SEAL_ROLES)
        actor = require_text(actor, "actor", 100)
        person_id = require_text(payload.get("person_id"), "person_id", 100)
        period = validate_period(payload.get("period"))
        summary = self.repository.seal_dose_period(person_id, period, actor)
        self.repository.append_audit("dose_seal", "剂量归集", summary["id"], actor, {
            "person_id": person_id, "period": period, "version": summary["version"],
            "total_msv": summary["total_msv"],
        })
        return self._summary_view(summary)

    def get_dose_summary(self, person_id: str, period: str, role: str,
                         version: Optional[int] = None) -> Dict[str, Any]:
        self._view(role)
        person_id = require_text(person_id, "person_id", 100)
        period = validate_period(period)
        return self._summary_view(self.repository.get_dose_summary(person_id, period, version))

    def list_dose_readings(self, person_id: str, period: str, role: str) -> list:
        self._view(role)
        person_id = require_text(person_id, "person_id", 100)
        period = validate_period(period)
        return self.repository.list_dose_readings(person_id, period)

    def _summary_view(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(summary)
        period_row = self.repository.get_dose_period(view["person_id"], view["period"])
        view["period_status"] = period_row["status"] if period_row is not None else "open"
        confirmed = self.repository.confirmed_dose_summary(view["person_id"], view["period"])
        view["confirmed_version"] = confirmed["version"] if confirmed is not None else None
        view["investigation_level_msv"] = MONTHLY_INVESTIGATION_LEVEL_MSV
        view["conclusion"] = dose_conclusion(view["total_msv"])
        view["previous"] = {
            "period": view.pop("prev_period"),
            "total_msv": view.pop("prev_total_msv"),
            "delta_msv": view.pop("delta_msv"),
        }
        return view

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
