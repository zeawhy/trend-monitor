import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

import trend_watch as tw
import opps

NOW_UTC = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)


class TestFastAndOfficialPlatforms(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.output_dir = os.path.join(self.test_dir, "outputs")
        os.makedirs(self.output_dir, exist_ok=True)
        self.seen_path = os.path.join(self.output_dir, "seen_terms.json")
        self.opps_path = os.path.join(self.output_dir, "opportunities.json")
        with open(self.opps_path, "w", encoding="utf-8") as f:
            json.dump([], f)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_fetch_openrouter_new_models(self):
        cutoff = NOW_UTC - timedelta(days=7)
        mock_data = {
            "data": [
                {
                    "id": "org/fast-model-free",
                    "name": "Fast Free Model",
                    "created": int((NOW_UTC - timedelta(days=2)).timestamp()),
                    "description": "Fast free text/vision model",
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"modality": "text+image->text"},
                },
                {
                    "id": "org/fast-model-paid",
                    "name": "Fast Paid Model",
                    "created": int((NOW_UTC - timedelta(days=3)).timestamp()),
                    "description": "Paid model",
                    "pricing": {"prompt": "0.0000005", "completion": "0.000002"},
                    "architecture": {"modality": "text->text"},
                },
                {
                    "id": "org/old-model",
                    "name": "Old Model",
                    "created": int((NOW_UTC - timedelta(days=15)).timestamp()),
                    "description": "Old model",
                }
            ]
        }
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_data

        with mock.patch("requests.get", return_value=mock_resp):
            res = tw._fetch_openrouter_new_models(cutoff, limit=5)

        self.assertEqual(len(res), 2)
        # Free model
        self.assertEqual(res[0]["term"], "Fast Free Model")
        self.assertEqual(res[0]["unit_price"], "免费")
        self.assertEqual(res[0]["platform"], "openrouter")
        self.assertEqual(res[0]["source_url"], "https://openrouter.ai/org/fast-model-free")
        # Paid model
        self.assertEqual(res[1]["term"], "Fast Paid Model")
        self.assertIn("$0.50/$2.00 per 1M tokens", res[1]["unit_price"])

    def test_fetch_bfl_new_models(self):
        cutoff = NOW_UTC - timedelta(days=7)
        html_content = """
        <html><body>
        <a aria-label="Read blog post: FLUX 3 Image Klein" href="/blog/flux-3-image-klein">
          <div>October 18, 2026</div>
        </a>
        <a aria-label="Read blog post: Old FLUX Model" href="/blog/old-flux">
          <div>September 1, 2026</div>
        </a>
        </body></html>
        """
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.text = html_content

        with mock.patch("requests.get", return_value=mock_resp):
            res = tw._fetch_bfl_new_models(cutoff, limit=5)

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["term"], "FLUX 3 Image Klein")
        self.assertEqual(res[0]["platform"], "bfl")
        self.assertEqual(res[0]["source_url"], "https://bfl.ai/blog/flux-3-image-klein")

    def test_run_fast_dedup_and_stdout(self):
        # Prepare mock models
        mock_models = [
            {
                "term": "Mock Fal Model",
                "source": "api_platform",
                "heat": 80,
                "intent": "tool",
                "commercial_score": 8,
                "event_type": "new_model",
                "note": "平台: fal.ai | 单价: $0.05/sec",
                "source_url": "https://fal.ai/models/org/mock-fal",
                "platform": "fal.ai",
                "unit_price": "$0.05/sec",
            },
            {
                "term": "Mock OpenRouter Model",
                "source": "api_platform",
                "heat": 85,
                "intent": "tool",
                "commercial_score": 8,
                "event_type": "new_model",
                "note": "平台: OpenRouter | 单价: 免费",
                "source_url": "https://openrouter.ai/models/org/mock-or",
                "platform": "openrouter",
                "unit_price": "免费",
            }
        ]

        with mock.patch("trend_watch._fetch_fal_new_models", return_value=[mock_models[0]]), \
             mock.patch("trend_watch._fetch_replicate_new_models", return_value=[]), \
             mock.patch("trend_watch._fetch_openrouter_new_models", return_value=[mock_models[1]]), \
             mock.patch("trend_watch._fetch_bfl_new_models", return_value=[]), \
             mock.patch("opps.OPPS_PATH", self.opps_path):

            # 1. First run: discovers 2 models
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                discovered = tw.run_fast(dry_run=False, limit=5, output_dir=self.output_dir)

            out_text = buf.getvalue()
            self.assertEqual(len(discovered), 2)
            self.assertIn("Mock Fal Model", out_text)
            self.assertIn("Mock OpenRouter Model", out_text)
            self.assertIn("单价: $0.05/sec", out_text)
            self.assertIn("单价: 免费", out_text)

            # Check seen_terms.json was saved
            self.assertTrue(os.path.exists(self.seen_path))
            with open(self.seen_path, encoding="utf-8") as f:
                seen = json.load(f)
            self.assertIn("mock fal model", seen)
            self.assertIn("mock openrouter model", seen)

            # Check opportunities.json was written with status=new
            self.assertTrue(os.path.exists(self.opps_path))
            with open(self.opps_path, encoding="utf-8") as f:
                opps_data = json.load(f)
            self.assertEqual(len(opps_data), 2)
            self.assertEqual(opps_data[0]["status"], "new")

            # 2. Second run: both models are in seen_terms -> should return [] and print nothing
            buf2 = io.StringIO()
            with mock.patch("sys.stdout", buf2):
                discovered2 = tw.run_fast(dry_run=False, limit=5, output_dir=self.output_dir)

            self.assertEqual(len(discovered2), 0)
            self.assertEqual(buf2.getvalue(), "")

    def test_run_fast_dry_run_does_not_persist(self):
        if os.path.exists(self.opps_path):
            os.remove(self.opps_path)
        mock_models = [
            {
                "term": "Dry Run Model",
                "source": "api_platform",
                "heat": 80,
                "intent": "tool",
                "commercial_score": 8,
                "event_type": "new_model",
                "note": "平台: fal.ai | 单价: $0.02",
                "source_url": "https://fal.ai/models/org/dry-run-model",
                "platform": "fal.ai",
                "unit_price": "$0.02",
            }
        ]

        with mock.patch("trend_watch._fetch_fal_new_models", return_value=mock_models), \
             mock.patch("trend_watch._fetch_replicate_new_models", return_value=[]), \
             mock.patch("trend_watch._fetch_openrouter_new_models", return_value=[]), \
             mock.patch("trend_watch._fetch_bfl_new_models", return_value=[]), \
             mock.patch("opps.OPPS_PATH", self.opps_path):

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                discovered = tw.run_fast(dry_run=True, limit=5, output_dir=self.output_dir)

            self.assertEqual(len(discovered), 1)
            self.assertIn("[dry-run]", buf.getvalue())
            # Neither seen_terms.json nor opportunities.json should be written
            self.assertFalse(os.path.exists(self.seen_path))
            self.assertFalse(os.path.exists(self.opps_path))


if __name__ == "__main__":
    unittest.main()
