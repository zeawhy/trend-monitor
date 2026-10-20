#!/usr/bin/env python3
"""页面效果追踪:记录每个新词页面的选题、投入、上线 URL、收录状态与 GSC 数据,
以及变现路线的成本/收入与止损复核(第 7 天 kill_by)。

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
  site_type       变现路线: quick_paid_site / tool_page / content(旧记录默认 content)
  costs           成本列表 [{item, amount, date}]
  revenue         收入列表 [{date, gross, api_cost, fees, refunds}]
  kill_by         止损复核日(上线后第 7 天,YYYY-MM-DD,add 时自动生成)
  merged_into     并入的目标页面 URL(outcome=merged 时填)
  outcome         running / killed / merged / kept

旧结构记录在 load 时自动补齐新字段默认值(向后兼容,不丢数据)。

用法:
  python3 page_tracking.py add --term "solidcraft" --url "https://yuliuswiki.com/solidcraft" --reason "产品发布+社区热度,证据:GSC 有展现" [--site-type content]
  python3 page_tracking.py update --term "solidcraft" --status indexed --effort "建站 90min" [--outcome kept] [--merged-into URL] [--site-type tool_page]
  python3 page_tracking.py gsc --term "solidcraft" --date 2026-10-27 --impressions 120 --clicks 3 --position 8.5
  python3 page_tracking.py revenue --term "wan2-tool" --date 2026-10-27 --gross 99 --api-cost 12 --fees 3 --refunds 0
  python3 page_tracking.py cost --term "wan2-tool" --item "域名" --amount 68 [--date 2026-10-21]
  python3 page_tracking.py pnl
  python3 page_tracking.py list
  python3 page_tracking.py show --term "solidcraft"
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

TRACKING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hidden_files", "page_tracking.json"
)

VALID_STATUSES = ("deploying", "sitemap_updated", "index_checked", "indexed")
SITE_TYPES = ("quick_paid_site", "tool_page", "content")
OUTCOMES = ("running", "killed", "merged", "kept")
KILL_BY_DAYS = 7  # 上线后第几天做止损复核
PNL_DAYS = (1, 7, 28)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(s):
    """把 ISO 时间或 YYYY-MM-DD 解析成 date,失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _plus_days(iso_str, days):
    d = _parse_date(iso_str)
    if d is None:
        return None
    return (d + timedelta(days=days)).isoformat()


def migrate_record(rec: dict) -> dict:
    """给旧结构记录补齐变现/止损字段默认值(不覆盖已有值)。"""
    if not isinstance(rec, dict):
        rec = {}
    rec.setdefault("site_type", "content")
    rec.setdefault("costs", [])
    rec.setdefault("revenue", [])
    if not rec.get("kill_by"):
        base = rec.get("decided_at") or rec.get("created_at")
        rec["kill_by"] = _plus_days(base, KILL_BY_DAYS) if base else None
    rec.setdefault("merged_into", "")
    rec.setdefault("outcome", "running")
    return rec


def load() -> list:
    if os.path.exists(TRACKING_PATH):
        with open(TRACKING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [migrate_record(r) for r in data]
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
    created = now_iso()
    rec = {
        "term": args.term.strip(),
        "topic_url": args.url or "",
        "decided_at": created,
        "decision_reason": args.reason or "",
        "effort_notes": args.effort or "",
        "index_status": "deploying",
        "gsc": [],
        "created_at": created,
        "site_type": args.site_type or "content",
        "costs": [],
        "revenue": [],
        "kill_by": _plus_days(created, KILL_BY_DAYS),
        "merged_into": "",
        "outcome": "running",
    }
    data.append(rec)
    save(data)
    print(f"已记录: {args.term} -> {rec['topic_url']} (状态: deploying, 路线: {rec['site_type']}, kill_by: {rec['kill_by']})")


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
    if args.site_type:
        rec["site_type"] = args.site_type
    if args.outcome:
        rec["outcome"] = args.outcome
    if args.merged_into:
        rec["merged_into"] = args.merged_into
    save(data)
    print(f"已更新: {args.term} (状态: {rec['index_status']}, outcome: {rec.get('outcome')})")


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


def cmd_revenue(args):
    data = load()
    rec = find(data, args.term)
    if not rec:
        print(f"找不到记录: {args.term},先用 add 创建")
        sys.exit(1)
    entry = {
        "date": args.date,
        "gross": args.gross,
        "api_cost": args.api_cost,
        "fees": args.fees,
        "refunds": args.refunds,
    }
    rec.setdefault("revenue", [])
    rec["revenue"] = [r for r in rec["revenue"] if r.get("date") != args.date]
    rec["revenue"].append(entry)
    rec["revenue"].sort(key=lambda r: r.get("date", ""))
    save(data)
    net = args.gross - args.api_cost - args.fees - args.refunds
    print(f"收入已记录: {args.term} @ {args.date}: gross {args.gross} - api {args.api_cost} - fees {args.fees} - refunds {args.refunds} = 净 {net:.2f}(未扣成本)")


def cmd_cost(args):
    data = load()
    rec = find(data, args.term)
    if not rec:
        print(f"找不到记录: {args.term},先用 add 创建")
        sys.exit(1)
    date = args.date or datetime.now(timezone.utc).date().isoformat()
    rec.setdefault("costs", [])
    rec["costs"].append({"item": args.item, "amount": args.amount, "date": date})
    rec["costs"].sort(key=lambda c: c.get("date", ""))
    save(data)
    total = sum(c.get("amount") or 0 for c in rec["costs"])
    print(f"成本已记录: {args.term}: {args.item} {args.amount} @ {date}(累计成本 {total:.2f})")


def _net_asof(rec: dict, day_n: int):
    """上线后第 day_n 天的累计净收入;数据不完整返回 None(显示「未知」,不当 0)。
    净收入 = 窗口内 revenue 的 (gross - api_cost - fees - refunds) 之和 - 窗口内成本之和。
    窗口 = [上线日, 上线日 + day_n 天];窗口内无 revenue、或从未录过成本 → 未知。"""
    base = _parse_date(rec.get("decided_at") or rec.get("created_at"))
    if base is None:
        return None
    end = base + timedelta(days=day_n)
    revs = [
        r for r in (rec.get("revenue") or [])
        if (_parse_date(r.get("date")) is not None and _parse_date(r.get("date")) <= end)
    ]
    if not revs:
        return None
    if not rec.get("costs"):
        return None
    costs = [
        c for c in rec["costs"]
        if (_parse_date(c.get("date")) is not None and _parse_date(c.get("date")) <= end)
    ]
    rev_net = sum(
        (r.get("gross") or 0) - (r.get("api_cost") or 0) - (r.get("fees") or 0) - (r.get("refunds") or 0)
        for r in revs
    )
    cost_sum = sum((c.get("amount") or 0) for c in costs)
    return rev_net - cost_sum


def summarize_pnl(records: list) -> dict:
    """按 site_type 汇总第 1/7/28 天净收入:{site_type: {day: value|None}}。
    某路线某天数下没有任何一条记录数据完整 → None(未知)。"""
    out = {}
    for rec in records:
        st = rec.get("site_type") or "content"
        bucket = out.setdefault(st, {d: None for d in PNL_DAYS})
        for d in PNL_DAYS:
            v = _net_asof(rec, d)
            if v is not None:
                bucket[d] = (bucket[d] or 0) + v
    return out


def cmd_pnl(_args):
    data = load()
    if not data:
        print("暂无页面记录")
        return
    summary = summarize_pnl(data)
    print("按 site_type 汇总净收入(上线后累计;净收入 = gross - api_cost - fees - refunds - 成本;数据缺失显示「未知」):")
    print(f"{'site_type':<16} {'第1天':>10} {'第7天':>10} {'第28天':>10}")
    for st in SITE_TYPES:
        if st not in summary:
            continue
        cells = []
        for d in PNL_DAYS:
            v = summary[st][d]
            cells.append(f"{v:>10.2f}" if v is not None else f"{'未知':>10}")
        print(f"{st:<16} {' '.join(cells)}")


def cmd_list(_args):
    data = load()
    if not data:
        print("暂无页面记录")
        return
    today = datetime.now(timezone.utc).date()
    for rec in data:
        gsc = rec.get("gsc") or []
        last = gsc[-1] if gsc else None
        gsc_info = (
            f" | GSC最新 {last['date']}: {last['impressions']}展现/{last['clicks']}点击/排名{last['position']}"
            if last
            else ""
        )
        print(f"- {rec['term']} [{rec['index_status']}] {rec.get('topic_url','')}{gsc_info}")
        kb = rec.get("kill_by")
        kb_date = _parse_date(kb)
        if kb_date and kb_date <= today and (rec.get("outcome") or "running") == "running":
            print(f"  ⚠️ 到期复核（kill_by {kb}）：净收入是否覆盖成本？覆盖不了就 killed；搜索仍持续的就 merged 到 yuliusbox")


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
    a.add_argument("--site-type", choices=SITE_TYPES, default="content", help="变现路线(默认 content)")
    a.set_defaults(fn=cmd_add)

    u = sub.add_parser("update", help="更新状态/投入/URL/路线/结局")
    u.add_argument("--term", required=True)
    u.add_argument("--status", choices=VALID_STATUSES)
    u.add_argument("--effort", default="")
    u.add_argument("--url", default="")
    u.add_argument("--site-type", choices=SITE_TYPES)
    u.add_argument("--outcome", choices=OUTCOMES)
    u.add_argument("--merged-into", default="")
    u.set_defaults(fn=cmd_update)

    g = sub.add_parser("gsc", help="记录某日 GSC 快照(7/14/28 天)")
    g.add_argument("--term", required=True)
    g.add_argument("--date", required=True, help="YYYY-MM-DD")
    g.add_argument("--impressions", type=int, default=0)
    g.add_argument("--clicks", type=int, default=0)
    g.add_argument("--position", type=float, default=0)
    g.set_defaults(fn=cmd_gsc)

    r = sub.add_parser("revenue", help="录入某日收入(手动,不接 Stripe)")
    r.add_argument("--term", required=True)
    r.add_argument("--date", required=True, help="YYYY-MM-DD")
    r.add_argument("--gross", type=float, required=True)
    r.add_argument("--api-cost", type=float, required=True)
    r.add_argument("--fees", type=float, required=True)
    r.add_argument("--refunds", type=float, required=True)
    r.set_defaults(fn=cmd_revenue)

    c = sub.add_parser("cost", help="录入一笔成本")
    c.add_argument("--term", required=True)
    c.add_argument("--item", required=True)
    c.add_argument("--amount", type=float, required=True)
    c.add_argument("--date", default="", help="YYYY-MM-DD,默认今天")
    c.set_defaults(fn=cmd_cost)

    sub.add_parser("pnl", help="按 site_type 汇总第 1/7/28 天净收入").set_defaults(fn=cmd_pnl)
    sub.add_parser("list", help="列出所有页面").set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="查看单条记录")
    s.add_argument("--term", required=True)
    s.set_defaults(fn=cmd_show)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
