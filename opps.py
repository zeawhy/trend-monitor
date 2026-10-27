#!/usr/bin/env python3
"""机会看板与状态管理:
记录新发现的词、变现建议路线、人工决策、上线状态与止损复核。

数据文件: outputs/opportunities.json (JSON 数组)
字段:
  term             候选词/项目名
  source           数据源 (api_platform, huggingface, github_breakout, reddit, gsc, x 等)
  discovered_at    首次发现时间 (ISO)
  score            商业价值分/热度分
  suggested_route  建议路线: wiki_article | box_tool | quick_paid_site | ignore
  decision         决策 (build | watch | drop 等)
  status           状态: new | evaluating | decided | building | live | review | dropped
  outcome          结局: running | merged | killed (或 None)
  page_url         已上线页面 URL
  deadline         处理截止时间或复盘日
  notes            备注
  drop_reason      放弃原因
  updated_at       最后更新时间 (ISO)

用法:
  python3 opps.py list [--status new,evaluating] [--view pending|overdue|review]
  python3 opps.py set <term> [--decision build] [--status building] [--url URL] [--note "..."] [--outcome running] [--drop-reason "..."] [--deadline YYYY-MM-DD] [--route wiki_article]
  python3 opps.py merge --into <target> <term1> <term2>... [--dry-run]
  python3 opps.py merge --auto [--dry-run] [--exclude <term>...]
  python3 opps.py sync
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from lock_utils import ConcurrencyLock
from trend_config import model_family_key, is_sensitive

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPPS_PATH = os.path.join(BASE_DIR, "outputs", "opportunities.json")
PAGE_TRACKING_PATH = os.path.join(BASE_DIR, "hidden_files", "page_tracking.json")
WORD_TRACKING_PATH = os.path.join(BASE_DIR, "hidden_files", "word_tracking.json")

VALID_ROUTES = ("wiki_article", "box_tool", "quick_paid_site", "ignore")
VALID_STATUSES = ("new", "evaluating", "decided", "building", "live", "review", "dropped")
VALID_OUTCOMES = ("running", "merged", "killed")

BJ_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(BJ_TZ).isoformat()


def _parse_datetime(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def map_suggested_route(monetization_path: str = None, intent: str = None, event_type: str = None) -> str:
    path = (monetization_path or "").strip().lower()
    if path == "quick_paid_site":
        return "quick_paid_site"
    elif path == "tool_page":
        return "box_tool"
    elif path == "content":
        return "wiki_article"
    elif path in ("reject", "watch"):
        return "ignore"

    if event_type == "new_model":
        return "quick_paid_site"
    if event_type in ("derivative", "version_bump", "version_update"):
        return "ignore"
    if intent == "tool":
        return "box_tool"
    if intent == "info":
        return "wiki_article"
    return "ignore"


def migrate_record(rec: dict) -> tuple[dict, bool]:
    """迁移单条机会记录，补齐默认值。返回 (迁移后记录, 是否有变更)。"""
    if not isinstance(rec, dict):
        rec = {}
    changed = False
    defaults = {
        "term": "",
        "source": "unknown",
        "discovered_at": now_iso(),
        "score": 0,
        "suggested_route": "wiki_article",
        "decision": None,
        "status": "new",
        "outcome": None,
        "page_url": "",
        "deadline": None,
        "notes": "",
        "drop_reason": None,
        "updated_at": now_iso(),
        "variants": [],
        "family_key": "",
    }
    for k, v in defaults.items():
        if k not in rec:
            rec[k] = v
            changed = True
    if not rec.get("family_key") and rec.get("term"):
        rec["family_key"] = model_family_key(rec["term"])
        changed = True
    if rec.get("variants") is None:
        rec["variants"] = []
        changed = True

    # 规范化枚举值
    if rec["suggested_route"] not in VALID_ROUTES:
        rec["suggested_route"] = map_suggested_route(rec.get("suggested_route"))
        changed = True
    if rec["status"] not in VALID_STATUSES:
        rec["status"] = "new"
        changed = True
    if rec["outcome"] and rec["outcome"] not in VALID_OUTCOMES:
        rec["outcome"] = "running" if rec["outcome"] == "kept" else None
        changed = True

    return rec, changed


def load_page_tracking(path: str = PAGE_TRACKING_PATH) -> list:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[opps] 读取 page_tracking 异常: {e}")
    return []


def sync_with_page_tracking(opps: list, page_data: list = None) -> bool:
    """已上线的词与 page_tracking 关联: page_url 或 term 对上就同步状态、URL与截止复核日。"""
    if page_data is None:
        page_data = load_page_tracking()
    if not page_data:
        return False

    url_map = {}
    term_map = {}
    for p in page_data:
        url = (p.get("topic_url") or "").strip()
        if url:
            url_map[url.lower()] = p
        t = (p.get("term") or "").strip().lower()
        if t:
            term_map[t] = p

    changed = False
    today = datetime.now(timezone.utc).date()

    for opp in opps:
        opp_url = (opp.get("page_url") or "").strip().lower()
        opp_term = (opp.get("term") or "").strip().lower()

        match = url_map.get(opp_url) if opp_url else None
        if not match and opp_term in term_map:
            match = term_map[opp_term]
            if not opp.get("page_url") and match.get("topic_url"):
                opp["page_url"] = match.get("topic_url")
                changed = True

        if match:
            # outcome 同步 (page_tracking 的 kept 视为 running)
            pt_outcome = match.get("outcome") or "running"
            norm_outcome = "running" if pt_outcome == "kept" else pt_outcome
            if norm_outcome in VALID_OUTCOMES and opp.get("outcome") != norm_outcome:
                opp["outcome"] = norm_outcome
                changed = True

            # deadline 与 kill_by 同步
            kb = match.get("kill_by")
            if kb and opp.get("deadline") != kb:
                opp["deadline"] = kb
                changed = True

            # 状态同步
            if norm_outcome == "killed":
                if opp.get("status") != "dropped":
                    opp["status"] = "dropped"
                    changed = True
            elif norm_outcome == "merged":
                if opp.get("status") != "review":
                    opp["status"] = "review"
                    changed = True
            else:
                kb_date = _parse_datetime(kb)
                if kb_date and kb_date.date() <= today:
                    if opp.get("status") != "review":
                        opp["status"] = "review"
                        changed = True
                else:
                    if opp.get("status") not in ("live", "review"):
                        opp["status"] = "live"
                        changed = True

    return changed


def _init_from_word_tracking(path: str = OPPS_PATH, wt_path: str = WORD_TRACKING_PATH) -> list:
    """首次没有 opportunities.json 时，从 word_tracking.json 迁移历史发现词。"""
    opps = []
    if wt_path and os.path.exists(wt_path):
        try:
            with open(wt_path, "r", encoding="utf-8") as f:
                wt_data = json.load(f)
            if isinstance(wt_data, list):
                for item in wt_data:
                    t = item.get("term")
                    if not t:
                        continue
                    mpath = item.get("monetization_path")
                    route = map_suggested_route(mpath)
                    disc = item.get("discovered_at") or now_iso()
                    disc_dt = _parse_datetime(disc)
                    deadline = (disc_dt + timedelta(hours=48)).isoformat() if disc_dt else None
                    rec = {
                        "term": t,
                        "source": item.get("source") or "unknown",
                        "discovered_at": disc,
                        "score": item.get("initial_heat") or 0,
                        "suggested_route": route,
                        "decision": None,
                        "status": "new",
                        "outcome": None,
                        "page_url": item.get("source_url") if "yulius" in (item.get("source_url") or "") else "",
                        "deadline": deadline,
                        "notes": "",
                        "drop_reason": None,
                        "updated_at": now_iso(),
                    }
                    opps.append(rec)
        except Exception as e:
            print(f"[opps] 从 word_tracking 初始化异常: {e}")

    sync_with_page_tracking(opps)
    save_opportunities(opps, path=path)
    return opps


def load_opportunities(path: str = OPPS_PATH, wt_path: str = None) -> list:
    """加载 opportunities.json。不存在时初始化；结构有升级时备份并向后兼容。"""
    if wt_path is None:
        wt_path = WORD_TRACKING_PATH if path == OPPS_PATH else None

    if not os.path.exists(path):
        return _init_from_word_tracking(path, wt_path=wt_path)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[opps] 加载 {path} 异常: {e}")
        return []

    if not isinstance(data, list):
        return []

    migrated = []
    any_migrated = False
    for r in data:
        rec, chg = migrate_record(r)
        if chg:
            any_migrated = True
        migrated.append(rec)

    # 关联同步 page_tracking
    if sync_with_page_tracking(migrated):
        any_migrated = True

    if any_migrated:
        save_opportunities(migrated, path=path, backup=True)

    return migrated


def save_opportunities(data: list, path: str = OPPS_PATH, backup: bool = False):
    """保存 opportunities.json。backup=True 时保存前备份原文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if backup and os.path.exists(path):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        bak = f"{path}.bak-{ts}"
        try:
            shutil.copy2(path, bak)
        except Exception as e:
            print(f"[opps] 备份失败(继续保存): {e}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _variant_entry(term, source, source_url, discovered_at, score):
    return {
        "term": term,
        "source": source or "unknown",
        "source_url": source_url or "",
        "discovered_at": discovered_at or now_iso(),
        "score": score if score is not None else 0,
    }


def _fold_variant(base: dict, var: dict):
    """把一条变体记录折进 base：variants 去重追加、discovered_at 取最早。"""
    variants = base.setdefault("variants", [])
    key = (var.get("term", "").strip().lower(), (var.get("source") or "").strip().lower())
    existing_keys = {(v.get("term", "").strip().lower(), (v.get("source") or "").strip().lower())
                     for v in variants if isinstance(v, dict)}
    if key not in existing_keys:
        variants.append(var)
    vd = _parse_datetime(var.get("discovered_at"))
    bd = _parse_datetime(base.get("discovered_at"))
    if vd and (not bd or vd < bd):
        base["discovered_at"] = var["discovered_at"]
    base["updated_at"] = now_iso()
    return base


def record_opportunities(candidates: list, path: str = OPPS_PATH, wt_path: str = None) -> int:
    """系统新发现的词自动写入 opportunities.json (status=new)。
    同一模型族（family_key 相同）只保留一条，变体折进 variants；
    官方来源（bfl_official）用公告日做 discovered_at（跨来源取最早）；
    命中敏感词表的直接记 dropped（drop_reason=sensitive），不推送。
    返回新增记录数量。
    """
    if not candidates:
        return 0

    with ConcurrencyLock():
        opps = load_opportunities(path, wt_path=wt_path)
        existing = {o.get("term", "").strip().lower(): o for o in opps if o.get("term")}
        family_index = {}
        for o in opps:
            fk = o.get("family_key") or model_family_key(o.get("term", ""))
            if fk and fk not in family_index:
                family_index[fk] = o

        new_count = 0
        now_str = now_iso()
        now_dt = _parse_datetime(now_str)
        deadline_48h = (now_dt + timedelta(hours=48)).isoformat() if now_dt else None

        for c in candidates:
            term = (c.get("term") or "").strip()
            if not term:
                continue
            if term.lower() in existing:
                continue

            score = c.get("commercial_score")
            if score is None:
                score = c.get("heat") or 0

            # 敏感兜底：命中敏感词表的不建 new，直接 dropped
            sens_hit, sens_word = is_sensitive(term)
            if sens_hit:
                rec = {
                    "term": term,
                    "source": c.get("source") or "unknown",
                    "discovered_at": now_str,
                    "score": score,
                    "suggested_route": "ignore",
                    "decision": None,
                    "status": "dropped",
                    "outcome": None,
                    "page_url": "",
                    "deadline": None,
                    "notes": f"敏感词自动过滤（命中 {sens_word}）",
                    "drop_reason": "sensitive",
                    "updated_at": now_str,
                    "variants": [],
                    "family_key": model_family_key(term),
                }
                opps.append(rec)
                existing[term.lower()] = rec
                new_count += 1
                print(f"[机会看板] 敏感词自动 dropped: {term}（命中 {sens_word}）")
                continue

            mpath = c.get("monetization_path")
            route = map_suggested_route(mpath, intent=c.get("intent"), event_type=c.get("event_type"))

            # 官方公告日优先（跨来源取最早发现时间）
            disc = now_str
            if c.get("source") == "bfl_official" and c.get("published_at"):
                pd = _parse_datetime(c["published_at"])
                if pd:
                    disc = pd.isoformat()

            # 同族归组：已存在的同族记录（非 dropped）直接折为变体，不新增
            fk = model_family_key(term)
            base = family_index.get(fk) if fk else None
            if base is not None and base.get("status") != "dropped":
                _fold_variant(base, _variant_entry(
                    term, c.get("source"), c.get("source_url"), disc, score))
                existing[term.lower()] = base
                print(f"[机会看板] 同族归组: {term} -> {base.get('term')}（变体）")
                continue

            rec = {
                "term": term,
                "source": c.get("source") or "unknown",
                "discovered_at": disc,
                "score": score,
                "suggested_route": route,
                "decision": None,
                "status": "new",
                "outcome": None,
                "page_url": "",
                "deadline": deadline_48h,
                "notes": c.get("note") or "",
                "drop_reason": None,
                "updated_at": now_str,
                "variants": [],
                "family_key": fk,
            }
            opps.append(rec)
            existing[term.lower()] = rec
            if fk and fk not in family_index:
                family_index[fk] = rec
            new_count += 1

        if new_count > 0:
            sync_with_page_tracking(opps)
            save_opportunities(opps, path=path)

        return new_count


def find_opp(opps: list, term: str):
    key = term.strip().lower()
    for o in opps:
        if o.get("term", "").strip().lower() == key:
            return o
    return None


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------
def cmd_list(args):
    with ConcurrencyLock():
        opps = load_opportunities()

    now = datetime.now(timezone.utc)
    filtered = []

    # 视图筛选
    if args.view == "pending":
        filtered = [o for o in opps if o.get("status") in ("new", "evaluating") and not o.get("decision")]
    elif args.view == "overdue":
        for o in opps:
            if o.get("status") in ("new", "evaluating") and not o.get("decision"):
                d = _parse_datetime(o.get("discovered_at"))
                if d and (now - d).total_seconds() > 48 * 3600:
                    filtered.append(o)
    elif args.view == "review":
        today = now.date()
        for o in opps:
            if o.get("status") == "review":
                filtered.append(o)
            elif o.get("status") == "live":
                dl = _parse_datetime(o.get("deadline"))
                if dl and dl.date() <= today:
                    filtered.append(o)
    else:
        filtered = opps

    # 状态过滤
    if args.status:
        target_statuses = {s.strip().lower() for s in args.status.split(",") if s.strip()}
        filtered = [o for o in filtered if o.get("status", "").lower() in target_statuses]

    if not filtered:
        print("未找到符合条件的机会记录")
        return

    print(f"找到 {len(filtered)} 条机会记录:")
    print(f"{'词 (term)':<24} {'状态':<10} {'路线':<16} {'评分':<6} {'来源':<14} {'发现时间':<12} {'截止/复盘':<12}")
    print("-" * 100)
    for o in filtered:
        t = o.get("term", "")[:23]
        st = o.get("status", "")
        rt = o.get("suggested_route", "")
        sc = str(o.get("score", ""))
        src = o.get("source", "")[:13]
        disc = str(o.get("discovered_at", ""))[:10]
        dl = str(o.get("deadline", ""))[:10]
        print(f"{t:<24} {st:<10} {rt:<16} {sc:<6} {src:<14} {disc:<12} {dl:<12}")
        if o.get("decision") or o.get("page_url") or o.get("notes"):
            extra = []
            if o.get("decision"):
                extra.append(f"决策: {o['decision']}")
            if o.get("outcome"):
                extra.append(f"结局: {o['outcome']}")
            if o.get("page_url"):
                extra.append(f"URL: {o['page_url']}")
            if o.get("notes"):
                extra.append(f"备注: {o['notes']}")
            print(f"  └─ {' | '.join(extra)}")


def cmd_set(args):
    with ConcurrencyLock():
        opps = load_opportunities()
        rec = find_opp(opps, args.term)
        if not rec:
            print(f"找不到词: {args.term}，可在全量或 fast 发现后自动录入，或检查拼写")
            sys.exit(1)

        updated_fields = []
        if args.decision:
            rec["decision"] = args.decision
            updated_fields.append(f"decision={args.decision}")
            if rec.get("status") in ("new", "evaluating") and not args.status:
                rec["status"] = "decided"
                updated_fields.append("status=decided")

        if args.status:
            if args.status not in VALID_STATUSES:
                print(f"非法状态: {args.status}，可选: {', '.join(VALID_STATUSES)}")
                sys.exit(1)
            rec["status"] = args.status
            updated_fields.append(f"status={args.status}")

        if args.url:
            rec["page_url"] = args.url
            updated_fields.append(f"page_url={args.url}")
            if rec.get("status") not in ("live", "review", "dropped") and not args.status:
                rec["status"] = "live"
                rec["outcome"] = "running"
                updated_fields.append("status=live, outcome=running")

        if args.note:
            prev = rec.get("notes") or ""
            rec["notes"] = f"{prev}；{args.note}".strip("；") if prev else args.note
            updated_fields.append(f"notes={rec['notes']}")

        if args.outcome:
            if args.outcome not in VALID_OUTCOMES:
                print(f"非法结局: {args.outcome}，可选: {', '.join(VALID_OUTCOMES)}")
                sys.exit(1)
            rec["outcome"] = args.outcome
            updated_fields.append(f"outcome={args.outcome}")

        if args.drop_reason:
            rec["drop_reason"] = args.drop_reason
            rec["status"] = "dropped"
            updated_fields.append(f"drop_reason={args.drop_reason}, status=dropped")

        if args.deadline:
            rec["deadline"] = args.deadline
            updated_fields.append(f"deadline={args.deadline}")

        if args.route:
            rec["suggested_route"] = args.route
            updated_fields.append(f"suggested_route={args.route}")

        rec["updated_at"] = now_iso()
        sync_with_page_tracking(opps)
        save_opportunities(opps)

        print(f"已更新 {args.term}: {', '.join(updated_fields)}")


def _merge_records_into(base: dict, others: list) -> dict:
    """把 others 折进 base：variants 合并、discovered_at 取最早、决策冲突记备注。"""
    base_terms = [base.get("term", "")]
    for o in others:
        _fold_variant(base, _variant_entry(
            o.get("term", ""), o.get("source"), o.get("page_url"),
            o.get("discovered_at"), o.get("score")))
        for v in (o.get("variants") or []):
            if isinstance(v, dict):
                _fold_variant(base, v)
        base_terms.append(o.get("term", ""))
        if o.get("decision") and not base.get("decision"):
            base["decision"] = o["decision"]
        elif o.get("decision") and base.get("decision") and o["decision"] != base["decision"]:
            note = f"合并冲突决策（{o.get('term')}）：{o['decision']}（保留 {base['decision']}）"
            base["notes"] = f"{base.get('notes') or ''}；{note}".strip("；")
        if o.get("drop_reason") and not base.get("drop_reason"):
            base["drop_reason"] = o["drop_reason"]
    base["notes"] = f"{base.get('notes') or ''}；合并自：{'、'.join(t for t in base_terms[1:] if t)}".strip("；")
    base["updated_at"] = now_iso()
    return base


def cmd_merge(args):
    with ConcurrencyLock():
        opps = load_opportunities()
        removed = []

        if args.auto:
            exclude = {t.strip().lower() for t in (args.exclude or [])}
            groups = {}
            for o in opps:
                fk = o.get("family_key") or model_family_key(o.get("term", ""))
                if not fk or o.get("term", "").strip().lower() in exclude:
                    continue
                groups.setdefault(fk, []).append(o)
            # 只自动合并状态兼容的组：全在 {new,evaluating,decided} 或全 dropped；
            # live/review/building 绝不自动合并（已有页面/在建，动它需人工）。
            targets = []
            for fk, members in groups.items():
                if len(members) < 2:
                    continue
                statuses = {m.get("status") for m in members}
                if statuses <= {"new", "evaluating", "decided"} or statuses == {"dropped"}:
                    targets.append((fk, members))
            print(f"自动合并预览：{len(opps)} 条 -> {len(opps) - sum(len(m) - 1 for _, m in targets)} 条，{len(targets)} 组")
            for fk, members in targets:
                members_sorted = sorted(members, key=lambda m: m.get("discovered_at") or "")
                print(f"  组 {fk}: {' + '.join(m.get('term', '') for m in members_sorted)} "
                      f"-> {members_sorted[0].get('term', '')}")
            if args.dry_run:
                print("[dry-run] 未写入")
                return
            for fk, members in targets:
                members_sorted = sorted(members, key=lambda m: m.get("discovered_at") or "")
                base = members_sorted[0]
                _merge_records_into(base, members_sorted[1:])
                for m in members_sorted[1:]:
                    opps.remove(m)
                    removed.append(m.get("term", ""))
            save_opportunities(opps, backup=True)
            print(f"已合并 {len(removed)} 条，剩余 {len(opps)} 条")
            return

        # 显式合并：--into 目标 + 若干被合并词
        if not args.into or not args.terms:
            print("显式合并需要 --into <目标词> 和至少一个被合并词")
            sys.exit(1)
        target = find_opp(opps, args.into)
        others = []
        for t in args.terms:
            if t.strip().lower() == args.into.strip().lower():
                continue
            o = find_opp(opps, t)
            if not o:
                print(f"找不到词: {t}")
                sys.exit(1)
            others.append(o)
        if not others:
            print("没有可合并的词")
            return
        if target is None:
            # 目标不存在：取最早的一条改名做 base，原词名也记为变体
            others_sorted = sorted(others, key=lambda m: m.get("discovered_at") or "")
            target = others_sorted[0]
            others = others_sorted[1:]
            orig_term = target.get("term", "")
            _fold_variant(target, _variant_entry(
                orig_term, target.get("source"), target.get("page_url"),
                target.get("discovered_at"), target.get("score")))
            target["term"] = args.into
            target["family_key"] = model_family_key(args.into)
        print(f"合并预览：{target.get('term')} <- {'、'.join(o.get('term', '') for o in others)}")
        if args.dry_run:
            print("[dry-run] 未写入")
            return
        _merge_records_into(target, others)
        for o in others:
            opps.remove(o)
            removed.append(o.get("term", ""))
        save_opportunities(opps, backup=True)
        print(f"已合并 {len(removed)} 条，剩余 {len(opps)} 条")


def cmd_sync(_args):
    with ConcurrencyLock():
        opps = load_opportunities()
        changed = sync_with_page_tracking(opps)
        if changed:
            save_opportunities(opps)
            print("已同步 page_tracking 状态到 opportunities.json")
        else:
            print("机会看板状态与 page_tracking 已保持最新，无变更")


def main(argv=None):
    ap = argparse.ArgumentParser(description="机会看板与状态管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # list
    l = sub.add_parser("list", help="查看机会看板")
    l.add_argument("--status", help="状态过滤，多个以逗号分隔 (new,evaluating,decided,building,live,review,dropped)")
    l.add_argument("--view", choices=["pending", "overdue", "review"], help="内置视图 (pending: 待决定, overdue: >48h未处理, review: 已上线待复盘)")
    l.set_defaults(fn=cmd_list)

    # set
    s = sub.add_parser("set", help="修改机会状态与信息")
    s.add_argument("term", help="目标词")
    s.add_argument("--decision", help="决策 (build|watch|drop等)")
    s.add_argument("--status", choices=VALID_STATUSES, help="更新状态")
    s.add_argument("--url", help="已上线页面链接")
    s.add_argument("--note", help="添加备注")
    s.add_argument("--outcome", choices=VALID_OUTCOMES, help="结局 (running|merged|killed)")
    s.add_argument("--drop-reason", help="放弃原因")
    s.add_argument("--deadline", help="处理截止日或复盘日 (YYYY-MM-DD)")
    s.add_argument("--route", "--suggested-route", dest="route", choices=VALID_ROUTES, help="建议路线")
    s.set_defaults(fn=cmd_set)

    # merge
    m = sub.add_parser("merge", help="合并同一模型族的多条记录（变体折进 variants）")
    m.add_argument("terms", nargs="*", help="显式合并的被合并词（与 --into 联用）")
    m.add_argument("--into", help="显式合并的目标词")
    m.add_argument("--auto", action="store_true", help="自动合并全部同族兼容组")
    m.add_argument("--exclude", nargs="*", help="自动合并时排除的词")
    m.add_argument("--dry-run", action="store_true", help="只预览不写入")
    m.set_defaults(fn=cmd_merge)

    # sync
    sy = sub.add_parser("sync", help="与 page_tracking 同步已上线页面状态")
    sy.set_defaults(fn=cmd_sync)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
