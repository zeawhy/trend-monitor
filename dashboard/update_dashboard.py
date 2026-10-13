#!/usr/bin/env python3
"""把 trend-monitor 输出转成控制台 data/*.json，供 dashboard/index.html 读取。"""
import json, os, glob

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, '..', 'outputs')
HID = os.path.join(BASE, '..', 'hidden_files')
DATA = os.path.join(BASE, 'data')
os.makedirs(DATA, exist_ok=True)

def load(p, default):
    try:
        with open(p) as f: return json.load(f)
    except Exception:
        return default

# 最新一轮
last_seen = load(os.path.join(OUT, 'last_seen.json'), {})
latest_path = last_seen.get('last_output', '')
latest = load(latest_path, []) if latest_path else []
with open(os.path.join(DATA, 'latest.json'), 'w') as f:
    json.dump(latest, f, ensure_ascii=False)

# 命中率报告 -> 控制台格式
hr = load(os.path.join(OUT, 'hit_rate_report.json'), {})
ov = hr.get('overall', {})
rate_s = ov.get('hit_rate', '0%')
try: rate = float(rate_s.strip('%')) / 100
except Exception: rate = 0.0
by_source = []
for src, s in (hr.get('sources') or {}).items():
    t, h = s.get('total', 0), s.get('hits', 0)
    by_source.append({'source': src, 'total': t, 'hits': h, 'rate': (h / t if t else 0)})
with open(os.path.join(DATA, 'hit_rate.json'), 'w') as f:
    json.dump({
        'total_tracked': ov.get('total_tracked', 0),
        'verified': ov.get('total_evaluated', 0),
        'overall_rate': rate,
        'by_source': by_source,
    }, f, ensure_ascii=False)

# 追踪中
tracking = load(os.path.join(HID, 'word_tracking.json'), [])
with open(os.path.join(DATA, 'tracking.json'), 'w') as f:
    json.dump(tracking, f, ensure_ascii=False)

# meta
with open(os.path.join(DATA, 'meta.json'), 'w') as f:
    json.dump({'updated_at': last_seen.get('updated_at', hr.get('generated_at', ''))}, f, ensure_ascii=False)

print('dashboard data updated:', latest_path)
