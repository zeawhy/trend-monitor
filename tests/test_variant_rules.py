"""变体合并与规则配置的回归测试。

覆盖：fal 模型族提取（Klein 4 端点归一族）、模型族 key、
敏感词表、衍生判定、热度归一化、快站降级。
运行：python3 -m unittest tests.test_variant_rules -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trend_config import (
    classify_derivative,
    is_sensitive,
    model_family_key,
    normalize_heat,
    quick_site_downgrade,
)
from trend_watch import _extract_fal_family


class TestFalFamily(unittest.TestCase):
    def test_klein_four_endpoints_one_family(self):
        cases = [
            ("blackforestlabs/flux-3/klein/edit-image/lora", "FLUX 3 Klein Edit"),
            ("blackforestlabs/flux-3/klein/text-to-image/lora", "FLUX 3 Klein Text to Image"),
            ("blackforestlabs/flux-3/klein/base/edit-image/lora", "FLUX 3 Klein Base Edit"),
            ("blackforestlabs/flux-3/klein/base/text-to-image/lora", "FLUX 3 Klein Base Text to Image"),
        ]
        paths = set()
        names = set()
        for endpoint, display in cases:
            path, name = _extract_fal_family(endpoint, display)
            paths.add(path)
            names.add(name)
        self.assertEqual(paths, {"blackforestlabs/flux-3/klein"})
        self.assertEqual(names, {"FLUX 3 Klein"})

    def test_max_and_fibo_unchanged(self):
        self.assertEqual(
            _extract_fal_family("blackforestlabs/flux-3-max/text-to-video", "FLUX 3 Max Text to Video"),
            ("blackforestlabs/flux-3-max", "FLUX 3 Max"))
        self.assertEqual(
            _extract_fal_family("bria/fibo-2/generate", "Fibo 2.0 Generate"),
            ("bria/fibo-2", "Fibo 2.0"))


class TestModelFamilyKey(unittest.TestCase):
    def test_klein_variants(self):
        self.assertEqual(model_family_key("flux-3-image-klein-comfy"), "flux-3-klein")
        self.assertEqual(model_family_key("FLUX 3 Klein Base Text to Image"), "flux-3-klein")

    def test_size_and_family(self):
        self.assertEqual(model_family_key("Eden Live 5B Video"), "eden-live")
        self.assertEqual(model_family_key("Eden Live 14B Video"), "eden-live")
        self.assertEqual(model_family_key("Kling 4.0 Video Editing"), "kling-4.0")


class TestSensitive(unittest.TestCase):
    def test_hits(self):
        for term in ("DeepSeek-V4.1-Flash-UNCENSORED", "qwen21 nsfw pose skill",
                     "artex 使用", "codescan-artex", "xx jailbreak tool", "渗透测试神器"):
            hit, word = is_sensitive(term)
            self.assertTrue(hit, term)
            self.assertTrue(word)

    def test_no_false_positive(self):
        hit, _ = is_sensitive("FLUX 3 Klein")
        self.assertFalse(hit)


class TestDerivative(unittest.TestCase):
    def test_quantization_and_suffixes(self):
        self.assertEqual(classify_derivative("llama-3-gguf"), "derivative")
        self.assertEqual(classify_derivative("qwen-gptq-int4"), "derivative")
        self.assertEqual(classify_derivative("model-awq"), "derivative")
        self.assertEqual(classify_derivative("某模型-abliterated"), "derivative")
        self.assertEqual(classify_derivative("qwen3-量化版"), "derivative")

    def test_version_bump(self):
        # 日期后缀（-1017、-20261017）判 version_bump
        self.assertEqual(classify_derivative("flux-klein-1017"), "version_bump")
        self.assertEqual(classify_derivative("qwen3-20261017"), "version_bump")

    def test_manual_drops_not_auto_classified(self):
        # GSQ-RCO、Spark-X2.6-4B 不在自动标记/日期后缀口径内，Part A 的
        # derivative_or_version_bump 是人工决定，不是分类器输出；分类器返回 None
        # 是符合设计的（同架构小版本升级靠 opps.merge 的 family 分组处理）
        self.assertIsNone(classify_derivative("DeepSeek-V4.1-Flash-GSQ-RCO"))
        self.assertIsNone(classify_derivative("Spark-X2.6-4B"))

    def test_normal_model_not_derivative(self):
        self.assertIsNone(classify_derivative("FLUX 3 Klein"))
        self.assertIsNone(classify_derivative("Carbon-X"))


class TestNormalizeHeat(unittest.TestCase):
    def test_none(self):
        self.assertEqual(normalize_heat(None), (None, "none"))
        self.assertEqual(normalize_heat(""), (None, "none"))

    def test_numbers(self):
        self.assertEqual(normalize_heat(211), (211, "reported"))
        self.assertEqual(normalize_heat("85"), (85, "reported"))
        self.assertEqual(normalize_heat("heat: 42"), (42, "reported"))


class TestQuickSiteDowngrade(unittest.TestCase):
    def test_mature_brand(self):
        downgrade, reason = quick_site_downgrade({"term": "HeyGen Video", "monetization_path": "quick_paid_site"})
        self.assertTrue(downgrade)
        self.assertIn("已成熟", reason)

    def test_open_weights(self):
        downgrade, reason = quick_site_downgrade({"term": "X Model", "monetization_path": "quick_paid_site",
                                                  "open_weights": True})
        self.assertTrue(downgrade)
        self.assertIn("开放权重", reason)

    def test_official_free_entry(self):
        downgrade, reason = quick_site_downgrade({"term": "X Model", "monetization_path": "quick_paid_site",
                                                  "official_free_url": "https://example.com/playground"})
        self.assertTrue(downgrade)
        self.assertIn("免费在线入口", reason)

    def test_high_image_price(self):
        downgrade, reason = quick_site_downgrade({"term": "X Model", "monetization_path": "quick_paid_site",
                                                  "unit_price": 0.09, "pipeline_tag": "image_generation"})
        self.assertTrue(downgrade)
        self.assertIn("单价", reason)

    def test_klein_not_downgraded(self):
        # FLUX Kommunity Licence 不在开放许可列表，fal 无免费入口字段时不降级
        downgrade, _reason = quick_site_downgrade({"term": "FLUX 3 Klein", "monetization_path": "quick_paid_site",
                                                   "license": "FLUX Kommunity Licence", "pipeline_tag": "image_generation",
                                                   "unit_price": 0.009})
        self.assertFalse(downgrade)


if __name__ == "__main__":
    unittest.main()
