import tempfile
import unittest
from pathlib import Path

from src.repository import Repository
from src.service import Service


class DoseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.p = self.service.register_person(
            {"code": "P-001", "name": "张三"}, "clerk", "dosimetrist")
        self.pid = self.p["id"]

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit(self, period, source, ref, value):
        return self.service.submit_reading(
            {"person_id": self.pid, "period": period, "source": source,
             "external_ref": ref, "value": value}, "clerk", "dosimetrist")

    def test_aggregate_correction_seal_and_immutability(self):
        # 同一人员同一周期、多来源归集
        self._submit("2026-07", "TLD", "TLD-7", 0.30)
        self._submit("2026-07", "EPD", "EPD-7", 0.10)
        july = self.service.collect_month(
            {"person_id": self.pid, "period": "2026-07"}, "clerk", "dosimetrist")
        self.assertAlmostEqual(july["total_msv"], 0.40)
        self.assertEqual(july["latest_status"], "draft")
        self.assertFalse(july["investigation_required"])
        self.service.seal_period({"period": "2026-07"}, "officer", "radiation_officer")
        july_view = self.service.get_month_view(self.pid, "2026-07", "viewer")
        self.assertEqual(july_view["latest_status"], "confirmed")
        self.assertTrue(july_view["snapshot"])

        # 8月数据：更正取代旧值，原记录保留
        self._submit("2026-08", "TLD", "TLD-8", 0.50)
        self._submit("2026-08", "EPD", "EPD-8", 0.20)
        tld_id = self.service.list_readings(
            self.pid, "2026-08", "viewer")[0]["id"]
        corrected = self.service.correct_reading(
            tld_id, {"value": 0.60, "reason": "实验室复测"}, "clerk", "dosimetrist")
        self.assertEqual(corrected["value"], 0.60)
        readings = self.service.list_readings(self.pid, "2026-08", "viewer")
        self.assertEqual(len(readings), 3)
        old = next(r for r in readings if r["status"] == "superseded")
        self.assertEqual(old["superseded_by"], corrected["id"])
        aug = self.service.collect_month(
            {"person_id": self.pid, "period": "2026-08"}, "clerk", "dosimetrist")
        self.assertAlmostEqual(aug["total_msv"], 0.80)  # 0.60 + 0.20，旧值0.50不参与
        self.assertEqual(aug["previous_period"]["period"], "2026-07")
        self.assertAlmostEqual(aug["previous_period"]["total_msv"], 0.40)
        self.assertAlmostEqual(aug["previous_period"]["delta_msv"], 0.40)
        self.assertFalse(aug["investigation_required"])

        # 封存8月：已确认结果固定
        self.service.seal_period({"period": "2026-08"}, "officer", "radiation_officer")
        sealed_aug = self.service.get_month_view(self.pid, "2026-08", "viewer")
        self.assertTrue(sealed_aug["snapshot"])
        self.assertEqual(sealed_aug["latest_version"], 1)

        # 封存后更正 -> 再归集必须生成新版本v2，v1不跟着变
        new_reading = self.service.correct_reading(
            corrected["id"], {"value": 0.90, "reason": "退件复核"},
            "clerk", "dosimetrist")
        del new_reading
        v2 = self.service.collect_month(
            {"person_id": self.pid, "period": "2026-08"}, "officer",
            "radiation_officer")
        self.assertEqual(v2["latest_version"], 2)
        self.assertEqual(v2["latest_status"], "draft")
        self.assertAlmostEqual(v2["total_msv"], 1.10)
        v1 = next(s for s in self.repo.list_dose_summaries("2026-08")
                  if s["version"] == 1)
        self.assertEqual(v1["status"], "confirmed")
        self.assertAlmostEqual(v1["total_msv"], 0.80)

        # 调查结论：9月大剂量达到调查水平，且以上一周期已确认值为对比基准
        self._submit("2026-09", "TLD", "TLD-9", 2.50)
        sep = self.service.collect_month(
            {"person_id": self.pid, "period": "2026-09"}, "clerk", "dosimetrist")
        self.assertTrue(sep["investigation_required"])
        self.assertTrue(sep["investigation_reasons"])
        self.assertEqual(sep["previous_period"]["period"], "2026-08")
        self.assertAlmostEqual(sep["previous_period"]["total_msv"], 0.80)
        self.assertEqual(sep["previous_period"]["status"], "confirmed")

        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
