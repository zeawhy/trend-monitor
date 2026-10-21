"""GitHub breakout 仓库抓取与同组织跨轮去重测试。"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw

NOW_UTC = datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc)


class TestGitHubBreakout(unittest.TestCase):
    def test_single_repo_emitted_when_below_threshold(self):
        # 仅有 1 个仓库且近 30 天无同 org 记录时，正常作为单仓库输出
        repo_data = {
            'items': [
                {
                    'name': 'awesome-tool',
                    'full_name': 'acme/awesome-tool',
                    'description': 'An awesome developer tool',
                    'created_at': (NOW_UTC - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'stargazers_count': 500,
                }
            ]
        }
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = repo_data

        with mock.patch('requests.get', return_value=mock_resp):
            res = tw.fetch_github_breakout(seen={})

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]['term'], 'awesome tool')
        self.assertEqual(res[0]['source_url'], 'https://github.com/acme/awesome-tool')

    def test_storytold_cross_run_series_dedup(self):
        # 真实样本测试: storytold 近 30 天已推送 ≥3 个仓库
        # 本轮仅出现 craft-launcher 和 figcraft (2个，不足单轮3个门槛)
        # 应触发跨轮组织去重，合并为 storytold suite，note 注明"同组织系列，疑似批量发布"
        seen = {
            'pdfcraft': {
                'last_notified_at': (NOW_UTC - timedelta(days=8)).isoformat(),
                'project_url': 'https://github.com/storytold/pdfcraft',
            },
            'lightcraft': {
                'last_notified_at': (NOW_UTC - timedelta(days=8)).isoformat(),
                'project_url': 'https://github.com/storytold/lightcraft',
            },
            'solidcraft': {
                'last_notified_at': (NOW_UTC - timedelta(days=3)).isoformat(),
                'project_url': 'https://github.com/storytold/solidcraft',
            },
        }

        repo_data = {
            'items': [
                {
                    'name': 'craft-launcher',
                    'full_name': 'storytold/craft-launcher',
                    'description': 'The Craft App Launcher',
                    'created_at': (NOW_UTC - timedelta(days=11)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'stargazers_count': 3675,
                },
                {
                    'name': 'figcraft',
                    'full_name': 'storytold/figcraft',
                    'description': 'FigCraft interface design and prototyping',
                    'created_at': (NOW_UTC - timedelta(days=11)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'stargazers_count': 2958,
                },
            ]
        }
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = repo_data

        with mock.patch('requests.get', return_value=mock_resp):
            res = tw.fetch_github_breakout(seen=seen)

        self.assertEqual(len(res), 1)
        suite = res[0]
        self.assertEqual(suite['term'], 'storytold suite')
        self.assertEqual(suite['source_url'], 'https://github.com/storytold')
        self.assertIn('同组织系列，疑似批量发布', suite['note'])
        self.assertIn('storytold', suite['note'])
        self.assertEqual(len(suite['members']), 2)
        self.assertIn('storytold/craft-launcher', suite['members'])
        self.assertIn('storytold/figcraft', suite['members'])


if __name__ == '__main__':
    unittest.main()
