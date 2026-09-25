import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import MONTHLY_INVESTIGATION_LEVEL_MSV, previous_period


class DoseAggregationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _reading(self, person, period, source, dose, ref):
        return self.service.submit_reading(
            {"person_id": person, "period": period, "source": source,
             "dose_msv": dose, "external_ref": ref}, "dosi", "dosimetrist")

    def _aggregate(self, person, period):
        return self.service.aggregate_period(
            {"person_id": person, "period": period}, "officer", "radiation_officer")

    def test_aggregate_by_source_and_investigation(self):
        self._reading("P1", "2026-08", "TLD", 1.0, "R1")
        self._reading("P1", "2026-08", "TLD", 0.5, "R2")
        self._reading("P1", "2026-08", "EPD", 1.2, "R3")
        summary = self._aggregate("P1", "2026-08")
        self.assertEqual(summary["breakdown"], {"TLD": 1.5, "EPD": 1.2})
        self.assertEqual(summary["total_msv"], 2.7)
        self.assertEqual(summary["reading_count"], 3)
        self.assertTrue(summary["total_msv"] >= MONTHLY_INVESTIGATION_LEVEL_MSV)
        self.assertTrue(summary["investigation_required"])
        self.assertIn("必须调查", summary["conclusion"])
        self.assertEqual(summary["version"], 1)
        self.assertEqual(summary["status"], "provisional")
        self.assertEqual(summary["period_status"], "open")

    def test_below_level_needs_no_investigation(self):
        self._reading("P1", "2026-08", "TLD", 0.3, "R1")
        summary = self._aggregate("P1", "2026-08")
        self.assertFalse(summary["investigation_required"])
        self.assertIn("无需调查", summary["conclusion"])

    def test_duplicate_submission_rejected(self):
        self._reading("P1", "2026-08", "TLD", 1.0, "R1")
        with self.assertRaises(ConflictError):
            self._reading("P1", "2026-08", "TLD", 2.0, "R1")

    def test_correction_supersedes_but_keeps_original(self):
        old = self._reading("P1", "2026-08", "TLD", 5.0, "R1")
        new = self.service.correct_reading(
            old["id"], {"dose_msv": 0.5, "external_ref": "R1-C",
                        "reason": "监测方退回更正"}, "dosi", "dosimetrist")
        self.assertEqual(new["supersedes_id"], old["id"])
        readings = self.service.list_dose_readings("P1", "2026-08", "viewer")
        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]["status"], "superseded")
        self.assertEqual(readings[0]["dose_msv"], 5.0)
        self.assertEqual(readings[1]["status"], "effective")
        summary = self._aggregate("P1", "2026-08")
        self.assertEqual(summary["total_msv"], 0.5)
        with self.assertRaises(ConflictError):
            self.service.correct_reading(
                old["id"], {"dose_msv": 0.6, "external_ref": "R1-C2"},
                "dosi", "dosimetrist")

    def test_seal_then_recalculate_versions_and_confirmed_frozen(self):
        reading = self._reading("P1", "2026-08", "TLD", 1.0, "R1")
        first = self._aggregate("P1", "2026-08")
        self.assertEqual(first["version"], 1)
        sealed = self.service.seal_period(
            {"person_id": "P1", "period": "2026-08"}, "officer", "radiation_officer")
        self.assertEqual(sealed["status"], "confirmed")
        with self.assertRaises(ConflictError):
            self._reading("P1", "2026-08", "EPD", 0.4, "R2")
        self.service.correct_reading(
            reading["id"], {"dose_msv": 3.0, "external_ref": "R1-C"},
            "dosi", "dosimetrist")
        second = self._aggregate("P1", "2026-08")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["total_msv"], 3.0)
        self.assertEqual(second["status"], "provisional")
        confirmed = self.service.get_dose_summary("P1", "2026-08", "viewer", version=1)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["total_msv"], 1.0)
        latest = self.service.get_dose_summary("P1", "2026-08", "viewer")
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["confirmed_version"], 1)
        self.assertEqual(latest["period_status"], "sealed")
        with self.assertRaises(ConflictError):
            self.service.seal_period(
                {"person_id": "P1", "period": "2026-08"}, "officer", "radiation_officer")

    def test_previous_period_comparison(self):
        self._reading("P1", "2026-07", "TLD", 1.0, "R1")
        self._aggregate("P1", "2026-07")
        self._reading("P1", "2026-08", "TLD", 1.4, "R2")
        summary = self._aggregate("P1", "2026-08")
        self.assertEqual(summary["previous"]["period"], "2026-07")
        self.assertEqual(summary["previous"]["total_msv"], 1.0)
        self.assertAlmostEqual(summary["previous"]["delta_msv"], 0.4)
        self.assertEqual(previous_period("2026-01"), "2025-12")

    def test_seal_requires_summary_and_permission_rules(self):
        self._reading("P1", "2026-08", "TLD", 1.0, "R1")
        with self.assertRaises(ConflictError):
            self.service.seal_period(
                {"person_id": "P1", "period": "2026-09"}, "officer", "radiation_officer")
        with self.assertRaises(PermissionDenied):
            self.service.aggregate_period(
                {"person_id": "P1", "period": "2026-08"}, "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.submit_reading(
                {"person_id": "P1", "period": "2026-08", "source": "TLD",
                 "dose_msv": 1, "external_ref": "R9"}, "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.seal_period(
                {"person_id": "P1", "period": "2026-08"}, "x", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.submit_reading(
                {"person_id": "P1", "period": "2026-13", "source": "TLD",
                 "dose_msv": 1, "external_ref": "R8"}, "x", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.submit_reading(
                {"person_id": "P1", "period": "2026-08", "source": "TLD",
                 "dose_msv": -1, "external_ref": "R7"}, "x", "dosimetrist")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
