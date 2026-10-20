"""第 2 项测试:Jev 稳定化(通道切换/解析/上下文/名额分配/缓存/健康监控)。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

GOOD_JEV = {"is_product_tech": 0.9, "intent": "paid_tool", "intent_confidence": 0.8, "newsworthiness": 0.7}


class TestParseJevAnswers(unittest.TestCase):
    def test_normal_shape(self):
        data = {"answers": {
            "is_product_tech": {"type": "noul", "noul": 0.96},
            "intent": {"type": "choice", "choice": "paid_tool", "confidence": 1},
            "newsworthiness": {"type": "score", "score": 0.63},
        }}
        r = tw._parse_jev_answers(data)
        self.assertEqual(r["is_product_tech"], 0.96)
        self.assertEqual(r["intent"], "paid_tool")
        self.assertEqual(r["intent_confidence"], 1.0)
        self.assertEqual(r["newsworthiness"], 0.63)

    def test_none_and_empty(self):
        self.assertIsNone(tw._parse_jev_answers(None))
        self.assertIsNone(tw._parse_jev_answers({}))
        self.assertIsNone(tw._parse_jev_answers({"answers": {}}))
        self.assertIsNone(tw._parse_jev_answers({"answers": "junk"}))

    def test_missing_fields_become_none(self):
        r = tw._parse_jev_answers({"answers": {"intent": {"choice": "generic"}}})
        self.assertIsNone(r["is_product_tech"])
        self.assertEqual(r["intent"], "generic")
        self.assertIsNone(r["intent_confidence"])
        self.assertIsNone(r["newsworthiness"])

    def test_score_with_legend_normalized(self):
        # Decisions API 形状: score 是原始刻度 2.38, legend 0..3 -> 0.793
        data = {"answers": {"newsworthiness": {
            "type": "score", "score": 2.38,
            "legend": {"0": "Low", "1": "Medium", "2": "High", "3": "Exceptional"},
        }}}
        r = tw._parse_jev_answers(data)
        self.assertAlmostEqual(r["newsworthiness"], 2.38 / 3, places=3)

    def test_score_above_one_without_legend_uses_default_scale(self):
        r = tw._parse_jev_answers({"answers": {"newsworthiness": {"score": 1.5}}})
        self.assertAlmostEqual(r["newsworthiness"], 0.5, places=3)


class TestProviderChain(unittest.TestCase):
    def test_provider_none(self):
        with mock.patch.object(tw, "JEV_PROVIDER", "none"):
            self.assertEqual(tw.score_with_jev_state("anything"), (None, None))

    def test_openrouter_no_key_falls_back(self):
        sentinel = (dict(GOOD_JEV), "jev-1.13-free")
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with mock.patch.object(tw, "JEV_PROVIDER", "openrouter"), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(tw, "_jev_via_opencode", return_value=sentinel) as oc:
            r = tw.score_with_jev_state("state text")
            self.assertEqual(r, sentinel)
            oc.assert_called_once()

    def test_openrouter_with_key_uses_openrouter_model(self):
        with mock.patch.object(tw, "JEV_PROVIDER", "openrouter"), \
             mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test-dummy"}), \
             mock.patch.object(tw, "_jev_post", return_value=dict(GOOD_JEV)) as post:
            r, model = tw.score_with_jev_state("state text")
            self.assertEqual(r, GOOD_JEV)
            self.assertEqual(model, "typesafe/jev-1.14")
            args = post.call_args
            self.assertEqual(args.args[0], "https://openrouter.ai/api/alpha/decisions")
            self.assertEqual(args.args[2], "sk-test-dummy")

    def test_openrouter_failure_falls_back(self):
        sentinel = (dict(GOOD_JEV), "jev-1.13-free")
        with mock.patch.object(tw, "JEV_PROVIDER", "openrouter"), \
             mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test-dummy"}), \
             mock.patch.object(tw, "_jev_post", return_value=None), \
             mock.patch.object(tw, "_jev_via_opencode", return_value=sentinel):
            self.assertEqual(tw.score_with_jev_state("x"), sentinel)

    def test_timeout_returns_none_gracefully(self):
        import urllib.request
        with mock.patch.object(tw, "JEV_PROVIDER", "opencode_free"), \
             mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            self.assertEqual(tw.score_with_jev_state("x"), (None, None))


class TestBuildJevState(unittest.TestCase):
    def test_includes_context_fields(self):
        item = {
            "term": "wan2.5", "source": "huggingface",
            "description": "a video model", "pipeline_tag": "text-to-video",
            "license": "apache-2.0", "note": "HF note", "source_url": "https://huggingface.co/x",
        }
        s = tw.build_jev_state(item)
        for frag in ["wan2.5", "huggingface", "a video model", "text-to-video", "apache-2.0", "HF note"]:
            self.assertIn(frag, s)

    def test_truncates_to_1500(self):
        item = {"term": "x", "note": "n" * 5000}
        self.assertLessEqual(len(tw.build_jev_state(item)), 1500)


class TestSelectForScoring(unittest.TestCase):
    def test_per_source_minimum(self):
        items = []
        for src in ("github_breakout", "huggingface", "reddit"):
            for i in range(6):
                items.append({"term": f"{src}-{i}", "source": src, "commercial_score": 10 - i, "heat": 100 - i})
        chosen = tw._select_for_scoring(list(items), 10)
        self.assertEqual(len(chosen), 10)
        from collections import Counter
        counts = Counter(c["source"] for c in chosen)
        for src in ("github_breakout", "huggingface", "reddit"):
            self.assertGreaterEqual(counts[src], 3)

    def test_under_limit_returns_all(self):
        items = [{"term": "a", "source": "reddit"}]
        self.assertEqual(tw._select_for_scoring(items, 15), items)


class TestScoreOneCacheAndRobustness(unittest.TestCase):
    def _stats(self):
        return {"provider": "openrouter", "attempted": 0, "succeeded": 0, "failed": 0, "cached": 0}

    def test_cache_reuse_on_cooldown(self):
        entry = tw._empty_seen_entry(NOW.isoformat())
        entry["jev"] = dict(GOOD_JEV)
        entry["jev_scored_at"] = (NOW - timedelta(days=1)).isoformat()
        entry["jev_model"] = "jev-1.13-free"
        seen = {"foo": entry}
        item = {"term": "foo", "_seen_key": "foo", "_notify_reason": "cooldown"}
        stats = self._stats()
        with mock.patch.object(tw.time, "sleep", lambda *a: None):
            tw._score_one_with_jev(item, seen, NOW, stats)
        self.assertEqual(item["jev_status"], "cached")
        self.assertEqual(stats["cached"], 1)
        self.assertEqual(stats["attempted"], 0)

    def test_new_evidence_rescores_and_updates_cache(self):
        entry = tw._empty_seen_entry(NOW.isoformat())
        entry["jev"] = dict(GOOD_JEV)
        entry["jev_scored_at"] = (NOW - timedelta(days=1)).isoformat()
        seen = {"foo": entry}
        item = {"term": "foo", "_seen_key": "foo", "_notify_reason": "heat_surge"}
        stats = self._stats()
        fresh = {"is_product_tech": 0.5, "intent": "tech_term", "intent_confidence": 0.4, "newsworthiness": 0.3}
        with mock.patch.object(tw.time, "sleep", lambda *a: None), \
             mock.patch.object(tw, "score_with_jev_state", return_value=(fresh, "jev-1.13-free")):
            tw._score_one_with_jev(item, seen, NOW, stats)
        self.assertEqual(item["jev_status"], "scored")
        self.assertEqual(stats["attempted"], 1)
        self.assertEqual(entry["jev"], fresh)
        self.assertEqual(entry["jev_model"], "jev-1.13-free")

    def test_none_newsworthiness_does_not_crash(self):
        item = {"term": "foo", "_seen_key": "foo", "_notify_reason": "first_time"}
        stats = self._stats()
        jev_none = {"is_product_tech": None, "intent": None, "intent_confidence": None, "newsworthiness": None}
        with mock.patch.object(tw.time, "sleep", lambda *a: None), \
             mock.patch.object(tw, "score_with_jev_state", return_value=(jev_none, "jev-1.13-free")):
            tw._score_one_with_jev(item, {}, NOW, stats)  # 不抛异常即通过
        self.assertEqual(item["jev_status"], "scored")

    def test_channel_failure_marks_unscored(self):
        item = {"term": "foo", "_seen_key": "foo", "_notify_reason": "first_time"}
        stats = self._stats()
        with mock.patch.object(tw.time, "sleep", lambda *a: None), \
             mock.patch.object(tw, "score_with_jev_state", return_value=(None, None)):
            tw._score_one_with_jev(item, {}, NOW, stats)
        self.assertEqual(item["jev_status"], "unscored")
        self.assertEqual(stats["failed"], 1)


class TestJevHealth(unittest.TestCase):
    def test_two_zero_rounds_warn(self):
        with tempfile.TemporaryDirectory() as d:
            s0 = {"provider": "openrouter", "attempted": 5, "succeeded": 0, "failed": 5, "cached": 0}
            self.assertIsNone(tw.update_jev_health(s0, NOW.isoformat(), d))
            w = tw.update_jev_health(s0, NOW.isoformat(), d)
            self.assertIsNotNone(w)
            self.assertIn("Jev 打分失效", w)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            s0 = {"provider": "openrouter", "attempted": 5, "succeeded": 0, "failed": 5, "cached": 0}
            tw.update_jev_health(s0, NOW.isoformat(), d, dry_run=True)
            self.assertFalse(os.path.exists(os.path.join(d, "jev_health.json")))


if __name__ == "__main__":
    unittest.main()
