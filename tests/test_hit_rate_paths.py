"""命中率新定义测试（B2，2026-10-27 用户定）。

新定义：
- 有上线页面：14 天内 GSC 展现 >= 20 算命中；无 GSC 数据 → pending。
- 无页面：热度 7 天增长率 >= 2.0 倍算命中；历史不足 7 天 → pending。
- 按来源和路线分别统计（huggingface、api_platform 全部计入）；
  pending 不计入命中率分母；旧 Trends 翻倍标准已废弃。
运行：python3 -m unittest tests.test_hit_rate_paths -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw

NOW = datetime.now(timezone.utc)
OLD = (NOW - timedelta(days=20)).isoformat()
D20 = (NOW - timedelta(days=20)).strftime("%Y-%m-%d")
D13 = (NOW - timedelta(days=13)).strftime("%Y-%m-%d")
D1 = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")


def heat_rec(term, source, path, heats):
    """heats: [(date_str, heat)]，按顺序构成 heat_history。"""
    return {
        "term": term, "discovered_at": OLD, "source": source,
        "initial_heat": heats[0][1],
        "heat_history": [{"date": d, "ts": f"{d}T05:00:00+08:00", "heat": h} for d, h in heats],
        "source_url": "", "monetization_path": path,
        "checked": False, "hit": None,
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

    def test_records_heat_none_when_heat_null(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "word_tracking.json")
            cands = [{"term": "api_model", "source": "api_platform", "heat": None,
                      "source_url": "https://fal.ai/models/x", "monetization_path": "quick_paid_site"}]
            tw.record_discovered_terms(cands, p, OLD)
            data = json.load(open(p, encoding="utf-8"))
            self.assertIsNone(data[0]["initial_heat"])
            # B1 起新点带 ts 字段
            self.assertEqual(data[0]["heat_history"],
                             [{"date": OLD[:10], "ts": OLD, "heat": None}])


class TestHitRateNewDefinition(unittest.TestCase):
    def setUp(self):
        # 隔离 page 匹配：默认无页面，走热度分支
        self._orig_find = tw._hit_page_match
        tw._hit_page_match = lambda term: None

    def tearDown(self):
        tw._hit_page_match = self._orig_find

    def _run(self, data):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        tp = os.path.join(d.name, "word_tracking.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        report = tw.check_hit_rate(tracking_path=tp, output_dir=d.name)
        return report, json.load(open(tp, encoding="utf-8"))

    def test_heat_branch_hit_and_miss(self):
        data = [
            heat_rec("hot1", "huggingface", "content", [(D20, 100), (D1, 250)]),   # 2.5 倍 → 命中
            heat_rec("flat1", "api_platform", "content", [(D20, 100), (D1, 150)]),  # 1.5 倍 → 未命中
        ]
        report, _after = self._run(data)
        self.assertEqual(report["overall"]["total_evaluated"], 2)
        self.assertEqual(report["overall"]["total_hits"], 1)
        self.assertEqual(report["overall"]["hit_rate"], "50.0%")
        # 来源分别统计：huggingface、api_platform 都计入
        self.assertEqual(report["sources"]["huggingface"]["hit_rate"], "100.0%")
        self.assertEqual(report["sources"]["api_platform"]["hit_rate"], "0.0%")
        self.assertEqual(report["paths"]["content"]["total"], 2)
        self.assertEqual(report["paths"]["content"]["hits"], 1)

    def test_heat_branch_pending_not_counted(self):
        data = [
            heat_rec("single", "reddit", "content", [(D1, 90)]),                    # 只有 1 点
            heat_rec("short", "reddit", "content", [(D1, 100), (D1, 300)]),         # 跨度不足 7 天
            heat_rec("hot1", "github_breakout", "content", [(D20, 100), (D1, 250)]),
        ]
        report, after = self._run(data)
        self.assertEqual(report["overall"]["total_evaluated"], 1)
        self.assertEqual(report["overall"]["total_hits"], 1)
        self.assertEqual(report["overall"]["pending"], 2)
        self.assertEqual(len(report["pending_details"]), 2)
        # pending 不算未命中：checked 保持 False，下轮重试
        pend = {r["term"]: r for r in after if r["term"] in ("single", "short")}
        self.assertFalse(pend["single"]["checked"])
        self.assertIsNone(pend["single"]["hit"])

    def test_page_branch_gsc_threshold(self):
        # 页面分支：mock GSC 存量快照，25 展现命中、5 展现未命中、无数据 pending
        orig_stored = tw._stored_gsc_impressions_14d
        orig_live = tw._live_gsc_page_daily
        tw._hit_page_match = lambda term: {
            "term": term, "topic_url": f"https://yuliuswiki.com/{term}"}
        impressions = {"hitpage": 25, "misspage": 5}
        tw._stored_gsc_impressions_14d = lambda page, now: impressions.get(page["term"])
        tw._live_gsc_page_daily = lambda url: None
        try:
            data = [
                heat_rec("hitpage", "huggingface", "content", [(D20, 100), (D1, 110)]),
                heat_rec("misspage", "huggingface", "content", [(D20, 100), (D1, 110)]),
                heat_rec("nodata", "huggingface", "content", [(D20, 100), (D1, 110)]),
            ]
            report, _after = self._run(data)
        finally:
            tw._stored_gsc_impressions_14d = orig_stored
            tw._live_gsc_page_daily = orig_live
        self.assertEqual(report["overall"]["total_evaluated"], 2)
        self.assertEqual(report["overall"]["total_hits"], 1)
        self.assertEqual(report["overall"]["pending"], 1)

    def test_legacy_identification_and_exclusion(self):
        # 1. 验证 is_legacy_tracking_entry 识别逻辑
        self.assertTrue(tw.is_legacy_tracking_entry({"source": "google_trends", "term": "nfl plus"}))
        self.assertTrue(tw.is_legacy_tracking_entry({"source": "huggingface", "term": "image 2.1"}))
        self.assertTrue(tw.is_legacy_tracking_entry({"source": "huggingface", "term": "2.1 comfyui"}))
        self.assertTrue(tw.is_legacy_tracking_entry({"source": "huggingface", "term": "qwen image"}))
        self.assertFalse(tw.is_legacy_tracking_entry({"source": "github_breakout", "term": "pdfcraft"}))
        self.assertFalse(tw.is_legacy_tracking_entry({"source": "huggingface", "term": "Wan2.5-T2V"}))

        # 2. 验证 check_hit_rate 默认排除 legacy 条目，且不删除原始数据
        data = [
            heat_rec("active1", "github_breakout", "tool_page", [(D20, 100), (D1, 250)]),
            heat_rec("nfl plus", "google_trends", None, [(D20, 100), (D1, 250)]),
            heat_rec("image 2.1", "huggingface", None, [(D20, 100), (D1, 250)]),
        ]
        report, after = self._run(data)
        # 活跃追踪数和评估数仅计算 active1，排除 2 个 legacy
        self.assertEqual(report["overall"]["total_tracked"], 1)
        self.assertEqual(report["overall"]["total_evaluated"], 1)
        self.assertEqual(report["overall"]["total_hits"], 1)
        self.assertEqual(report["overall"]["hit_rate"], "100.0%")

        # 原始数据未删除，且被标记 legacy=True
        self.assertEqual(len(after), 3)
        self.assertTrue(next(r for r in after if r["term"] == "nfl plus")["legacy"])
        self.assertTrue(next(r for r in after if r["term"] == "image 2.1")["legacy"])
        self.assertFalse(next(r for r in after if r["term"] == "active1").get("legacy", False))


if __name__ == "__main__":
    unittest.main()


class TestHitPageMatch(unittest.TestCase):
    """命中率严格页面匹配：通用 token（image/video）不能造成跨主题误归因。"""
    PAGES = [
        {"term": "Moss Image Pricing", "topic_url": "https://yuliuswiki.com/moss-image-pricing"},
        {"term": "AI video generation cost calculator", "topic_url": "https://www.yuliusbox.com/ai-video-cost-calculator"},
        {"term": "FLUX 3 Max vs Seedance 2.5 vs MiniMax H3", "topic_url": "https://yuliuswiki.com/flux-3-max-vs-seedance-minimax-h3"},
        {"term": "Kromoss", "topic_url": "https://yuliuswiki.com/kromoss-guide"},
        {"term": "Ideogram Vector Guide", "topic_url": "https://yuliuswiki.com/ideogram-vector-guide"},
    ]

    def test_no_false_positive_generic_token(self):
        # 'image' 是通用词，不能把 Klein 归因到 Moss 定价页
        self.assertIsNone(tw._hit_page_match_in("flux-3-image-klein-comfy", self.PAGES))
        # 'video' 是通用词，不能把视频模型归因到计算器页
        self.assertIsNone(tw._hit_page_match_in("FLUX 3 Max Text to Video", self.PAGES))
        self.assertIsNone(tw._hit_page_match_in("Eden Live 5B Video", self.PAGES))

    def test_true_matches(self):
        self.assertEqual(
            tw._hit_page_match_in("Moss Image", self.PAGES)["topic_url"],
            "https://yuliuswiki.com/moss-image-pricing")
        self.assertEqual(
            tw._hit_page_match_in("Ideogram Vector", self.PAGES)["topic_url"],
            "https://yuliuswiki.com/ideogram-vector-guide")
        self.assertEqual(
            tw._hit_page_match_in("Kromoss", self.PAGES)["topic_url"],
            "https://yuliuswiki.com/kromoss-guide")
        # 'minimax' 是 distinctive token（7 字符、非通用），可匹配同厂商对比文
        self.assertEqual(
            tw._hit_page_match_in("MiniMax-M3.1-Flash", self.PAGES)["topic_url"],
            "https://yuliuswiki.com/flux-3-max-vs-seedance-minimax-h3")
