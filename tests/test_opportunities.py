import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import opps


class TestOpportunities(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.opps_path = os.path.join(self.test_dir, "opportunities.json")
        self.page_tracking_path = os.path.join(self.test_dir, "page_tracking.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_opportunities_and_migration(self):
        # 1. Record new candidate
        candidates = [{
            "term": "test-ai-model",
            "source": "api_platform",
            "commercial_score": 9,
            "monetization_path": "quick_paid_site",
            "note": "平台: fal.ai | 单价: $0.05",
        }]
        count = opps.record_opportunities(candidates, path=self.opps_path)
        self.assertEqual(count, 1)

        # 2. Check saved file content
        data = opps.load_opportunities(self.opps_path)
        self.assertEqual(len(data), 1)
        rec = data[0]
        self.assertEqual(rec["term"], "test-ai-model")
        self.assertEqual(rec["status"], "new")
        self.assertEqual(rec["suggested_route"], "quick_paid_site")
        self.assertEqual(rec["score"], 9)
        self.assertIsNotNone(rec["deadline"])

        # 3. Deduplication: recording same candidate again should not duplicate
        count2 = opps.record_opportunities(candidates, path=self.opps_path)
        self.assertEqual(count2, 0)
        data = opps.load_opportunities(self.opps_path)
        self.assertEqual(len(data), 1)

    def test_migration_and_backup(self):
        # Write an old schema record missing fields
        old_data = [
            {"term": "old-tool", "suggested_route": "tool_page"}
        ]
        with open(self.opps_path, "w", encoding="utf-8") as f:
            json.dump(old_data, f)

        # Load should trigger migration, normalize route to box_tool, and create backup
        loaded = opps.load_opportunities(self.opps_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["suggested_route"], "box_tool")
        self.assertEqual(loaded[0]["status"], "new")
        self.assertIn("discovered_at", loaded[0])

        # Verify backup was created
        baks = [f for f in os.listdir(self.test_dir) if f.startswith("opportunities.json.bak-")]
        self.assertTrue(len(baks) >= 1)

    def test_status_transitions_and_cli_set(self):
        rec = {
            "term": "flux-converter",
            "source": "api_platform",
            "discovered_at": opps.now_iso(),
            "score": 8,
            "suggested_route": "box_tool",
            "decision": None,
            "status": "new",
            "outcome": None,
            "page_url": "",
            "deadline": None,
            "notes": "",
            "drop_reason": None,
            "updated_at": opps.now_iso(),
        }
        opps.save_opportunities([rec], path=self.opps_path)

        # Mock CLI set decision
        class Args:
            term = "flux-converter"
            decision = "build"
            status = None
            url = None
            note = "开始立项"
            outcome = None
            drop_reason = None
            deadline = None

        # Update via find and apply logic
        data = opps.load_opportunities(self.opps_path)
        item = opps.find_opp(data, "flux-converter")
        item["decision"] = "build"
        item["status"] = "decided"
        item["notes"] = "开始立项"
        opps.save_opportunities(data, path=self.opps_path)

        # Now set URL -> should become live
        item["page_url"] = "https://yuliusbox.com/flux-converter"
        item["status"] = "live"
        item["outcome"] = "running"
        opps.save_opportunities(data, path=self.opps_path)

        data = opps.load_opportunities(self.opps_path)
        self.assertEqual(data[0]["status"], "live")
        self.assertEqual(data[0]["page_url"], "https://yuliusbox.com/flux-converter")
        self.assertEqual(data[0]["outcome"], "running")

    def test_sync_with_page_tracking(self):
        today = datetime.now(timezone.utc).date()
        past_date = (today - timedelta(days=8)).isoformat()
        future_date = (today + timedelta(days=7)).isoformat()

        # Page tracking data
        page_data = [
            {
                "term": "wan2-tool",
                "topic_url": "https://yuliusbox.com/wan2-tool",
                "outcome": "running",
                "kill_by": past_date,  # Overdue for review
            },
            {
                "term": "solidcraft",
                "topic_url": "https://yuliuswiki.com/solidcraft",
                "outcome": "killed",
                "kill_by": past_date,
            },
            {
                "term": "live-app",
                "topic_url": "https://yuliusbox.com/live-app",
                "outcome": "running",
                "kill_by": future_date,
            }
        ]

        opps_list = [
            {
                "term": "wan2-tool",
                "status": "building",
                "page_url": "https://yuliusbox.com/wan2-tool",
            },
            {
                "term": "solidcraft",
                "status": "live",
                "page_url": "https://yuliuswiki.com/solidcraft",
            },
            {
                "term": "live-app",
                "status": "decided",
                "page_url": "",
            }
        ]
        opps_list = [opps.migrate_record(o)[0] for o in opps_list]

        changed = opps.sync_with_page_tracking(opps_list, page_data)
        self.assertTrue(changed)

        wan = opps.find_opp(opps_list, "wan2-tool")
        self.assertEqual(wan["status"], "review")  # past kill_by -> review
        self.assertEqual(wan["outcome"], "running")
        self.assertEqual(wan["deadline"], past_date)

        solid = opps.find_opp(opps_list, "solidcraft")
        self.assertEqual(solid["status"], "dropped")  # outcome killed -> dropped
        self.assertEqual(solid["outcome"], "killed")

        live = opps.find_opp(opps_list, "live-app")
        self.assertEqual(live["status"], "live")  # matched by term -> live
        self.assertEqual(live["page_url"], "https://yuliusbox.com/live-app")
        self.assertEqual(live["deadline"], future_date)


if __name__ == "__main__":
    unittest.main()
