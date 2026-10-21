#!/usr/bin/env python3
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from last_run_utils import (
    BJ_TZ,
    OutageAlertManager,
    evaluate_outage,
    load_last_run,
    record_last_run,
)


class TestOutageWatch(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.dash_dir = os.path.join(self.test_dir, "dashboard", "data")
        self.out_dir = os.path.join(self.test_dir, "outputs")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_last_run_full_and_fast(self):
        # 1. 首次全量运行记录
        now_dt = datetime(2026, 10, 21, 12, 0, 0, tzinfo=BJ_TZ)
        res1 = record_last_run(
            "full",
            dashboard_data_dir=self.dash_dir,
            output_dir=self.out_dir,
            now_iso=now_dt.isoformat(),
        )
        self.assertEqual(res1["full_at"], now_dt.isoformat())
        self.assertEqual(res1["status"], "ok")
        self.assertTrue(os.path.exists(os.path.join(self.dash_dir, "last_run.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out_dir, "last_run.json")))

        # 2. 随后 fast 运行记录
        fast_dt = datetime(2026, 10, 21, 12, 30, 0, tzinfo=BJ_TZ)
        res2 = record_last_run(
            "fast",
            dashboard_data_dir=self.dash_dir,
            output_dir=self.out_dir,
            now_iso=fast_dt.isoformat(),
        )
        self.assertEqual(res2["full_at"], now_dt.isoformat())  # full_at 保持不变
        self.assertEqual(res2["fast_at"], fast_dt.isoformat())  # fast_at 更新

        loaded = load_last_run(os.path.join(self.dash_dir, "last_run.json"))
        self.assertEqual(loaded["full_at"], now_dt.isoformat())
        self.assertEqual(loaded["fast_at"], fast_dt.isoformat())

    def test_evaluate_outage(self):
        base_time = datetime(2026, 10, 21, 12, 0, 0, tzinfo=BJ_TZ)

        # 正常状态 (full 5h 前, fast 1h 前)
        normal_data = {
            "full_at": (base_time - timedelta(hours=5)).isoformat(),
            "fast_at": (base_time - timedelta(hours=1)).isoformat(),
        }
        res = evaluate_outage(normal_data, now_dt=base_time)
        self.assertFalse(res["full_outage"])
        self.assertFalse(res["fast_outage"])
        self.assertFalse(res["is_outage"])

        # 全量超时 (full 8.5h 前)
        full_overdue_data = {
            "full_at": (base_time - timedelta(hours=8.5)).isoformat(),
            "fast_at": (base_time - timedelta(hours=1)).isoformat(),
        }
        res = evaluate_outage(full_overdue_data, now_dt=base_time)
        self.assertTrue(res["full_outage"])
        self.assertFalse(res["fast_outage"])
        self.assertTrue(res["is_outage"])

        # fast 超时 (fast 2.5h 前)
        fast_overdue_data = {
            "full_at": (base_time - timedelta(hours=2)).isoformat(),
            "fast_at": (base_time - timedelta(hours=2.5)).isoformat(),
        }
        res = evaluate_outage(fast_overdue_data, now_dt=base_time)
        self.assertFalse(res["full_outage"])
        self.assertTrue(res["fast_outage"])
        self.assertTrue(res["is_outage"])

        # 缺失字段
        empty_res = evaluate_outage({}, now_dt=base_time)
        self.assertTrue(empty_res["full_outage"])
        self.assertTrue(empty_res["fast_outage"])
        self.assertTrue(empty_res["is_outage"])

    def test_outage_alert_manager_dedup_and_recovery(self):
        manager = OutageAlertManager(repeat_suppression_hours=6.0)
        t0 = datetime(2026, 10, 21, 12, 0, 0, tzinfo=BJ_TZ)

        # 1. 正常状态 -> 无报警
        normal_data = {
            "full_at": (t0 - timedelta(hours=2)).isoformat(),
            "fast_at": (t0 - timedelta(minutes=30)).isoformat(),
        }
        notes = manager.check(normal_data, now_dt=t0)
        self.assertEqual(len(notes), 0)

        # 2. 全量任务断档 (超时 8.5 小时) -> 发送首次报警
        t1 = t0 + timedelta(hours=7)  # 此时 full_at 距今 2 + 7 = 9 小时
        overdue_full_at = normal_data["full_at"]
        notes = manager.check({
            "full_at": overdue_full_at,
            "fast_at": (t1 - timedelta(minutes=30)).isoformat(),
        }, now_dt=t1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["type"], "alert")
        self.assertEqual(notes[0]["target"], "full")
        self.assertIn("断档报警", notes[0]["text"])
        self.assertIn("全量任务", notes[0]["text"])

        # 3. 2 小时后 (仍未运行，但在 6 小时抑制期内，fast 保持活跃) -> 去重抑制，不发报警
        t2 = t1 + timedelta(hours=2)
        notes = manager.check({
            "full_at": overdue_full_at,
            "fast_at": (t2 - timedelta(minutes=30)).isoformat(),
        }, now_dt=t2)
        self.assertEqual(len(notes), 0)

        # 4. 5.9 小时后 -> 仍在 6 小时内，继续抑制
        t3 = t1 + timedelta(hours=5.9)
        notes = manager.check({
            "full_at": overdue_full_at,
            "fast_at": (t3 - timedelta(minutes=30)).isoformat(),
        }, now_dt=t3)
        self.assertEqual(len(notes), 0)

        # 5. 6.1 小时后 -> 超过 6 小时抑制期，重新报警
        t4 = t1 + timedelta(hours=6.1)
        notes = manager.check({
            "full_at": overdue_full_at,
            "fast_at": (t4 - timedelta(minutes=30)).isoformat(),
        }, now_dt=t4)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["type"], "alert")
        self.assertEqual(notes[0]["target"], "full")

        # 6. 全量任务恢复正常 (full_at 更新为当前时间) -> 发送恢复通知
        t5 = t4 + timedelta(minutes=10)
        recovered_data = {
            "full_at": t5.isoformat(),
            "fast_at": t5.isoformat(),
        }
        notes = manager.check(recovered_data, now_dt=t5)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["type"], "recovery")
        self.assertEqual(notes[0]["target"], "full")
        self.assertIn("已恢复", notes[0]["text"])

        # 7. 再次检测正常状态 -> 无多余通知
        t6 = t5 + timedelta(minutes=30)
        notes = manager.check(recovered_data, now_dt=t6)
        self.assertEqual(len(notes), 0)

    def test_fast_outage_and_recovery(self):
        manager = OutageAlertManager(repeat_suppression_hours=6.0)
        t0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=BJ_TZ)

        # Fast 超过 2.5 小时 -> 报警
        overdue_fast = {
            "full_at": t0.isoformat(),
            "fast_at": (t0 - timedelta(hours=2.5)).isoformat(),
        }
        notes = manager.check(overdue_fast, now_dt=t0)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["type"], "alert")
        self.assertEqual(notes[0]["target"], "fast")
        self.assertIn("Fast 任务", notes[0]["text"])

        # 恢复
        recovered_fast = {
            "full_at": t0.isoformat(),
            "fast_at": t0.isoformat(),
        }
        notes = manager.check(recovered_fast, now_dt=t0)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["type"], "recovery")
        self.assertEqual(notes[0]["target"], "fast")


if __name__ == "__main__":
    unittest.main()
