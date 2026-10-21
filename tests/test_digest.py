import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from digest import generate_digest, run_digest


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.opps_path = os.path.join(self.test_dir, "opportunities.json")
        self.page_tracking_path = os.path.join(self.test_dir, "page_tracking.json")
        self.hit_rate_path = os.path.join(self.test_dir, "hit_rate.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_empty_silent_exit(self):
        # Empty opportunities and empty page_tracking
        with open(self.opps_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        with open(self.page_tracking_path, "w", encoding="utf-8") as f:
            json.dump([], f)

        res = generate_digest(
            opps_path=self.opps_path,
            page_tracking_path=self.page_tracking_path,
            hit_rate_path=self.hit_rate_path,
        )
        self.assertEqual(res, "")

        # Also test with opps that are already decided/dropped (pending=0, overdue=0, no yesterday)
        now_utc = datetime.now(timezone.utc)
        old_dt = (now_utc - timedelta(days=10)).isoformat()
        resolved_opps = [
            {
                "term": "old-tool",
                "status": "decided",
                "decision": "build",
                "discovered_at": old_dt,
            },
            {
                "term": "dropped-model",
                "status": "dropped",
                "decision": "drop",
                "discovered_at": old_dt,
            }
        ]
        with open(self.opps_path, "w", encoding="utf-8") as f:
            json.dump(resolved_opps, f)

        res2 = generate_digest(
            opps_path=self.opps_path,
            page_tracking_path=self.page_tracking_path,
            hit_rate_path=self.hit_rate_path,
        )
        self.assertEqual(res2, "")

    def test_populated_digest(self):
        bj_tz = timezone(timedelta(hours=8))
        fixed_now = datetime(2026, 10, 21, 9, 0, 0, tzinfo=bj_tz)
        yesterday_str = (fixed_now - timedelta(days=1)).strftime("%Y-%m-%dT15:00:00+08:00")
        overdue_str = (fixed_now - timedelta(days=3)).strftime("%Y-%m-%dT10:00:00+08:00")

        opps_data = [
            {
                "term": "flux-3-new",
                "source": "api_platform",
                "suggested_route": "quick_paid_site",
                "score": 95,
                "status": "new",
                "decision": None,
                "discovered_at": yesterday_str,
            },
            {
                "term": "old-unhandled-opp",
                "source": "github_breakout",
                "suggested_route": "box_tool",
                "score": 50,
                "status": "evaluating",
                "decision": None,
                "discovered_at": overdue_str,
            }
        ]
        with open(self.opps_path, "w", encoding="utf-8") as f:
            json.dump(opps_data, f)

        # Page tracking data: 1 page kill_by today
        page_data = [
            {
                "term": "wan2-site",
                "topic_url": "https://yuliusbox.com/wan2",
                "kill_by": "2026-10-21",
                "outcome": "running",
                "site_type": "quick_paid_site",
            }
        ]
        with open(self.page_tracking_path, "w", encoding="utf-8") as f:
            json.dump(page_data, f)

        # Hit rate report data
        hr_data = {
            "paths": {
                "quick_paid_site": {"total": 2, "hits": 2, "hit_rate": "100.0%"},
                "tool_page": {"total": 3, "hits": 2, "hit_rate": "66.7%"},
            }
        }
        with open(self.hit_rate_path, "w", encoding="utf-8") as f:
            json.dump(hr_data, f)

        res = generate_digest(
            opps_path=self.opps_path,
            page_tracking_path=self.page_tracking_path,
            hit_rate_path=self.hit_rate_path,
            now_dt=fixed_now,
        )
        self.assertIn("flux-3-new", res)
        self.assertIn("old-unhandled-opp", res)
        self.assertIn("wan2-site", res)
        self.assertIn("quick_paid_site", res)
        self.assertIn("待决定数量: **2** 个", res)
        self.assertIn("超过 48 小时未处理清单 (1 个)", res)


if __name__ == "__main__":
    unittest.main()
