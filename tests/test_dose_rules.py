import unittest
from src import rules
from src.domain import ValidationError


class DoseRulesTest(unittest.TestCase):
    def test_period_normalize_and_previous(self):
        self.assertEqual(rules.normalize_period("2026-03"), "2026-03")
        self.assertEqual(rules.previous_period("2026-01"), "2025-12")
        self.assertEqual(rules.previous_period("2026-03"), "2026-02")
        with self.assertRaises(ValidationError):
            rules.normalize_period("2026-3")
        with self.assertRaises(ValidationError):
            rules.normalize_period("2026-13")

    def test_aggregate_by_source_ignores_superseded(self):
        readings = [
            {"id": 1, "source": "TLD", "value": 0.5, "superseded_by": 3},
            {"id": 2, "source": "EPD", "value": 0.8, "superseded_by": None},
            {"id": 3, "source": "TLD", "value": 0.7, "superseded_by": None},
        ]
        sources = rules.aggregate_by_source(readings)
        self.assertEqual({s["source"]: s["value"] for s in sources},
                         {"EPD": 0.8, "TLD": 0.7})
        self.assertEqual(rules.total_from_sources(sources), 1.5)

    def test_investigation_decision(self):
        required, reasons = rules.investigation_decision(2.1, 0.5)
        self.assertTrue(required)
        self.assertTrue(any("调查水平" in r for r in reasons))
        required, reasons = rules.investigation_decision(1.5, 0.4)
        self.assertTrue(required)
        self.assertTrue(any("3倍" in r for r in reasons))
        required, _ = rules.investigation_decision(0.3, 0.2)
        self.assertFalse(required)
        required, _ = rules.investigation_decision(0.3, None)
        self.assertFalse(required)


if __name__ == "__main__":
    unittest.main()
