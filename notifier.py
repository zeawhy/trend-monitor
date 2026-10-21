#!/usr/bin/env python3
"""Telegram 推送通道模块

读取环境变量:
- TELEGRAM_BOT_TOKEN: Telegram Bot Token (未配置时静默跳过)
- TELEGRAM_CHAT_ID: Telegram 接收会话 ID (未配置时静默跳过)
- TELEGRAM_ENABLED: 推送开关, 默认开启 (设为 "0" / "false" / "no" 时禁用)

特性:
- 没配置凭据时静默跳过, 绝不阻塞主流程
- 异常时仅记日志到 stderr, 不抛出异常
- 支持消息自动分片 (Telegram 限制 4096 字符)
- 提供新词发现、高频监控、每日日报和系统告警的统一格式化推送
"""

import html
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def is_telegram_enabled() -> bool:
    """检查 Telegram 推送开关是否开启 (默认开启)"""
    val = os.environ.get("TELEGRAM_ENABLED", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def get_telegram_creds() -> Tuple[Optional[str], Optional[str]]:
    """获取 Telegram 凭据 (token, chat_id)。若任意一个未配置则返回 (None, None)"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None, None
    return token, chat_id


def send_telegram_message(
    text: str, parse_mode: Optional[str] = "HTML", timeout: int = 10
) -> bool:
    """发送消息到 Telegram。

    - 若未启用或未配置凭据，静默跳过并返回 False。
    - 异常时不抛出异常，记录 stderr 并返回 False。
    - 消息超长时自动按 4000 字符切片分批发送。
    """
    if not is_telegram_enabled():
        return False

    token, chat_id = get_telegram_creds()
    if not token or not chat_id:
        return False

    if not text or not text.strip():
        return False

    max_len = 4000
    chunks = [text[i : i + max_len] for i in range(0, len(text), max_len)]
    all_success = True

    for chunk in chunks:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    all_success = False
        except Exception as e:
            # 如果是由于 HTML 解析错误引起的失败，降级为无 parse_mode 纯文本重试一次
            if parse_mode:
                try:
                    payload.pop("parse_mode", None)
                    req_plain = urllib.request.Request(
                        url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req_plain, timeout=timeout) as resp_plain:
                        if resp_plain.status == 200:
                            continue
                except Exception:
                    pass
            print(f"[Notifier] Telegram 推送失败(已忽略): {e}", file=sys.stderr)
            all_success = False

    return all_success


def notify_new_terms(
    items: List[Dict[str, Any]], is_fast: bool = False
) -> bool:
    """推送新词 / 新模型发现通知。

    - items 为空时不推送并返回 False。
    - is_fast=True 时按高频监控精简格式输出。
    - is_fast=False 时按全量监控格式（优先展示 quick_paid_site）输出。
    """
    if not items:
        return False

    if is_fast:
        lines = [
            f"🔥 <b>[Fast官方新模型]</b> 发现 {len(items)} 个新模型",
            "",
        ]
        for m in items:
            platform = html.escape(str(m.get("platform") or m.get("source") or "官方平台"))
            term = html.escape(str(m.get("term") or ""))
            link = m.get("source_url") or ""
            unit_price = m.get("unit_price")
            if not unit_price:
                note = m.get("note", "")
                if "单价:" in note:
                    unit_price = note.split("单价:")[1].split("|")[0].strip()
                else:
                    unit_price = "见模型页"
            unit_price_esc = html.escape(str(unit_price))

            if link:
                lines.append(
                    f"• <b>{term}</b> ({platform})\n  单价: {unit_price_esc} | <a href=\"{html.escape(link)}\">模型链接</a>"
                )
            else:
                lines.append(f"• <b>{term}</b> ({platform})\n  单价: {unit_price_esc}")
        return send_telegram_message("\n".join(lines))

    # 全量监控格式
    lines = [
        f"🚀 <b>[Trend Monitor]</b> 发现 {len(items)} 个新候选词/机会",
        "",
    ]

    quick = [it for it in items if it.get("monetization_path") == "quick_paid_site"]
    others = [it for it in items if it.get("monetization_path") != "quick_paid_site"]

    if quick:
        lines.append("⚡ <b>quick_paid_site（抢 48 小时窗口积分站）</b>:")
        for it in quick:
            t = html.escape(str(it.get("term") or ""))
            p = html.escape(str(it.get("unit_price") or "需人工确认"))
            hrs = it.get("hours_since_release")
            hrs_disp = f"距今 {hrs}h" if hrs is not None else "时间未知"
            url = it.get("source_url") or ""
            link_str = f' | <a href="{html.escape(url)}">主页</a>' if url else ""
            lines.append(f"  • <b>{t}</b> | 单价: {p} | {hrs_disp}{link_str}")
        lines.append("")

    if others:
        lines.append(f"📋 <b>其他候选 ({len(others)} 个)</b>:")
        for it in others[:15]:  # 最多列出 15 个避免超长
            t = html.escape(str(it.get("term") or ""))
            path = html.escape(str(it.get("monetization_path") or "watch"))
            src = html.escape(str(it.get("source") or "unknown"))
            heat = it.get("heat")
            heat_str = f"heat={heat}" if heat is not None else "heat=null"
            lines.append(f"  • <b>{t}</b> [{path}] ({src}, {heat_str})")
        if len(others) > 15:
            lines.append(f"  <i>... 以及其他 {len(others) - 15} 个</i>")

    return send_telegram_message("\n".join(lines))


def notify_digest(digest_text: str) -> bool:
    """推送每日机会与复盘日报。内容为空时静默跳过。"""
    if not digest_text or not digest_text.strip():
        return False

    # digest_text 默认是 Markdown，按纯文本发送避免复杂的 HTML 转义冲突
    return send_telegram_message(digest_text.strip(), parse_mode=None)


def notify_alert(title: str, message: str) -> bool:
    """推送监控报警信息。"""
    if not message or not message.strip():
        return False
    body = (
        f"⚠️ <b>[Trend Monitor 告警] {html.escape(title)}</b>\n\n"
        f"{html.escape(message.strip())}"
    )
    return send_telegram_message(body)
