#!/usr/bin/env python3
"""trend-monitor 可配置规则（trend_config.py）。

集中放置用户可调的过滤 / 判定规则，供 trend_watch.py 与 opps.py 共用。
本模块只依赖标准库，避免与 trend_watch 循环引用。

配置来源（优先级从高到低）：
  1. 同目录 config.json（可直接改，不用动代码；缺失或损坏时忽略）
  2. 下面的 DEFAULT_CONFIG 内置默认

可配置项：
  sensitive_terms                 敏感词表（子串匹配，不区分大小写）
  derivative_markers              衍生模型标记（名字含其一即 derivative）
  version_bump_date_re            版本/日期后缀正则（命中即 version_bump）
  gsc_hit_impressions_threshold  有页面的词：14 天内 GSC 展现数 >= 此值算命中
  hit_heat_growth_multiplier      无页面的词：7 天热度增长率 >= 此倍数算命中
  hit_heat_window_days            无页面的词：热度增长率窗口（天）
  quick_site_mature_brands        快站降级：已成熟品牌（子串匹配）
  quick_site_max_image_unit_price 快站降级：图片类单价高于此值（美元/张）即降级
  quick_site_open_licenses        快站降级：开放权重许可（命中即降级）
"""

import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    # 敏感词表：命中则自动 dropped（drop_reason=sensitive），不推送。
    # 用户 2026-10-27 定（含既有 GSC 敏感词 artex/exploit/渗透/破解/绕过/jailbreak）。
    "sensitive_terms": [
        "uncensored",
        "abliterated",
        "nsfw",
        "jailbreak",
        "exploit",
        "渗透",
        "破解",
        "绕过",
        "artex",
    ],
    # 衍生模型标记：名字（小写）含其一即 event_type=derivative，默认不推送。
    "derivative_markers": [
        "gguf",
        "gptq",
        "awq",
        "量化",
        "abliterated",
    ],
    # 日期后缀：-1017、-20261017、_1017 等（MMDD 或 YYYYMMDD，位于词尾或分隔符后）。
    "version_bump_date_re": r"[-_](?:(?:19|20)\d{2})?(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?=$|[-_])",
    # 命中率：有页面的词，14 天内 GSC 展现数 >= 此值算命中（与 GSC_MIN_IMPRESSIONS=20 同口径）。
    "gsc_hit_impressions_threshold": 20,
    # 命中率：无页面的词，7 天热度增长率倍数（最新热度 >= 7 天前热度 × 此值算命中）。
    "hit_heat_growth_multiplier": 2.0,
    "hit_heat_window_days": 7,
    # 快站降级：品牌已成熟（子串匹配，小写），付费站差异化不足。
    "quick_site_mature_brands": [
        "heygen",
        "kling",
    ],
    # 快站降级：图片类单价高于此值（美元/张）即降级；视频类因计价单位不同暂不做价格降级。
    "quick_site_max_image_unit_price": 0.05,
    # 快站降级：已开放权重的许可（命中即降级，用户可直接用原厂/社区部署）。
    "quick_site_open_licenses": [
        "apache-2.0",
        "apache 2.0",
        "mit",
        "bsd-2-clause",
        "bsd-3-clause",
    ],
}

# 模型族 key 生成时丢弃的通用 token：任务词、尺寸词、格式词、通用类别词。
# 注意：丢弃 "image"/"video" 是为了让 flux-3-image-klein 与 FLUX 3 Klein 归为同一族；
# 尺寸 token（5b/14b/33b 等）丢弃是为了让 Eden Live 5B/14B 归为同一族。
_FAMILY_DROP_TOKENS = {
    "edit", "editing", "text", "to", "image", "video", "audio", "speech", "voice",
    "base", "distilled", "distill", "lora", "comfy", "comfyui",
    "gguf", "gptq", "awq", "fp8", "fp16", "bf16", "int4", "int8", "int2",
    "diffusers", "vae", "instruct", "chat", "preview", "dev", "schnell",
    "safetensors", "onnx", "exl2", "bnb", "generate", "generation",
}
_SIZE_TOKEN_RE = re.compile(r"^\d+(\.\d+)?[bm]$")


def load_config(path: str = CONFIG_PATH) -> dict:
    """加载配置：内置默认 + config.json 覆盖（列表 whole 替换，标量替换）。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg["sensitive_terms"] = list(DEFAULT_CONFIG["sensitive_terms"])
    cfg["derivative_markers"] = list(DEFAULT_CONFIG["derivative_markers"])
    cfg["quick_site_mature_brands"] = list(DEFAULT_CONFIG["quick_site_mature_brands"])
    cfg["quick_site_open_licenses"] = list(DEFAULT_CONFIG["quick_site_open_licenses"])
    try:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                override = json.load(f)
            if isinstance(override, dict):
                for k, v in override.items():
                    if k in cfg:
                        cfg[k] = v
    except Exception:
        pass
    return cfg


# 模块级默认配置（测试可 monkeypatch trend_config.CONFIG）。
CONFIG = load_config()


def is_sensitive(term: str, terms=None) -> tuple:
    """敏感词检查：返回 (是否命中, 命中的词)。子串匹配，不区分大小写。"""
    if terms is None:
        terms = CONFIG.get("sensitive_terms") or []
    low = (term or "").strip().lower()
    if not low:
        return False, ""
    for s in terms:
        if s and s.lower() in low:
            return True, s
    return False, ""


def model_family_key(name: str) -> str:
    """模型族 canonical key：小写、去组织前缀、去日期后缀、去通用/尺寸 token。
    例如：
      'flux-3-image-klein-comfy' -> 'flux-3-klein'
      'FLUX 3 Klein Base Text to Image' -> 'flux-3-klein'
      'blackforestlabs/flux-3/klein/text-to-image/lora' -> 'flux-3-klein'
      'Eden Live 5B Video' / 'Eden Live 14B Video' -> 'eden-live'
      'Kling 4.0 Video Editing' -> 'kling-4.0'
    """
    if not name:
        return ""
    s = name.split("/")[-1].strip().lower()
    # 去日期后缀（复用与 trend_watch._DATE_SUFFIX_RE 同口径的规则）
    date_re = CONFIG.get("version_bump_date_re") or DEFAULT_CONFIG["version_bump_date_re"]
    try:
        s = re.sub(date_re, "", s, flags=re.I)
    except re.error:
        pass
    tokens = [t for t in re.split(r"[\s\-_]+", s) if t]
    kept = []
    for t in tokens:
        if t in _FAMILY_DROP_TOKENS:
            continue
        if _SIZE_TOKEN_RE.match(t):
            continue
        kept.append(t)
    return "-".join(kept)


def classify_derivative(term: str, context: str = "") -> str | None:
    """衍生/版本升级判定，返回 'derivative' | 'version_bump' | None。
    derivative：名字含 GGUF/GPTQ/AWQ/量化/-abliterated 等衍生标记。
    version_bump：名字含日期后缀（如 -1017、-20261017）。
    （同架构小版本升级的跨记录比对见 opps.merge 的 family 分组，不在这里做。）
    """
    text = f"{term or ''} {context or ''}".strip().lower()
    if not text:
        return None
    for m in CONFIG.get("derivative_markers") or []:
        if m and m.lower() in text:
            return "derivative"
    date_re = CONFIG.get("version_bump_date_re") or DEFAULT_CONFIG["version_bump_date_re"]
    try:
        if re.search(date_re, (term or "").strip(), flags=re.I):
            return "version_bump"
    except re.error:
        pass
    return None


def normalize_heat(value) -> tuple:
    """热度归一化：返回 (heat_or_None, heat_source)。
    没有热度就返回 (None, 'none')，绝不用写死数字填充。
    """
    if value is None:
        return None, "none"
    if isinstance(value, (int, float)):
        return int(value), "reported"
    s = str(value).strip().replace(",", "")
    m = re.search(r"\d+", s)
    if m:
        try:
            return int(m.group(0)), "reported"
        except ValueError:
            return None, "none"
    return None, "none"


def quick_site_downgrade(item: dict) -> tuple:
    """快站降级检查（判成 quick_paid_site 之前调用）。
    满足任一条即降级为 content，返回 (True, 降级原因)；否则 (False, '')。
    只用 item 自带字段判定，字段缺失时跳过该条（不臆测）。
    """
    term = (item.get("term") or "").lower()
    org = (item.get("platform") or "").lower()
    hay = f"{term} {org}"
    for b in CONFIG.get("quick_site_mature_brands") or []:
        if b and b.lower() in hay:
            return True, f"品牌已成熟（{b}），付费站差异化不足"
    if item.get("official_free_url") or item.get("free_demo") or item.get("playground_url"):
        return True, "官方已有免费在线入口"
    if item.get("open_weights") is True:
        return True, "已开放权重，用户可直接用原厂/社区部署"
    lic = (item.get("license") or "").strip().lower()
    if lic:
        for ol in CONFIG.get("quick_site_open_licenses") or []:
            if ol.lower() == lic:
                return True, f"已开放权重（许可 {item.get('license')}）"
    price = item.get("unit_price")
    if isinstance(price, (int, float)):
        cat = (item.get("pipeline_tag") or "").lower()
        if "image" in cat and price > (CONFIG.get("quick_site_max_image_unit_price") or 0.05):
            return True, f"单价过高（${price}/张 > ${CONFIG.get('quick_site_max_image_unit_price')}），积分制付费站难成立"
    return False, ""
