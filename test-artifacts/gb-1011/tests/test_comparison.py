"""两次已完成试验的缺陷去向对比：服务层单元测试。

运行：cd backend && python -m unittest discover -s ../test-artifacts/gb-1011/tests -v
"""
import copy
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

from fastapi import HTTPException  # noqa: E402

from src.services.business import analyze, seed_records  # noqa: E402
from src.services import comparison  # noqa: E402


class Dataset:
    def __init__(self, records, dataset_id=1, name="南环上行"):
        self.id = dataset_id
        self.name = name
        self.checksum = "checksum"
        self.records = records


def job(job_id, result, params=None, dataset_id=1, status="completed", name=None):
    return SimpleNamespace(id=job_id, name=name or f"试验{job_id}", dataset_id=dataset_id, result=result,
                           params=params or {}, status=status, approved=False, finished_at=1.0)


def legacy(result):
    """模拟本功能上线前保存的结果：没有 points 逐点明细。"""
    old = copy.deepcopy(result)
    old.pop("points", None)
    return old


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = seed_records()
        cls.wide = analyze(cls.records, {"eps_m": 100, "min_points": 5})
        cls.tight = analyze(cls.records, {"eps_m": 30, "min_points": 5})
        cls.coarse = analyze(cls.records, {"eps_m": 400, "min_points": 5})

    def compare(self, first, second, records=None, **kwargs):
        first_job = kwargs.get("first_job") or job(1, first, kwargs.get("first_params"))
        second_job = kwargs.get("second_job") or job(2, second, kwargs.get("second_params"),
                                                     kwargs.get("second_dataset", 1))
        return comparison.compare(first_job, second_job,
                                  Dataset(records if records is not None else self.records,
                                          kwargs.get("dataset_id", 1)))

    def test_per_point_totals_reconcile(self):
        result = self.compare(self.wide, self.tight)
        self.assertEqual(result["basis"], "per_point")
        total = result["totals"]["dataset_total"]
        self.assertEqual(total, 185)
        self.assertEqual(sum(result["counts"].values()), total)
        self.assertEqual(result["totals"]["matched"], total)
        self.assertTrue(all(check["ok"] for check in result["checks"]))
        self.assertEqual(len(result["defects"]), total)
        self.assertEqual(len({row["defect_id"] for row in result["defects"]}), total)

    def test_every_defect_has_one_fate(self):
        result = self.compare(self.wide, self.tight)
        buckets = result["counts"]
        self.assertGreater(buckets["split_points"], 0, "收紧半径应观察到分裂")
        self.assertGreater(buckets["band_to_noise_points"], 0)
        self.assertEqual(buckets["merged_points"], 0)
        for row in result["defects"]:
            source_noise = row["first_type"] == "noise"
            target_noise = row["second_type"] == "noise"
            if source_noise and target_noise:
                self.assertEqual(row["change"], "未变")
            elif source_noise:
                self.assertEqual(row["change"], "孤立点并入缺陷带")
            elif target_noise:
                self.assertEqual(row["change"], "缺陷带转为孤立点")
            else:
                self.assertIn(row["change"], {"随带分裂", "随带合并", "未变", "带未变（成员增减）", "带未变（部分点转孤立）"})

    def test_split_is_reported_per_band(self):
        result = self.compare(self.wide, self.tight)
        splits = [group for group in result["first_groups"] if group["kind"] == "split"]
        self.assertTrue(splits)
        for group in splits:
            band_targets = [target for target in group["targets"] if target["type"] == "band"]
            self.assertGreaterEqual(len(band_targets), 2)
            self.assertEqual(sum(target["count"] for target in group["targets"]), group["count"])
        self.assertTrue(any("分裂" in finding for finding in result["findings"]))

    def test_merge_is_detected(self):
        result = self.compare(self.tight, self.wide)
        self.assertEqual(result["basis"], "per_point")
        self.assertGreater(result["counts"]["merged_points"], 0)
        self.assertTrue(any("合并" in finding for finding in result["findings"]))
        self.assertEqual(sum(result["counts"].values()), 185)

    def test_noise_becomes_band(self):
        result = self.compare(self.tight, self.coarse)
        self.assertGreater(result["counts"]["noise_to_band_points"], 0)
        self.assertEqual(sum(result["counts"].values()), 185)

    def test_identical_trials_are_unchanged(self):
        result = self.compare(self.wide, self.wide)
        self.assertEqual(result["counts"]["unchanged_points"], 185)
        self.assertTrue(all(group["kind"] == "unchanged" for group in result["first_groups"]))
        self.assertEqual(result["params_changed"], [])

    def test_legacy_result_reconstructed_from_band_ranges(self):
        result = self.compare(legacy(self.wide), self.tight)
        self.assertEqual(result["basis"], "per_point")
        self.assertEqual(result["detail_source"], {"first": "reconstructed", "second": "recorded"})
        self.assertEqual(sum(result["counts"].values()), 185)
        self.assertEqual(result["counts"]["split_points"], self.compare(self.wide, self.tight)["counts"]["split_points"])

    def test_both_legacy_results(self):
        result = self.compare(legacy(self.wide), legacy(self.tight))
        self.assertEqual(result["basis"], "per_point")
        self.assertEqual(result["detail_source"], {"first": "reconstructed", "second": "reconstructed"})
        total = result["totals"]["dataset_total"]
        self.assertEqual(sum(result["counts"].values()), total)

    def test_legacy_mismatching_counts_falls_back_to_summary(self):
        tampered = legacy(self.wide)
        tampered["table"]["rows"][0]["count"] += 1
        result = self.compare(tampered, legacy(self.tight))
        self.assertEqual(result["basis"], "band_summary")
        self.assertEqual(result["defects"], [])
        self.assertIsNone(result["totals"]["dataset_total"])
        text = "".join(result["warnings"] + result["findings"])
        self.assertTrue("降级" in text or "推断" in text)
        self.assertEqual(result["totals"]["first_total"], 186)

    def test_legacy_without_table_is_rejected(self):
        broken = legacy(self.wide)
        broken.pop("table")
        with self.assertRaises(HTTPException) as caught:
            self.compare(broken, self.wide)
        self.assertEqual(caught.exception.status_code, 409)

    def test_stored_results_are_not_mutated(self):
        first, second = copy.deepcopy(self.wide), copy.deepcopy(self.tight)
        self.compare(first, second)
        self.assertEqual(first["points"], self.wide["points"])
        self.assertEqual(second["table"], self.tight["table"])
        self.assertNotIn("first_groups", self.wide)

    def test_rejects_incomplete_trials(self):
        for status in ("queued", "running", "failed"):
            with self.assertRaises(HTTPException) as caught:
                self.compare(self.wide, self.tight,
                             second_job=job(2, self.tight, status=status))
            self.assertEqual(caught.exception.status_code, 409)
        for status in ("queued", "running", "failed"):
            with self.assertRaises(HTTPException) as caught:
                self.compare(self.wide, self.tight,
                             first_job=job(1, self.wide, status=status))
            self.assertEqual(caught.exception.status_code, 409)

    def test_rejects_same_trial(self):
        with self.assertRaises(HTTPException) as caught:
            comparison.compare(job(1, self.wide), job(1, self.wide), Dataset(self.records))
        self.assertEqual(caught.exception.status_code, 422)

    def test_rejects_cross_dataset(self):
        with self.assertRaises(HTTPException) as caught:
            self.compare(self.wide, self.tight, second_dataset=2)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("跨批次", caught.exception.detail)

    def test_rejects_missing_defect_id(self):
        records = copy.deepcopy(self.records)
        records[7]["defect_id"] = ""
        with self.assertRaises(HTTPException) as caught:
            self.compare(self.wide, self.tight, records=records)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("缺少缺陷编号", caught.exception.detail)

    def test_rejects_duplicate_defect_id(self):
        records = copy.deepcopy(self.records)
        records[3]["defect_id"] = records[4]["defect_id"]
        with self.assertRaises(HTTPException) as caught:
            self.compare(legacy(self.wide), legacy(self.tight), records=records)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("重复", caught.exception.detail)

    def test_rejects_point_detail_not_matching_dataset(self):
        broken = copy.deepcopy(self.wide)
        broken["points"][0]["defect_id"] = "NOT-IN-BATCH"
        with self.assertRaises(HTTPException) as caught:
            self.compare(broken, self.tight)
        self.assertEqual(caught.exception.status_code, 409)

    def test_rejects_unknown_job_path(self):
        with self.assertRaises(HTTPException) as caught:
            comparison.compare(job(1, self.wide), job(2, self.tight), None)
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
