# trend-outage-watch (断档报警 Cloudflare Worker)

独立的 Cloudflare Worker，每小时定时检查 `https://trend-dash.pages.dev/data/last_run.json`。
若全量任务超过 8 小时未运行，或 fast 任务超过 2 小时未运行，通过 Telegram 发送报警。同一种报警 6 小时内最多发一次，恢复正常后发送恢复通知。

---

## 1. 部署与配置步骤

### 前置要求
确保本机或部署机已安装 Node.js (18+) 与 Cloudflare 认证：
```bash
cd workers/outage-watch
```

### 步骤 A：配置 Telegram Secrets (必填)
用户需自行执行以下命令写入 Telegram 凭据，**绝不写入代码或仓库文件**：
```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN
# 提示输入时粘贴你的 Telegram Bot Token

npx wrangler secret put TELEGRAM_CHAT_ID
# 提示输入时粘贴你的 Telegram Chat ID (数字，如 12345678)
```

### 步骤 B：创建 KV 命名空间（推荐持久化去重）
为了跨 Worker 冷启动精确保证 6 小时去重与恢复状态，可创建并绑定 KV：
```bash
npx wrangler kv namespace create OUTAGE_KV
```
执行后将输出的 `id` 取出，在 `workers/outage-watch/wrangler.toml` 中取消注释并填入：
```toml
[[kv_namespaces]]
binding = "OUTAGE_KV"
id = "<输出的_KV_ID>"
```
*(注：若不配置 KV，Worker 会自动降级为实例内存去重)*

### 步骤 C：部署 Worker
```bash
npx wrangler deploy
```

部署完成后，Worker 会在每小时整点 (`0 * * * *`) 自动触发 Cron 检测。你也可以通过浏览器直接访问 Worker 分配的 HTTP 路由，随时查看当前的断档与去重状态。
