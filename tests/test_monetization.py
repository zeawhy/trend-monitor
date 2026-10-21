"""第 4 项测试:classify_monetization 每条规则与优先级。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw


def jev(intent=None, news=None, pt=0.9):
    return {"is_product_tech": pt, "intent": intent, "intent_confidence": 0.8, "newsworthiness": news}


class TestClassifyMonetization(unittest.TestCase):
    def test_rule1_quick_paid_site(self):
        item = {"event_type": "new_model", "api_available": True, "intent": "tool", "jev": jev("paid_tool", 0.9)}
        path, reason = tw.classify_monetization(item)
        self.assertEqual(path, "quick_paid_site")
        self.assertIn("48 小时", reason)
        self.assertIn("积分制付费站", reason)

    def test_rule1_api_platform_media_generation(self):
        # 补测试: api_platform 来源、媒体生成类(FLUX 3 Max、Fibo 2.0 样本)必须落到 quick_paid_site
        for sample in [
            {
                "term": "FLUX 3 Max Text to Video",
                "source": "api_platform",
                "event_type": "new_model",
                "api_available": True,
                "pipeline_tag": "text-to-video",
                "jev": jev("media_generation", 0.49),
            },
            {
                "term": "Fibo 2.0 Generate",
                "source": "api_platform",
                "event_type": "new_model",
                "api_available": True,
                "pipeline_tag": "text-to-image",
                "jev": jev("media_generation", 0.34),
            },
            {
                "term": "Some Audio Model",
                "source": "api_platform",
                "event_type": None,
                "api_available": True,
                "pipeline_tag": "text-to-speech",
                "jev": jev("media_generation", 0.5),
            },
        ]:
            path, reason = tw.classify_monetization(sample)
            self.assertEqual(path, "quick_paid_site", f"Failed for {sample['term']}")
            self.assertIn("48 小时", reason)

    def test_rule1_requires_api_available(self):
        for api in (False, None):
            item = {"event_type": "new_model", "api_available": api, "intent": "other", "jev": jev(None, None)}
            path, _ = tw.classify_monetization(item)
            self.assertEqual(path, "watch", api)

    def test_rule2_tool_intent(self):
        path, _ = tw.classify_monetization({"intent": "tool"})
        self.assertEqual(path, "tool_page")

    def test_rule2_jev_paid_tool_and_media_generation(self):
        for ji in ("paid_tool", "media_generation"):
            path, _ = tw.classify_monetization({"intent": "other", "jev": jev(ji, 0.5)})
            self.assertEqual(path, "tool_page", ji)

    def test_rule2_beats_rule3(self):
        # 本地 intent=info 但 Jev 说 paid_tool:按顺序先命中规则 2
        path, _ = tw.classify_monetization({"intent": "info", "jev": jev("paid_tool", 0.5)})
        self.assertEqual(path, "tool_page")

    def test_rule3_content(self):
        self.assertEqual(tw.classify_monetization({"intent": "info"})[0], "content")
        self.assertEqual(tw.classify_monetization({"intent": "other", "jev": jev("tech_term", 0.5)})[0], "content")

    def test_rule4_reject_generic_low_news(self):
        path, reason = tw.classify_monetization({"intent": "other", "jev": jev("generic", 0.39)})
        self.assertEqual(path, "reject")
        self.assertIn("generic", reason)

    def test_rule4_not_reject_when_news_ok_or_missing(self):
        self.assertEqual(tw.classify_monetization({"intent": "other", "jev": jev("generic", 0.4)})[0], "watch")
        self.assertEqual(tw.classify_monetization({"intent": "other", "jev": jev("generic", None)})[0], "watch")
        self.assertEqual(tw.classify_monetization({"intent": "other", "jev": jev("generic", 0.9)})[0], "watch")

    def test_rule5_watch_default(self):
        self.assertEqual(tw.classify_monetization({})[0], "watch")
        self.assertEqual(tw.classify_monetization({"intent": "other"})[0], "watch")

    def test_path_priority_order(self):
        p = tw.MONETIZATION_PATH_PRIORITY
        self.assertLess(p["quick_paid_site"], p["tool_page"])
        self.assertLess(p["tool_page"], p["content"])
        self.assertLess(p["content"], p["watch"])

    def test_sort_key_integration(self):
        items = [
            {"term": "w", "monetization_path": "watch", "event_type": "product_launch", "commercial_score": 10, "heat": 999},
            {"term": "q", "monetization_path": "quick_paid_site", "event_type": None, "commercial_score": 0, "heat": 1},
            {"term": "c", "monetization_path": "content", "event_type": None, "commercial_score": 5, "heat": 5},
            {"term": "t", "monetization_path": "tool_page", "event_type": None, "commercial_score": 5, "heat": 5},
        ]
        items.sort(key=lambda it: (tw.MONETIZATION_PATH_PRIORITY.get(it.get("monetization_path"), 3), tw.get_sort_key(it)))
        self.assertEqual([i["term"] for i in items], ["q", "t", "c", "w"])

    def test_heat_null_sorts_at_end_of_same_route(self):
        # 验证: 拿不到真实热度(heat=None)时，排在同一路线的最后，不参与和其他来源的比较
        items = [
            {"term": "q_null", "monetization_path": "quick_paid_site", "event_type": "new_model", "commercial_score": 8, "heat": None, "heat_source": "none"},
            {"term": "q_real", "monetization_path": "quick_paid_site", "event_type": "new_model", "commercial_score": 8, "heat": 10, "heat_source": "run_count"},
            {"term": "t_real", "monetization_path": "tool_page", "event_type": None, "commercial_score": 5, "heat": 50},
        ]
        items.sort(key=lambda it: (tw.MONETIZATION_PATH_PRIORITY.get(it.get("monetization_path"), 3), tw.get_sort_key(it)))
        # q_real (有热度) 优先于 q_null (无热度)，但都在 quick_paid_site，先于 tool_page
        self.assertEqual([i["term"] for i in items], ["q_real", "q_null", "t_real"])


if __name__ == "__main__":
    unittest.main()
