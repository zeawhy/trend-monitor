"""第 5 项测试:page_tracking 收益/止损字段、旧结构迁移、pnl 汇总、到期复核。"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("page_tracking", os.path.join(ROOT, "page_tracking.py"))
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)


def old_record():
    return {
        "term": "solidcraft",
        "topic_url": "https://yuliuswiki.com/solidcraft",
        "decided_at": "2026-10-01T00:00:00+00:00",
        "decision_reason": "x",
        "effort_notes": "",
        "index_status": "deploying",
        "gsc": [],
        "created_at": "2026-10-01T00:00:00+00:00",
    }


class TestMigration(unittest.TestCase):
    def test_old_record_gets_defaults(self):
        m = pt.migrate_record(old_record())
        self.assertEqual(m["site_type"], "content")
        self.assertEqual(m["costs"], [])
        self.assertEqual(m["revenue"], [])
        self.assertEqual(m["kill_by"], "2026-10-08")  # 上线后第 7 天
        self.assertEqual(m["merged_into"], "")
        self.assertEqual(m["outcome"], "running")

    def test_existing_values_preserved(self):
        rec = old_record()
        rec.update({"site_type": "quick_paid_site", "outcome": "kept", "kill_by": "2026-11-01",
                    "costs": [{"item": "a", "amount": 1, "date": "2026-10-01"}]})
        m = pt.migrate_record(rec)
        self.assertEqual(m["site_type"], "quick_paid_site")
        self.assertEqual(m["outcome"], "kept")
        self.assertEqual(m["kill_by"], "2026-11-01")
        self.assertEqual(len(m["costs"]), 1)


class TestNetAsof(unittest.TestCase):
    def _rec(self):
        rec = old_record()
        rec["site_type"] = "quick_paid_site"
        rec["costs"] = [{"item": "域名", "amount": 100, "date": "2026-10-01"}]
        rec["revenue"] = [
            {"date": "2026-10-01", "gross": 50, "api_cost": 5, "fees": 2, "refunds": 0},
            {"date": "2026-10-05", "gross": 200, "api_cost": 20, "fees": 6, "refunds": 10},
        ]
        return pt.migrate_record(rec)

    def test_day1_net(self):
        self.assertAlmostEqual(pt._net_asof(self._rec(), 1), 43 - 100)

    def test_day7_cumulative(self):
        self.assertAlmostEqual(pt._net_asof(self._rec(), 7), (43 + 164) - 100)

    def test_missing_costs_is_unknown(self):
        rec = self._rec()
        rec["costs"] = []
        self.assertIsNone(pt._net_asof(rec, 7))

    def test_no_revenue_in_window_is_unknown(self):
        rec = self._rec()
        rec["revenue"] = [{"date": "2026-11-01", "gross": 10, "api_cost": 0, "fees": 0, "refunds": 0}]
        self.assertIsNone(pt._net_asof(rec, 7))
        self.assertIsNotNone(pt._net_asof(rec, 28 + 10))  # 窗口够大时有数据


class TestSummarizePnl(unittest.TestCase):
    def test_grouped_by_site_type_with_unknown(self):
        rec_ok = old_record()
        rec_ok["site_type"] = "quick_paid_site"
        rec_ok["costs"] = [{"item": "x", "amount": 10, "date": "2026-10-01"}]
        rec_ok["revenue"] = [{"date": "2026-10-02", "gross": 30, "api_cost": 0, "fees": 0, "refunds": 0}]
        rec_empty = old_record()
        rec_empty["term"] = "other"
        rec_empty["site_type"] = "content"
        s = pt.summarize_pnl([pt.migrate_record(rec_ok), pt.migrate_record(rec_empty)])
        self.assertAlmostEqual(s["quick_paid_site"][7], 20)
        self.assertIsNone(s["content"][7])  # 无数据 → 未知,不是 0


class TestCliFlow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = pt.TRACKING_PATH
        pt.TRACKING_PATH = os.path.join(self._tmp.name, "page_tracking.json")

    def tearDown(self):
        pt.TRACKING_PATH = self._old
        self._tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with mock_argv(argv), contextlib.redirect_stdout(buf):
            pt.main()
        return buf.getvalue()

    def test_add_revenue_cost_pnl_list(self):
        out = self._run(["page_tracking.py", "add", "--term", "wan2tool", "--url", "https://x.example", "--site-type", "quick_paid_site"])
        self.assertIn("kill_by", out)
        self._run(["page_tracking.py", "cost", "--term", "wan2tool", "--item", "服务器", "--amount", "50"])
        today = datetime.now(timezone.utc).date().isoformat()
        self._run(["page_tracking.py", "revenue", "--term", "wan2tool", "--date", today,
                     "--gross", "120", "--api-cost", "10", "--fees", "5", "--refunds", "0"])
        out = self._run(["page_tracking.py", "pnl"])
        self.assertIn("quick_paid_site", out)
        self.assertIn("55.00", out)  # 120-10-5-0-50,第 1/7/28 天同值
        data = pt.load()
        self.assertEqual(data[0]["kill_by"], (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat())

    def test_list_kill_review_hint(self):
        rec = pt.migrate_record(old_record())  # kill_by=2026-10-08 已过期,outcome=running
        pt.save([rec])
        out = self._run(["page_tracking.py", "list"])
        self.assertIn("到期复核", out)
        rec["outcome"] = "killed"
        pt.save([rec])
        out = self._run(["page_tracking.py", "list"])
        self.assertNotIn("到期复核", out)


class mock_argv:
    def __init__(self, argv):
        self.argv = argv

    def __enter__(self):
        self._old = sys.argv
        sys.argv = self.argv

    def __exit__(self, *a):
        sys.argv = self._old


if __name__ == "__main__":
    unittest.main()
