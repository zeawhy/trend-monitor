"""第 3 项测试:HuggingFace 来源重写(模型名保真/7 天新模型/new_model 标记)。"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw

NOW = datetime.now(timezone.utc)


class FakeCardData:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return dict(self._d)


def fake_model(mid, created_days_ago, pipeline_tag=None, license_=None, ipm="unset", likes=10, downloads=500):
    m = SimpleNamespace(
        id=mid,
        created_at=NOW - timedelta(days=created_days_ago),
        last_modified=NOW,  # 老模型也会有新 last_modified,不应据此放行
        pipeline_tag=pipeline_tag,
        card_data=FakeCardData({"license": license_}) if license_ else None,
        tags=[],
        likes=likes,
        downloads=downloads,
    )
    if ipm != "unset":
        m.inference_provider_mapping = ipm
    return m


def live_mapping():
    return [SimpleNamespace(provider="novita", status="live")]


class FakeHfApi:
    def __init__(self, models, fail_on_ipm=False):
        self._models = models
        self._fail = fail_on_ipm

    def list_models(self, sort=None, limit=None, expand=None):
        assert sort == "trendingScore", "只允许 trendingScore 排序"
        if self._fail and expand and "inferenceProviderMapping" in expand:
            raise ValueError("expand not supported")
        return list(self._models)


class TestLicenseBlocks(unittest.TestCase):
    def test_blocks_nc(self):
        for lic in ["cc-by-nc-4.0", "CC-BY-NC-SA-4.0", "non-commercial", "research only", "openrail-nc"]:
            self.assertTrue(tw._license_blocks_commercial(lic), lic)

    def test_allows_commercial_and_unknown(self):
        for lic in ["mit", "apache-2.0", "other", "bsd-3-clause", None, ""]:
            self.assertFalse(tw._license_blocks_commercial(lic), lic)


class TestFetchHuggingFace(unittest.TestCase):
    def _run(self, models, fail_on_ipm=False):
        with mock.patch.object(tw, "HfApi", lambda: FakeHfApi(models, fail_on_ipm)):
            return tw.fetch_huggingface_trending()

    def test_old_model_excluded_even_if_recently_modified(self):
        models = [fake_model("org/OldModel-1.0", created_days_ago=30)]
        self.assertEqual(self._run(models), [])

    def test_term_keeps_name_and_version(self):
        models = [fake_model("Wan-AI/Wan2.5-T2V", created_days_ago=1, pipeline_tag="text-to-video", license_="apache-2.0", ipm=live_mapping())]
        r = self._run(models)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["term"], "Wan2.5-T2V")
        self.assertEqual(r[0]["hf_model_id"], "Wan-AI/Wan2.5-T2V")
        self.assertEqual(r[0]["source_url"], "https://huggingface.co/Wan-AI/Wan2.5-T2V")

    def test_new_model_event_type(self):
        models = [fake_model("org/VidGen-2", created_days_ago=2, pipeline_tag="text-to-video", license_="apache-2.0", ipm=live_mapping())]
        r = self._run(models)
        self.assertEqual(r[0]["event_type"], "new_model")
        self.assertTrue(r[0]["api_available"])
        self.assertEqual(tw.EVENT_TYPE_PRIORITY["new_model"], -1)

    def test_nc_license_not_new_model(self):
        models = [fake_model("org/VidGen-2", created_days_ago=2, pipeline_tag="text-to-video", license_="cc-by-nc-4.0", ipm=live_mapping())]
        r = self._run(models)
        self.assertNotEqual(r[0]["event_type"], "new_model")

    def test_no_live_provider_not_new_model(self):
        models = [fake_model("org/ImgGen-1", created_days_ago=2, pipeline_tag="text-to-image", license_="mit", ipm=[])]
        r = self._run(models)
        self.assertFalse(r[0]["api_available"])
        self.assertNotEqual(r[0]["event_type"], "new_model")

    def test_expand_unsupported_api_available_none(self):
        models = [fake_model("org/VidGen-2", created_days_ago=2, pipeline_tag="text-to-video", license_="mit", ipm=live_mapping())]
        r = self._run(models, fail_on_ipm=True)
        self.assertEqual(len(r), 1)
        self.assertIsNone(r[0]["api_available"])
        self.assertNotEqual(r[0]["event_type"], "new_model")

    def test_max_five_results(self):
        models = [fake_model(f"org/M{i}-1", created_days_ago=1, pipeline_tag="text-to-image", license_="mit", ipm=live_mapping()) for i in range(8)]
        r = self._run(models)
        self.assertEqual(len(r), 5)


if __name__ == "__main__":
    unittest.main()
