#!/usr/bin/env python3
"""每日机会与复盘日报 (--digest):
输出:
  1. 昨日新机会 (前5)
  2. 待决定数量
  3. 超过48小时未处理清单
  4. 今日到复盘日 (kill_by) 的页面
  5. 各路线命中率

规则: 没有内容就不推送 (输出空时静默退出，不要打印空日报)。
每天 9:00 (Asia/Shanghai) 由 cron 触发。
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

from opps import (
    load_opportunities,
    _parse_datetime,
    OPPS_PATH,
    PAGE_TRACKING_PATH,
    load_page_tracking,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HIT_RATE_REPORT_PATH = os.path.join(BASE_DIR, "outputs", "hit_rate_report.json")
BJ_TZ = timezone(timedelta(hours=8))


def generate_digest(
    opps_path=OPPS_PATH,
    page_tracking_path=PAGE_TRACKING_PATH,
    hit_rate_path=HIT_RATE_REPORT_PATH,
    now_dt=None,
) -> str:
    """生成日报内容。若完全无需要处理/关注的内容，返回空字符串。"""
    if now_dt is None:
        now_dt = datetime.now(BJ_TZ)

    today_bj = now_dt.date()
    yesterday_bj = today_bj - timedelta(days=1)

    opps = load_opportunities(path=opps_path)
    page_data = load_page_tracking(path=page_tracking_path)

    # 1. 昨日新机会 (前 5)
    yesterday_opps = []
    pending_count = 0
    overdue_list = []

    now_utc = now_dt.astimezone(timezone.utc)

    for o in opps:
        disc_dt = _parse_datetime(o.get("discovered_at"))
        st = o.get("status")
        dec = o.get("decision")

        # 是否昨日发现
        if disc_dt:
            disc_bj = disc_dt.astimezone(BJ_TZ).date()
            if disc_bj == yesterday_bj:
                yesterday_opps.append(o)

        # 待决定数量
        if st in ("new", "evaluating") and not dec:
            pending_count += 1
            if disc_dt and (now_utc - disc_dt.astimezone(timezone.utc)).total_seconds() > 48 * 3600:
                overdue_list.append(o)

    # 昨日新机会按 score/heat 降序排序取前 5
    yesterday_opps.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    top_yesterday = yesterday_opps[:5]

    # 2. 今日到复盘日 (kill_by) 的页面
    today_reviews = []
    for p in page_data:
        kb = p.get("kill_by")
        if not kb:
            continue
        kb_dt = _parse_datetime(kb)
        if kb_dt and kb_dt.date() <= today_bj:
            if (p.get("outcome") or "running") == "running":
                today_reviews.append(p)

    # 判断是否"没有内容":
    # 若无昨日新机会、待决定为0、无>48h未处理、无今日复盘页面 -> 视为无内容，静默退出
    if not top_yesterday and pending_count == 0 and not overdue_list and not today_reviews:
        return ""

    # 读取各路线命中率
    route_hit_rates = []
    if os.path.exists(hit_rate_path):
        try:
            with open(hit_rate_path, "r", encoding="utf-8") as f:
                hr_data = json.load(f)
            paths = hr_data.get("paths") or {}
            for path_name, stats in paths.items():
                tot = stats.get("total", 0)
                hits = stats.get("hits", 0)
                rate = stats.get("hit_rate", "0%")
                note = stats.get("note", "")
                if note:
                    route_hit_rates.append(f"- **{path_name}**: {hits}/{tot} ({rate}) - {note}")
                else:
                    route_hit_rates.append(f"- **{path_name}**: {hits}/{tot} ({rate})")
        except Exception:
            pass

    # 组装 Markdown 日报
    lines = [
        f"# 每日机会与复盘日报 ({today_bj.isoformat()})",
        "",
        "## 1. 昨日新机会 (Top 5)",
    ]

    if top_yesterday:
        for idx, o in enumerate(top_yesterday, 1):
            term = o.get("term", "")
            src = o.get("source", "")
            route = o.get("suggested_route", "")
            sc = o.get("score", 0)
            url = o.get("page_url") or ""
            url_str = f" | [链接]({url})" if url else ""
            lines.append(f"{idx}. **{term}** ({src}) - 路线: `{route}`, 评分/热度: {sc}{url_str}")
    else:
        lines.append("- 昨日无新收录机会词")

    lines.extend([
        "",
        "## 2. 待决定概况",
        f"- 当前待决定数量: **{pending_count}** 个",
    ])

    if overdue_list:
        lines.append(f"- ⚠️ **超过 48 小时未处理清单 ({len(overdue_list)} 个)**:")
        for o in overdue_list:
            t = o.get("term", "")
            src = o.get("source", "")
            disc = str(o.get("discovered_at", ""))[:10]
            lines.append(f"  - **{t}** (来源: {src}, 发现于: {disc})")
    else:
        lines.append("- 无超过 48 小时未处理机会")

    lines.extend([
        "",
        "## 3. 今日到复盘日 (kill_by) 的页面",
    ])

    if today_reviews:
        for p in today_reviews:
            t = p.get("term", "")
            url = p.get("topic_url", "")
            kb = p.get("kill_by", "")
            st = p.get("site_type", "content")
            lines.append(f"- **{t}** [{st}] - 到期日: {kb} | [页面]({url}) (需复核: 净收入是否覆盖成本)")
    else:
        lines.append("- 今日无待复盘页面")

    if route_hit_rates:
        lines.extend([
            "",
            "## 4. 各路线命中率",
        ])
        lines.extend(route_hit_rates)

    return "\n".join(lines)


def run_digest(dry_run: bool = False) -> str:
    digest_text = generate_digest()
    if not digest_text.strip():
        # 无内容静默退出，不输出空日报
        return ""
    print(digest_text)
    return digest_text


def main():
    run_digest()


if __name__ == "__main__":
    main()
