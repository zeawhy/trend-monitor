"""第 6 项测试:命中率按变现路线分组 + word_tracking 记录 monetization_path。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw

OLD = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()


def rec(term, source, path, checked, hit):
    return {
        "term": term, "discovered_at": OLD, "source": source,
        "initial_heat": 10, "heat_history": [{"date": OLD[:10], "heat": 10}],
        "source_url": "", "monetization_path": path,
        "checked": checked, "hit": hit,
        "heat_at_discovery": 5, "heat_after_14d": 20, "check_time": OLD,
    }


class TestRecordDiscoveredTerms(unittest.TestCase):
    def test_records_monetization_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "word_tracking.json")
            cands = [{"term": "foo", "source": "huggingface", "heat": 5,
                      "source_url": "https://huggingface.co/x", "monetization_path": "quick_paid_site"}]
            tw.record_discovered_terms(cands, p, OLD)
            data = json.load(open(p, encoding="utf-8"))
            self.assertEqual(data[0]["monetization_path"], "quick_paid_site")


class TestHitRateByPath(unittest.TestCase):
    def test_grouping_and_quick_paid_standard(self):
        with tempfile.TemporaryDirectory() as d:
            tp = os.path.join(d, "word_tracking.json")
            data = [
                rec("c1", "reddit", "content", True, True),
                rec("c2", "reddit", "content", True, False),
                rec("t1", "github_breakout", "tool_page", True, True),
                rec("q1", "huggingface", "quick_paid_site", False, None),  # 不应被 Trends 核验
                rec("l1", "reddit", None, True, True),  # 旧记录 → legacy
            ]
            with open(tp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            report = tw.check_hit_rate(tracking_path=tp, output_dir=d)
            paths = report["paths"]
            self.assertEqual(paths["content"]["total"], 2)
            self.assertEqual(paths["content"]["hits"], 1)
            self.assertEqual(paths["tool_page"]["hit_rate"], "100.0%")
            self.assertEqual(paths["legacy"]["total"], 1)
            q = paths["quick_paid_site"]
            self.assertEqual(q["tracked"], 1)
            self.assertEqual(q["total"], 0)
            self.assertIsNone(q["hit_rate"])
            self.assertEqual(q["standard"], "以 page_tracking 第 7 天净收入为准")
            # quick_paid_site 记录没有被 Trends 标记 checked
            after = json.load(open(tp, encoding="utf-8"))
            q1 = [r for r in after if r["term"] == "q1"][0]
            self.assertFalse(q1["checked"])
            # MD 报告包含路线表和判定标准标注
            md = open(os.path.join(d, "hit_rate_report.md"), encoding="utf-8").read()
            self.assertIn("各变现路线命中率统计", md)
            self.assertIn("以 page_tracking 第 7 天净收入为准", md)


if __name__ == "__main__":
    unittest.main()
