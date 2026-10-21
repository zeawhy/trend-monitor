"""API 平台 (fal.ai / Replicate) 新上架模型抓取测试。"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw

NOW_UTC = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)


class TestApiPlatformNewModels(unittest.TestCase):
    def test_is_media_category(self):
        self.assertTrue(tw._is_media_category("text-to-video"))
        self.assertTrue(tw._is_media_category("image-to-image"))
        self.assertTrue(tw._is_media_category("text-to-speech"))
        self.assertTrue(tw._is_media_category("video-generation"))
        self.assertTrue(tw._is_media_category("voice-cloning"))
        self.assertFalse(tw._is_media_category("text-classification"))
        self.assertFalse(tw._is_media_category("tabular-data"))
        self.assertFalse(tw._is_media_category(""))
        self.assertFalse(tw._is_media_category(None))

    def test_fal_filters_date_and_category(self):
        cutoff = NOW_UTC - timedelta(days=7)
        mock_data = {
            "models": [
                {
                    "endpoint_id": "fal-ai/recent-video",
                    "metadata": {
                        "display_name": "Recent Video Model",
                        "category": "text-to-video",
                        "date": (NOW_UTC - timedelta(days=2)).isoformat(),
                        "description": "A recent text-to-video model",
                        "unit_price": "$0.05/sec",
                    }
                },
                {
                    "endpoint_id": "fal-ai/old-video",
                    "metadata": {
                        "display_name": "Old Video Model",
                        "category": "text-to-video",
                        "date": (NOW_UTC - timedelta(days=15)).isoformat(),
                    }
                },
                {
                    "endpoint_id": "fal-ai/recent-text",
                    "metadata": {
                        "display_name": "Recent Text Classifier",
                        "category": "text-classification",
                        "date": (NOW_UTC - timedelta(days=1)).isoformat(),
                    }
                }
            ]
        }

        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_data

        with mock.patch("requests.get", return_value=mock_resp):
            res = tw._fetch_fal_new_models(cutoff, limit=5)

        self.assertEqual(len(res), 1)
        item = res[0]
        self.assertEqual(item["term"], "Recent Video Model")
        self.assertEqual(item["source"], "api_platform")
        self.assertTrue(item["api_available"])
        self.assertEqual(item["event_type"], "new_model")
        self.assertIn("fal.ai", item["note"])
        self.assertIn("$0.05/sec", item["note"])
        self.assertEqual(item["source_url"], "https://fal.ai/models/fal-ai/recent-video")

    def test_fal_key_from_env_only(self):
        cutoff = NOW_UTC - timedelta(days=7)
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models": []}

        # 1. 环境变量有 FAL_KEY
        with mock.patch.dict(os.environ, {"FAL_KEY": "test-fal-key-123"}), \
             mock.patch("requests.get", return_value=mock_resp) as mock_get:
            tw._fetch_fal_new_models(cutoff, limit=5)
            headers = mock_get.call_args[1]["headers"]
            self.assertEqual(headers.get("Authorization"), "Key test-fal-key-123")

        # 2. 环境变量无 FAL_KEY
        env = {k: v for k, v in os.environ.items() if k != "FAL_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("requests.get", return_value=mock_resp) as mock_get2:
            tw._fetch_fal_new_models(cutoff, limit=5)
            headers2 = mock_get2.call_args[1]["headers"]
            self.assertNotIn("Authorization", headers2)

    def test_replicate_token_required_and_reads_from_env(self):
        cutoff = NOW_UTC - timedelta(days=7)
        # 1. 无 token 时不发网络请求，直接返回空
        env = {k: v for k, v in os.environ.items() if k != "REPLICATE_API_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("requests.get") as mock_get:
            res = tw._fetch_replicate_new_models(cutoff, limit=5)
            self.assertEqual(res, [])
            mock_get.assert_not_called()

        # 2. 有 token 时发送请求并携带 Bearer token
        mock_data = {
            "results": [
                {
                    "owner": "test-owner",
                    "name": "voice-cloner-v1",
                    "description": "Audio voice model",
                    "run_count": 2500,
                    "latest_version": {
                        "created_at": (NOW_UTC - timedelta(days=1)).isoformat()
                    }
                }
            ]
        }
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_data

        with mock.patch.dict(os.environ, {"REPLICATE_API_TOKEN": "rep-test-token"}), \
             mock.patch("requests.get", return_value=mock_resp) as mock_get:
            res = tw._fetch_replicate_new_models(cutoff, limit=5)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["term"], "Voice Cloner V1")
            self.assertEqual(res[0]["platform"], "replicate")
            headers = mock_get.call_args[1]["headers"]
            self.assertEqual(headers.get("Authorization"), "Bearer rep-test-token")

    def test_fetch_api_platform_silent_on_error(self):
        # 网络异常时静默跳过，不抛异常
        with mock.patch("requests.get", side_effect=Exception("Connection reset")):
            res = tw.fetch_api_platform_new_models()
            self.assertEqual(res, [])

    def test_fetch_api_platform_limit_and_seen_dedup(self):
        now_iso = NOW_UTC.isoformat()
        sample_models = [
            {
                "term": f"Media Model {i}",
                "source": "api_platform",
                "heat": 80,
                "intent": "tool",
                "commercial_score": 8,
                "event_type": "new_model",
                "note": f"model {i}",
                "source_url": f"https://fal.ai/models/org/model-{i}",
                "api_available": True,
            }
            for i in range(10)
        ]

        # 模拟 fal 返回 10 个候选
        with mock.patch("trend_watch._fetch_fal_new_models", return_value=sample_models):
            # 1. 默认无 seen，最多输出 5 条
            res_all = tw.fetch_api_platform_new_models(seen=None, limit=5)
            self.assertEqual(len(res_all), 5)
            self.assertEqual(res_all[0]["term"], "Media Model 0")

            # 2. 模拟 seen 已经记录了 Model 0 和 Model 1（且在冷却期）
            seen = {
                "media model 0": {
                    "last_notified_at": (NOW_UTC - timedelta(days=1)).isoformat(),
                    "sources": ["api_platform"],
                    "project_url": "https://fal.ai/models/org/model-0"
                },
                "media model 1": {
                    "last_notified_at": (NOW_UTC - timedelta(days=1)).isoformat(),
                    "sources": ["api_platform"],
                    "project_url": "https://fal.ai/models/org/model-1"
                }
            }
            res_dedup = tw.fetch_api_platform_new_models(seen=seen, limit=5)
            self.assertEqual(len(res_dedup), 5)
            # 跳过了 Model 0 和 Model 1，首个应为 Model 2
            self.assertEqual(res_dedup[0]["term"], "Media Model 2")

    def test_fal_merges_flux3_and_fibo_variants(self):
        # 真实样本测试: FLUX 3 Max 的 3 个变体和 Fibo 2.0 的 2 个变体合并为 1 条
        cutoff = NOW_UTC - timedelta(days=7)
        mock_data = {
            "models": [
                {
                    "endpoint_id": "blackforestlabs/flux-3-max/text-to-video",
                    "metadata": {
                        "display_name": "FLUX 3 Max Text to Video",
                        "category": "text-to-video",
                        "date": NOW_UTC.isoformat(),
                        "description": "fal's FLUX 3 Max text-to-video variant",
                    }
                },
                {
                    "endpoint_id": "blackforestlabs/flux-3-max/keyframes-to-video",
                    "metadata": {
                        "display_name": "FLUX 3 Max Keyframes to Video",
                        "category": "image-to-video",
                        "date": NOW_UTC.isoformat(),
                        "description": "fal's FLUX 3 Max keyframes variant",
                    }
                },
                {
                    "endpoint_id": "blackforestlabs/flux-3-max/image-to-video",
                    "metadata": {
                        "display_name": "FLUX 3 Max Image to Video",
                        "category": "image-to-video",
                        "date": NOW_UTC.isoformat(),
                        "description": "fal's FLUX 3 Max image-to-video variant",
                    }
                },
                {
                    "endpoint_id": "bria/fibo-2/generate",
                    "metadata": {
                        "display_name": "Fibo 2.0 Generate",
                        "category": "text-to-image",
                        "date": NOW_UTC.isoformat(),
                        "description": "Fibo 2.0 Generate text-to-image",
                    }
                },
                {
                    "endpoint_id": "bria/fibo-2/edit",
                    "metadata": {
                        "display_name": "Fibo 2.0 Edit",
                        "category": "image-to-image",
                        "date": NOW_UTC.isoformat(),
                        "description": "Fibo 2.0 Edit instruction-based edits",
                    }
                },
            ]
        }
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_data

        with mock.patch("requests.get", return_value=mock_resp):
            res = tw._fetch_fal_new_models(cutoff, limit=5)

        self.assertEqual(len(res), 2)
        flux = next(it for it in res if "FLUX 3 Max" in it["term"])
        self.assertEqual(flux["term"], "FLUX 3 Max")
        self.assertEqual(flux["source_url"], "https://fal.ai/models/blackforestlabs/flux-3-max")
        self.assertEqual(len(flux["variants"]), 3)
        self.assertEqual(
            [v["endpoint"] for v in flux["variants"]],
            [
                "blackforestlabs/flux-3-max/text-to-video",
                "blackforestlabs/flux-3-max/keyframes-to-video",
                "blackforestlabs/flux-3-max/image-to-video",
            ]
        )

        fibo = next(it for it in res if "Fibo 2.0" in it["term"])
        self.assertEqual(fibo["term"], "Fibo 2.0")
        self.assertEqual(fibo["source_url"], "https://fal.ai/models/bria/fibo-2")
        self.assertEqual(len(fibo["variants"]), 2)
        self.assertEqual(
            [v["endpoint"] for v in fibo["variants"]],
            ["bria/fibo-2/generate", "bria/fibo-2/edit"]
        )

        # 验证热度为 null 且 heat_source 为 none
        self.assertIsNone(flux["heat"])
        self.assertEqual(flux.get("heat_source"), "none")
        self.assertIsNone(fibo["heat"])
        self.assertEqual(fibo.get("heat_source"), "none")

        # 验证 seen_terms 的 key 用模型族名，避免后续变体重复推送
        seen = {}
        key = tw.resolve_seen_key(flux["term"], flux["source_url"], seen)
        self.assertEqual(key, "flux 3 max")



if __name__ == "__main__":
    unittest.main()
