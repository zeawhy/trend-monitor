#!/usr/bin/env python3
"""更新控制台数据并部署到 Cloudflare Pages（供 cron 调用）。"""
import sys, os, subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, '/opt/hatch/skills/skill-creator/bin')
from dynamic_credentials import dynamic_credential_entry

r = subprocess.run([sys.executable, os.path.join(BASE, 'update_dashboard.py')],
                   capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    print(r.stderr); sys.exit(1)

e = dynamic_credential_entry('custom.cloudflare', 'access_token')
env = dict(os.environ, CLOUDFLARE_API_TOKEN=e['surrogate'])
r = subprocess.run(['npx', 'wrangler@latest', 'pages', 'deploy', BASE,
                    '--project-name=trend-dash', '--branch=main', '--commit-dirty=true'],
                   capture_output=True, text=True, env=env, timeout=180)
print(r.stdout[-800:])
if r.returncode != 0:
    print(r.stderr[-500:]); sys.exit(1)
print('dashboard deployed: https://trend-dash.pages.dev/')
