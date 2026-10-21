#!/usr/bin/env python3
"""运行状态与断档监控工具 (last_run_utils)

记录与读取全量任务与 fast 任务的最新运行时间戳:
{
  "full_at": "2026-10-21T15:00:00+08:00",
  "fast_at": "2026-10-21T15:30:00+08:00",
  "status": "ok"
}

文件位置:
- dashboard/data/last_run.json (随看板部署到 Cloudflare Pages, 供 Worker 公开读取)
- outputs/last_run.json (本地 outputs 目录镜像, 便于备份到 trend-state)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DATA_DIR = os.path.join(BASE_DIR, "dashboard", "data")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")
DEFAULT_LAST_RUN_FILE = os.path.join(DASHBOARD_DATA_DIR, "last_run.json")
OUTPUTS_LAST_RUN_FILE = os.path.join(OUTPUTS_DIR, "last_run.json")

BJ_TZ = timezone(timedelta(hours=8))


def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """解析 ISO 8601 时间戳为带时区的 datetime"""
    if not dt_str:
        return None
    try:
        s = str(dt_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJ_TZ)
        return dt
    except Exception:
        return None


def load_last_run(file_path: Optional[str] = None) -> Dict[str, Any]:
    """读取 last_run.json。若不存在或格式损坏，返回空字典。"""
    paths_to_try = [file_path] if file_path else [DEFAULT_LAST_RUN_FILE, OUTPUTS_LAST_RUN_FILE]
    for p in paths_to_try:
        if p and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
    return {}


def record_last_run(
    run_type: str,
    status: str = "ok",
    dashboard_data_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """记录一次任务运行完成时间戳。

    - run_type: "full" 或 "fast"
    - status: "ok" 或 异常状态字符串
    - 写入 dashboard/data/last_run.json 与 outputs/last_run.json
    - 返回更新后的 state 字典
    """
    dash_dir = dashboard_data_dir or DASHBOARD_DATA_DIR
    out_dir = output_dir or OUTPUTS_DIR

    dash_file = os.path.join(dash_dir, "last_run.json")
    out_file = os.path.join(out_dir, "last_run.json")

    state = load_last_run(dash_file)
    if not state:
        state = load_last_run(out_file)

    if not now_iso:
        now_dt = datetime.now(timezone.utc).astimezone(BJ_TZ)
        now_iso = now_dt.isoformat()

    if run_type == "full":
        state["full_at"] = now_iso
        if "fast_at" not in state or not state["fast_at"]:
            state["fast_at"] = now_iso
    elif run_type == "fast":
        state["fast_at"] = now_iso
        if "full_at" not in state or not state["full_at"]:
            state["full_at"] = now_iso
    else:
        state[f"{run_type}_at"] = now_iso

    state["status"] = status

    for target_dir, target_file in [(dash_dir, dash_file), (out_dir, out_file)]:
        try:
            os.makedirs(target_dir, exist_ok=True)
            with open(target_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[LastRun] 写入 {target_file} 失败: {e}", file=sys.stderr)

    return state


def evaluate_outage(
    last_run_data: Dict[str, Any],
    now_dt: Optional[datetime] = None,
    full_threshold_hours: float = 8.0,
    fast_threshold_hours: float = 2.0,
) -> Dict[str, Any]:
    """评估是否存在断档。

    返回:
    {
      "full_age_hours": float or None,
      "fast_age_hours": float or None,
      "full_outage": bool,
      "fast_outage": bool,
      "is_outage": bool
    }
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc).astimezone(BJ_TZ)

    full_dt = parse_iso_datetime(last_run_data.get("full_at"))
    fast_dt = parse_iso_datetime(last_run_data.get("fast_at"))

    full_age = None
    if full_dt:
        full_age = max(0.0, (now_dt - full_dt).total_seconds() / 3600.0)

    fast_age = None
    if fast_dt:
        fast_age = max(0.0, (now_dt - fast_dt).total_seconds() / 3600.0)

    full_outage = full_age is None or full_age > full_threshold_hours
    fast_outage = fast_age is None or fast_age > fast_threshold_hours

    return {
        "full_age_hours": full_age,
        "fast_age_hours": fast_age,
        "full_outage": full_outage,
        "fast_outage": fast_outage,
        "is_outage": full_outage or fast_outage,
    }


class OutageAlertManager:
    """断档报警去重与恢复状态机。

    规则:
    - full_at > 8 小时 或 fast_at > 2 小时 -> 报警
    - 同一种报警 6 小时内最多发一次
    - 恢复正常后发一条"已恢复"
    """

    def __init__(self, repeat_suppression_hours: float = 6.0):
        self.repeat_suppression_hours = repeat_suppression_hours
        # state: {"full": {"in_outage": bool, "last_alert_ts": float}, "fast": ...}
        self.state = {
            "full": {"in_outage": False, "last_alert_ts": 0.0},
            "fast": {"in_outage": False, "last_alert_ts": 0.0},
        }

    def check(
        self,
        last_run_data: Dict[str, Any],
        now_dt: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """检查并返回需要发送的通知列表 [{type: 'alert'|'recovery', target: 'full'|'fast', text: str}]"""
        if now_dt is None:
            now_dt = datetime.now(timezone.utc).astimezone(BJ_TZ)

        now_ts = now_dt.timestamp()
        eval_res = evaluate_outage(last_run_data, now_dt=now_dt)
        notifications = []

        for target in ("full", "fast"):
            is_outage = eval_res[f"{target}_outage"]
            age = eval_res[f"{target}_age_hours"]
            threshold = 8.0 if target == "full" else 2.0
            name = "全量任务" if target == "full" else "Fast 任务"
            curr_state = self.state[target]

            if is_outage:
                curr_state["in_outage"] = True
                last_ts = curr_state.get("last_alert_ts", 0.0)
                # 首次报警或超过 6 小时抑制期
                if last_ts == 0.0 or (now_ts - last_ts) >= self.repeat_suppression_hours * 3600.0:
                    curr_state["last_alert_ts"] = now_ts
                    age_str = f"{age:.1f} 小时" if age is not None else "无记录"
                    last_time_str = last_run_data.get(f"{target}_at") or "从未运行"
                    text = (
                        f"⚠️ [Trend Monitor 断档报警] {name}已超过 {age_str} 未运行 (阈值 {threshold:.0f} 小时)\n"
                        f"上次运行时间: {last_time_str}"
                    )
                    notifications.append({
                        "type": "alert",
                        "target": target,
                        "text": text,
                    })
            else:
                # 恢复正常
                if curr_state.get("in_outage", False):
                    curr_state["in_outage"] = False
                    curr_state["last_alert_ts"] = 0.0
                    last_time_str = last_run_data.get(f"{target}_at") or "刚刚"
                    text = (
                        f"✅ [Trend Monitor 已恢复] {name}已恢复正常运行\n"
                        f"恢复运行时间: {last_time_str}"
                    )
                    notifications.append({
                        "type": "recovery",
                        "target": target,
                        "text": text,
                    })

        return notifications
