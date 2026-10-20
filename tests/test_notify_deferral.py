"""第 1 项回归测试:推送状态延后写入。

bug 场景:候选词在去重循环里就被写了 last_notified_at,之后被 Jev 过滤
丢弃时,它已经被当成"已推送",7 天冷却期内凭新证据也无法再提醒。
修复后:只有进入输出 JSON 的词才写 last_notified_*;被过滤的词只写
last_filtered_*,下一轮出现新证据时仍能推送。
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone(timedelta(hours=8)))
NOW_ISO = NOW.isoformat()


def _entry(**kw):
    e = tw._empty_seen_entry(NOW_ISO)
    e.update(kw)
    return e


def _item(term="foo tool", key="foo tool", reason="first_time", **kw):
    it = {"term": term, "source": "reddit", "heat": 10, "_seen_key": key, "_notify_reason": reason}
    it.update(kw)
    return it


class TestMarkRunResults(unittest.TestCase):
    def test_filtered_term_not_marked_notified(self):
        seen = {"foo tool": _entry()}
        item = _item()
        tw.mark_run_results([], [(item, "jev_product_tech=0.30 < 0.6")], seen, NOW_ISO)
        e = seen["foo tool"]
        self.assertIsNone(e["last_notified_at"])
        self.assertIsNone(e["last_notified_reason"])
        self.assertEqual(e["last_filtered_at"], NOW_ISO)
        self.assertIn("jev_product_tech", e["last_filtered_reason"])

    def test_final_item_marked_notified(self):
        seen = {"foo tool": _entry()}
        item = _item(reason="heat_surge")
        tw.mark_run_results([item], [], seen, NOW_ISO)
        e = seen["foo tool"]
        self.assertEqual(e["last_notified_at"], NOW_ISO)
        self.assertEqual(e["last_notified_reason"], "heat_surge")
        self.assertIsNone(e["last_filtered_at"])

    def test_filtered_term_still_notifiable_next_round(self):
        """被 Jev 过滤的词,下一轮出现时仍能推送(last_notified_at 保持 None)。"""
        seen = {"foo tool": _entry()}
        item = _item()
        tw.mark_run_results([], [(item, "jev_product_tech=0.10 < 0.6")], seen, NOW_ISO)
        entry = seen["foo tool"]
        next_item = {"term": "foo tool", "source": "github_breakout", "heat": 500, "note": ""}
        notify, reason = tw.should_notify(next_item, entry, NOW + timedelta(days=1))
        self.assertTrue(notify)

    def test_filtered_after_previous_notify_keeps_old_notify_time(self):
        """曾经推送过、这轮被过滤:last_notified_at 不被覆盖,filtered 单独记录。"""
        old = (NOW - timedelta(days=10)).isoformat()
        seen = {"foo tool": _entry(last_notified_at=old, last_notified_reason="first_time")}
        item = _item(reason="new_source")
        tw.mark_run_results([], [(item, "monetization_reject: generic")], seen, NOW_ISO)
        e = seen["foo tool"]
        self.assertEqual(e["last_notified_at"], old)
        self.assertEqual(e["last_filtered_reason"], "monetization_reject: generic")


class TestSeenMigrationDefaults(unittest.TestCase):
    def test_v1_entry_migrates_with_new_fields(self):
        old = {"first_seen": "2026-10-10T23:54:58+08:00", "count": 3}
        m = tw.migrate_seen_entry(old)
        self.assertEqual(m["seen_count"], 3)
        self.assertEqual(m["last_notified_reason"], "legacy_migration")
        self.assertIsNone(m["last_filtered_at"])
        self.assertIsNone(m["last_filtered_reason"])
        self.assertIsNone(m["jev"])
        self.assertIsNone(m["jev_scored_at"])
        self.assertIsNone(m["jev_model"])

    def test_v2_entry_missing_new_fields_gets_defaults(self):
        v2 = {
            "first_seen_at": "2026-10-10T00:00:00+08:00",
            "last_seen_at": "2026-10-11T00:00:00+08:00",
            "seen_count": 5,
            "last_notified_at": "2026-10-10T00:00:00+08:00",
        }
        m = tw.migrate_seen_entry(v2)
        self.assertEqual(m["seen_count"], 5)
        self.assertIsNone(m["jev"])
        self.assertIsNone(m["last_filtered_at"])


if __name__ == "__main__":
    unittest.main()
