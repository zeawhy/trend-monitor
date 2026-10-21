#!/usr/bin/env python3
import os
import unittest
from unittest.mock import MagicMock, patch

import notifier


class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        os.environ["TELEGRAM_BOT_TOKEN"] = "fake_test_token"
        os.environ["TELEGRAM_CHAT_ID"] = "12345678"
        os.environ["TELEGRAM_ENABLED"] = "true"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_disabled_env(self):
        os.environ["TELEGRAM_ENABLED"] = "false"
        with patch("urllib.request.urlopen") as mock_open:
            ret = notifier.send_telegram_message("hello")
            self.assertFalse(ret)
            mock_open.assert_not_called()

    def test_missing_token(self):
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        with patch("urllib.request.urlopen") as mock_open:
            ret = notifier.send_telegram_message("hello")
            self.assertFalse(ret)
            mock_open.assert_not_called()

    def test_missing_chat_id(self):
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        with patch("urllib.request.urlopen") as mock_open:
            ret = notifier.send_telegram_message("hello")
            self.assertFalse(ret)
            mock_open.assert_not_called()

    def test_empty_message(self):
        with patch("urllib.request.urlopen") as mock_open:
            self.assertFalse(notifier.send_telegram_message(""))
            self.assertFalse(notifier.send_telegram_message("   "))
            mock_open.assert_not_called()

    def test_send_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            ret = notifier.send_telegram_message("<b>test</b>", parse_mode="HTML")
            self.assertTrue(ret)
            self.assertEqual(mock_open.call_count, 1)
            req = mock_open.call_args[0][0]
            self.assertTrue(req.full_url.endswith("/sendMessage"))
            self.assertEqual(req.headers.get("Content-type"), "application/json")

    def test_send_network_failure_does_not_raise(self):
        with patch("urllib.request.urlopen", side_effect=Exception("network timeout")):
            # Must return False and not raise
            ret = notifier.send_telegram_message("test message")
            self.assertFalse(ret)

    def test_chunking(self):
        long_text = "A" * 9000
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            ret = notifier.send_telegram_message(long_text)
            self.assertTrue(ret)
            self.assertEqual(mock_open.call_count, 3)

    def test_notify_new_terms_empty(self):
        with patch("notifier.send_telegram_message") as mock_send:
            ret = notifier.notify_new_terms([])
            self.assertFalse(ret)
            mock_send.assert_not_called()

    def test_notify_new_terms_fast(self):
        items = [
            {
                "term": "test-model-1",
                "platform": "fal.ai",
                "unit_price": "$0.05/megapixel",
                "source_url": "https://fal.ai/models/test-1",
            }
        ]
        with patch("notifier.send_telegram_message", return_value=True) as mock_send:
            ret = notifier.notify_new_terms(items, is_fast=True)
            self.assertTrue(ret)
            msg = mock_send.call_args[0][0]
            self.assertIn("[Fast官方新模型]", msg)
            self.assertIn("test-model-1", msg)
            self.assertIn("fal.ai", msg)
            self.assertIn("$0.05/megapixel", msg)

    def test_notify_new_terms_full(self):
        items = [
            {
                "term": "quick-tool",
                "monetization_path": "quick_paid_site",
                "unit_price": "$0.02",
                "hours_since_release": 12.5,
                "source_url": "https://example.com/tool",
            },
            {
                "term": "watch-tool",
                "monetization_path": "watch",
                "source": "github_breakout",
                "heat": 500,
            },
        ]
        with patch("notifier.send_telegram_message", return_value=True) as mock_send:
            ret = notifier.notify_new_terms(items, is_fast=False)
            self.assertTrue(ret)
            msg = mock_send.call_args[0][0]
            self.assertIn("quick_paid_site", msg)
            self.assertIn("quick-tool", msg)
            self.assertIn("watch-tool", msg)

    def test_notify_digest(self):
        with patch("notifier.send_telegram_message", return_value=True) as mock_send:
            self.assertFalse(notifier.notify_digest(""))
            mock_send.assert_not_called()

            self.assertTrue(notifier.notify_digest("# Daily Digest\nItem 1"))
            self.assertEqual(mock_send.call_count, 1)

    def test_notify_alert(self):
        with patch("notifier.send_telegram_message", return_value=True) as mock_send:
            self.assertFalse(notifier.notify_alert("Title", ""))
            mock_send.assert_not_called()

            self.assertTrue(notifier.notify_alert("Jev Failure", "Consecutive errors"))
            msg = mock_send.call_args[0][0]
            self.assertIn("Jev Failure", msg)
            self.assertIn("Consecutive errors", msg)


if __name__ == "__main__":
    unittest.main()
