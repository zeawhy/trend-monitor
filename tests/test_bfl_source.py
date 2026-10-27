"""BFL 官方来源回归测试（B4）。

回归场景：FLUX 3 Klein 2026-10-15 官方发布——若抓取器当天运行，
应该在当天发现它（公告日 = 发现日），且与 fal.ai 的 Klein 端点族
归组为同一机会（发现时间取最早的那个来源）。
注意：生产 cutoff 为近 7 天，旧帖不会被回填；本测试验证“当天运行
就能当天发现”的解析与归组行为，不模拟事后补抓。
运行：python3 -m unittest tests.test_bfl_source -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trend_config import model_family_key
from trend_watch import parse_bfl_blog_posts
from opps import record_opportunities

FIXTURE_HTML = """
<html><body>
<a aria-label="Read blog post: Introducing FLUX.3 Klein" href="/blog/flux-3-klein">
<span>Models</span><span>FLUX.3 Klein is our most efficient image model yet.</span>
<span>Oct 15, 2026</span></a>
<a aria-label="Read blog post: FLUX.3 Max" href="/blog/flux-3-max">
<span>Models</span><span>FLUX.3 Max raises the bar.</span>
<span>September 30, 2026</span></a>
<a aria-label="Read blog post: Research Update" href="/blog/research-update">
<span>Research</span><span>Notes from the lab.</span></a>
</body></html>
"""


class TestBflParsing(unittest.TestCase):
    def test_klein_announcement_abbreviated_month(self):
        posts = parse_bfl_blog_posts(FIXTURE_HTML)
        klein = next(p for p in posts if p["slug"] == "flux-3-klein")
        # 旧正则只认全称月份，"Oct 15, 2026" 会解析失败导致无日期；
        # 新解析必须认出缩写月份，且标题清洗为模型名
        self.assertEqual(klein["title"], "FLUX 3 Klein")
        self.assertEqual(klein["published_at"].isoformat(), "2026-10-15")

    def test_full_month_and_undated(self):
        posts = parse_bfl_blog_posts(FIXTURE_HTML)
        max_post = next(p for p in posts if p["slug"] == "flux-3-max")
        self.assertEqual(max_post["published_at"].isoformat(), "2026-09-30")
        undated = next(p for p in posts if p["slug"] == "research-update")
        self.assertIsNone(undated["published_at"])


class TestCrossSourceAssociation(unittest.TestCase):
    def test_bfl_and_fal_share_family_key(self):
        self.assertEqual(
            model_family_key("FLUX 3 Klein"),
            model_family_key("FLUX 3 Klein Base Text to Image"))
        self.assertEqual(model_family_key("FLUX 3 Klein"), "flux-3-klein")

    def test_opps_keeps_earliest_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            opps_path = os.path.join(d, "opportunities.json")
            bfl = {
                "term": "FLUX 3 Klein", "source": "bfl_official",
                "source_url": "https://bfl.ai/blog/flux-3-klein",
                "commercial_score": 9, "event_type": "new_model",
                "published_at": "2026-10-15",
            }
            fal = {
                "term": "FLUX 3 Klein Base Text to Image", "source": "api_platform",
                "source_url": "https://fal.ai/models/blackforestlabs/flux-3/klein/base/text-to-image/lora",
                "commercial_score": 8, "event_type": "new_model",
            }
            n = record_opportunities([bfl, fal], path=opps_path)
            self.assertEqual(n, 1)
            data = json.load(open(opps_path, encoding="utf-8"))
            self.assertEqual(len(data), 1)
            rec = data[0]
            # 发现时间取最早来源：BFL 公告日 2026-10-15，而非 fal 上架日
            self.assertTrue(rec["discovered_at"].startswith("2026-10-15"))
            self.assertEqual(rec["term"], "FLUX 3 Klein")
            variant_terms = [v["term"] for v in rec.get("variants", [])]
            self.assertIn("FLUX 3 Klein Base Text to Image", variant_terms)


if __name__ == "__main__":
    unittest.main()
