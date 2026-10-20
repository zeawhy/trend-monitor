#!/usr/bin/env python3
"""页面效果追踪:记录每个新词页面的选题、投入、上线 URL、收录状态与 GSC 数据。

数据文件: hidden_files/page_tracking.json (JSON 数组,每个元素一条页面记录)
字段:
  term            选题词
  topic_url       上线 URL(如 https://yuliuswiki.com/xxx)
  decided_at      决策时间(ISO)
  decision_reason build/watch/reject 的理由(5 步决策输出)
  effort_notes    投入记录(如 "建站 90 分钟,Reddit 答 1 题")
  index_status    收录状态: deploying / sitemap_updated / index_checked / indexed
                  (只能通过 GSC/人工核实推进,不得用接口请求成功冒充)
  gsc             按日期的 GSC 数据快照列表 [{date, impressions, clicks, position}]
  created_at      记录创建时间

用法:
  python3 page_tracking.py add --term "solidcraft" --url "https://yuliuswiki.com/solidcraft" --reason "产品发布+社区热度,证据:GSC 有展现"
  python3 page_tracking.py update --term "solidcraft" --status indexed --effort "建站 90min"
  python3 page_tracking.py gsc --term "solidcraft" --date 2026-10-27 --impressions 120 --clicks 3 --position 8.5
  python3 page_tracking.py list
  python3 page_tracking.py show --term "solidcraft"
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

TRACKING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hidden_files", "page_tracking.json"
)

VALID_STATUSES = ("deploying", "sitemap_updated", "index_checked", "indexed")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load() -> list:
    if os.path.exists(TRACKING_PATH):
        with open(TRACKING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    return []


def save(data: list):
    os.makedirs(os.path.dirname(TRACKING_PATH), exist_ok=True)
    with open(TRACKING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find(data: list, term: str):
    key = term.strip().lower()
    for rec in data:
        if rec.get("term", "").strip().lower() == key:
            return rec
    return None


def cmd_add(args):
    data = load()
    if find(data, args.term):
        print(f"已存在记录: {args.term},用 update 更新")
        sys.exit(1)
    rec = {
        "term": args.term.strip(),
        "topic_url": args.url or "",
        "decided_at": now_iso(),
        "decision_reason": args.reason or "",
        "effort_notes": args.effort or "",
        "index_status": "deploying",
        "gsc": [],
        "created_at": now_iso(),
    }
    data.append(rec)
    save(data)
    print(f"已记录: {args.term} -> {rec['topic_url']} (状态: deploying)")


def cmd_update(args):
    data = load()
    rec = find(data, args.term)
    if not rec:
        print(f"找不到记录: {args.term},先用 add 创建")
        sys.exit(1)
    if args.status:
        if args.status not in VALID_STATUSES:
            print(f"非法状态: {args.status},可选: {', '.join(VALID_STATUSES)}")
            sys.exit(1)
        # 收录确认只能人工/GSC 核实后推进;记录是谁推进的
        rec["index_status"] = args.status
        rec["index_status_updated_at"] = now_iso()
    if args.effort:
        prev = rec.get("effort_notes") or ""
        rec["effort_notes"] = (prev + "；" + args.effort).strip("；") if prev else args.effort
    if args.url:
        rec["topic_url"] = args.url
    save(data)
    print(f"已更新: {args.term} (状态: {rec['index_status']})")


def cmd_gsc(args):
    data = load()
    rec = find(data, args.term)
    if not rec:
        print(f"找不到记录: {args.term}")
        sys.exit(1)
    snap = {
        "date": args.date,
        "impressions": args.impressions or 0,
        "clicks": args.clicks or 0,
        "position": args.position or 0,
    }
    rec.setdefault("gsc", [])
    rec["gsc"] = [s for s in rec["gsc"] if s.get("date") != args.date]
    rec["gsc"].append(snap)
    rec["gsc"].sort(key=lambda s: s.get("date", ""))
    save(data)
    print(f"GSC 快照已记录: {args.term} @ {args.date}: {args.impressions} 展现 / {args.clicks} 点击 / 排名 {args.position}")


def cmd_list(_args):
    data = load()
    if not data:
        print("暂无页面记录")
        return
    for rec in data:
        gsc = rec.get("gsc") or []
        last = gsc[-1] if gsc else None
        gsc_info = (
            f" | GSC最新 {last['date']}: {last['impressions']}展现/{last['clicks']}点击/排名{last['position']}"
            if last
            else ""
        )
        print(f"- {rec['term']} [{rec['index_status']}] {rec.get('topic_url','')}{gsc_info}")


def cmd_show(args):
    data = load()
    rec = find(data, args.term)
    if not rec:
        print(f"找不到记录: {args.term}")
        sys.exit(1)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="新词页面效果追踪")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="新增页面记录")
    a.add_argument("--term", required=True)
    a.add_argument("--url", default="")
    a.add_argument("--reason", default="")
    a.add_argument("--effort", default="")
    a.set_defaults(fn=cmd_add)

    u = sub.add_parser("update", help="更新状态/投入/URL")
    u.add_argument("--term", required=True)
    u.add_argument("--status", choices=VALID_STATUSES)
    u.add_argument("--effort", default="")
    u.add_argument("--url", default="")
    u.set_defaults(fn=cmd_update)

    g = sub.add_parser("gsc", help="记录某日 GSC 快照(7/14/28 天)")
    g.add_argument("--term", required=True)
    g.add_argument("--date", required=True, help="YYYY-MM-DD")
    g.add_argument("--impressions", type=int, default=0)
    g.add_argument("--clicks", type=int, default=0)
    g.add_argument("--position", type=float, default=0)
    g.set_defaults(fn=cmd_gsc)

    sub.add_parser("list", help="列出所有页面").set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="查看单条记录")
    s.add_argument("--term", required=True)
    s.set_defaults(fn=cmd_show)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
