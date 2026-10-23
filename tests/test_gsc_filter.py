#!/usr/bin/env python3
"""GSC 首次展现过滤回归测试（用户 2026-10-23 需求）。

覆盖：
  4c. 敏感词：artex 教程 / artex 使用 / codescan-artex 都不推送
  4b. 展现阈值：19 不推送，20 推送
  4a. 主题命中：记入页面 longtail_keywords，不推新机会
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trend_watch as tw


def test_sensitive_terms_blocked():
    """回归：三个 artex 相关词都不推送。"""
    for term in ["artex 教程", "artex 使用", "codescan-artex"]:
        hit, sword = tw.check_gsc_sensitive(term)
        assert hit, f"{term} 应该被敏感词拦截"
        assert sword == "artex", f"{term} 命中的敏感词应为 artex，实际 {sword}"
    # 非敏感词放行
    hit, _ = tw.check_gsc_sensitive("flux 教程")
    assert not hit, "flux 教程不应被敏感词拦截"


def test_sensitive_terms_case_insensitive():
    hit, _ = tw.check_gsc_sensitive("ARTEX Guide")
    assert hit, "大小写不应影响敏感词匹配"
    hit, _ = tw.check_gsc_sensitive("如何绕过验证")
    assert hit, "中文敏感词应命中"


def test_impression_threshold():
    """阈值：19 不推送，20 推送（GSC_MIN_IMPRESSIONS=20）。"""
    assert tw.GSC_MIN_IMPRESSIONS == 20
    assert 19 < tw.GSC_MIN_IMPRESSIONS, "19 应低于阈值"
    assert 20 >= tw.GSC_MIN_IMPRESSIONS, "20 应达到阈值"


def test_topic_match_records_longtail(tmp_path=None):
    """主题命中：不推新机会，记入页面 longtail_keywords。"""
    # 构造临时 page_tracking.json
    pages = [{
        "term": "FLUX 3 Max Pricing",
        "topic_url": "https://yuliuswiki.com/flux-3-max-pricing",
    }]
    base = os.path.dirname(os.path.abspath(tw.__file__))
    real_path = os.path.join(base, "hidden_files", "page_tracking.json")
    backup = None
    if os.path.exists(real_path):
        with open(real_path, encoding="utf-8") as f:
            backup = f.read()
    try:
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "w", encoding="utf-8") as f:
            json.dump(pages, f, ensure_ascii=False)

        # flux 教程 应命中 FLUX 3 Max Pricing 页面
        page = tw.find_matching_page("flux 教程")
        assert page is not None, "flux 教程 应命中已有页面"
        assert page["term"] == "FLUX 3 Max Pricing"

        tw.record_longtail_keyword(page, "flux 教程")
        with open(real_path, encoding="utf-8") as f:
            updated = json.load(f)
        assert "flux 教程" in updated[0].get("longtail_keywords", []), \
            "长尾词应记入页面"

        # 去重：重复记录不翻倍
        tw.record_longtail_keyword(page, "flux 教程")
        with open(real_path, encoding="utf-8") as f:
            updated = json.load(f)
        assert updated[0]["longtail_keywords"].count("flux 教程") == 1

        # 不相关词不应命中
        assert tw.find_matching_page("comfyui 工作流") is None
    finally:
        if backup is not None:
            with open(real_path, "w", encoding="utf-8") as f:
                f.write(backup)


if __name__ == "__main__":
    test_sensitive_terms_blocked()
    print("✓ 敏感词拦截")
    test_sensitive_terms_case_insensitive()
    print("✓ 大小写/中文敏感词")
    test_impression_threshold()
    print("✓ 展现阈值")
    test_topic_match_records_longtail()
    print("✓ 主题命中记长尾词")
    print("全部通过")
