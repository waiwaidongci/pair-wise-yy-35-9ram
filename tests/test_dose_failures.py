import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class DoseFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.p = self.service.register_person(
            {"code": "P-002", "name": "李四"}, "clerk", "dosimetrist")
        self.pid = self.p["id"]

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit(self, period="2026-05", source="TLD", ref="X-1", value=0.2):
        return self.service.submit_reading(
            {"person_id": self.pid, "period": period, "source": source,
             "external_ref": ref, "value": value}, "clerk", "dosimetrist")

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_person(
                {"code": "X", "name": "Y"}, "s", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.submit_reading(
                {"person_id": self.pid, "period": "2026-05", "source": "TLD",
                 "external_ref": "a", "value": 1}, "s", "radiation_officer")
        self._submit()
        with self.assertRaises(PermissionDenied):
            self.service.seal_period({"period": "2026-05"}, "s", "dosimetrist")

    def test_duplicate_submission_rejected(self):
        self._submit()
        with self.assertRaises(ConflictError):
            self._submit()

    def test_sealed_period_blocks_submit_and_double_seal(self):
        self._submit()
        self.service.seal_period({"period": "2026-05"}, "o", "radiation_officer")
        with self.assertRaises(ConflictError):
            self._submit(ref="X-2")
        with self.assertRaises(ConflictError):
            self.service.seal_period({"period": "2026-05"}, "o", "radiation_officer")

    def test_double_correction_and_double_confirm(self):
        reading = self._submit()
        new = self.service.correct_reading(
            reading["id"], {"value": 0.3, "reason": "r"}, "c", "dosimetrist")
        with self.assertRaises(ConflictError):
            self.service.correct_reading(
                reading["id"], {"value": 0.4, "reason": "r"}, "c", "dosimetrist")
        self.service.collect_month(
            {"person_id": self.pid, "period": "2026-05"}, "c", "dosimetrist")
        self.service.confirm_summary(
            {"person_id": self.pid, "period": "2026-05"}, "o", "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.confirm_summary(
                {"person_id": self.pid, "period": "2026-05"},
                "o", "radiation_officer")
        del new

    def test_validation_and_not_found(self):
        with self.assertRaises(ValidationError):
            self.service.submit_reading(
                {"person_id": self.pid, "period": "2026/05", "source": "TLD",
                 "external_ref": "a", "value": 1}, "s", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.submit_reading(
                {"person_id": self.pid, "period": "2026-05", "source": "TLD",
                 "external_ref": "a", "value": -1}, "s", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.get_month_view(self.pid, "bad", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.get_month_view(self.pid, "2026-05", "stranger")


if __name__ == "__main__":
    unittest.main()
