"""追踪历史回归测试（B1）。

覆盖：同日多轮都记点（不再按天去重）、新点带 ts、
create_new=False 不新建条目只追加热度点、7 天变化率计算。
运行：python3 -m unittest tests.test_tracking_history -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trend_watch import heat_7d_change, record_discovered_terms


class TestTrackingHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "word_tracking.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_same_day_multiple_rounds_append(self):
        record_discovered_terms(
            [{"term": "flux-klein", "source": "api_platform", "heat": 90}],
            self.path, "2026-10-27T05:08:00+08:00")
        record_discovered_terms(
            [{"term": "flux-klein", "source": "api_platform", "heat": 95}],
            self.path, "2026-10-27T11:08:00+08:00")
        data = self._load()
        self.assertEqual(len(data), 1)
        hist = data[0]["heat_history"]
        # 旧逻辑按天去重会只剩 1 点；新逻辑同日两轮记 2 点
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["heat"], 90)
        self.assertEqual(hist[1]["heat"], 95)
        self.assertEqual(hist[1]["ts"], "2026-10-27T11:08:00+08:00")

    def test_filtered_items_append_only_no_create(self):
        record_discovered_terms(
            [{"term": "tracked-term", "source": "huggingface", "heat": 100}],
            self.path, "2026-10-27T05:08:00+08:00")
        # 过滤项：已追踪词追加点，未追踪词不新建
        record_discovered_terms(
            [{"term": "tracked-term", "source": "huggingface", "heat": 120},
             {"term": "never-tracked", "source": "huggingface", "heat": 50}],
            self.path, "2026-10-27T11:08:00+08:00", create_new=False)
        data = self._load()
        self.assertEqual(len(data), 1)
        self.assertEqual(len(data[0]["heat_history"]), 2)

    def test_heat_7d_change(self):
        item = {"heat_history": [
            {"date": "2026-10-20", "ts": "2026-10-20T05:00:00+08:00", "heat": 100},
            {"date": "2026-10-24", "ts": "2026-10-24T05:00:00+08:00", "heat": 150},
            {"date": "2026-10-27", "ts": "2026-10-27T05:00:00+08:00", "heat": 300},
        ]}
        rate, ref, latest, ref_label = heat_7d_change(item)
        # 最新 10-27，7 天前目标 10-20，ref 取 10-20 的 100
        self.assertEqual(ref, 100)
        self.assertEqual(latest, 300)
        self.assertEqual(ref_label, "2026-10-20")
        self.assertAlmostEqual(rate, 2.0)

    def test_heat_7d_change_single_point(self):
        rate, _ref, latest, _label = heat_7d_change(
            {"heat_history": [{"date": "2026-10-27", "ts": "2026-10-27T05:00:00+08:00", "heat": 90}]})
        self.assertIsNone(rate)
        self.assertEqual(latest, 90)


if __name__ == "__main__":
    unittest.main()
