# Trend Monitor (搜索新词监控)

监控突发搜索新词与社区高频短语的自动化工具，旨在第一时间发现刚冒头的新趋势、新工具与热点事件。

---

## 1. 功能简介

1. **Google Trends（主数据源）**：
   - 抓取美国区（US）Daily Trending Searches，获取 24 小时内热度飙升词与关联新闻。
   - 调用 `interest_over_time` 查询候选词过去 7 天热度曲线，比对基线热度与 24 小时峰值，确认是“突然冒头”的新词，排除“长期高热度词”。
   - 遇到 Google 接口限流（429）时自动降级依托 24h 榜单数据与官方 RSS 源。

2. **Reddit rising（辅助数据源）**：
   - 抓取 Reddit `r/all/rising` 社区上升帖标题。
   - 提取 2~4 词的高频短语并按得分聚合。

3. **智能过滤与意图分类**：
   - **人名与八卦过滤**：使用 `PERSON_PATTERN` 正则结合 500+ 常见英文姓名库（名+姓结构）与上下文职务称谓（Coach, Senator, QB, Actor 等），直接过滤纯人名资讯与娱乐八卦（如大学橄榄球教练、政客等）。若事件中包含工具/产品/教程信号则豁免。
   - **短语质量清洗**：通过 `USELESS_PHRASES` 黑名单与口语正则，彻底过滤纯寒暄与无意义短语（如 `good morning`, `really good`, `thank you`, `anyone else` 等）。
   - **严格收紧意图判定 (`intent`)**：
     - `tool`：仅在词本身或关联文本中包含明确工具信号词（如 `tool`, `converter`, `compressor`, `calculator`, `generator`, `download`, `template`, `app`, `software`, `online free` 等）时标记；绝不把人名标为 tool。
     - `info`：包含明确资讯、赛况、发布、规格等信号词时标记。
     - `other`：未匹配上述强意图的其他词汇。

4. **自动化输出**：
   - 结果保存至 `~/workspace/trend-monitor/outputs/YYYY-MM-DD-HH.json`（按北京时间 UTC+8 命名）。

---

## 2. 依赖安装

环境要求：Python 3.8+

```bash
pip install pytrends requests pandas
```

---

## 3. 运行方式

直接运行监控脚本：

```bash
python3 ~/workspace/trend-monitor/trend_watch.py
```

执行后终端将输出抓取日志、过滤日志及生成的 JSON 结果。

---

## 4. 输出格式说明

输出文件保存在 `outputs/` 目录下，文件名为 `YYYY-MM-DD-HH.json`。格式为 JSON 数组，每个元素包含以下字段：

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `term` | string | 发现的候选新词 / 趋势短语 |
| `source` | string | 数据源（`google_trends` 或 `reddit`） |
| `heat` | string / int | 热度指标（Google 为近似搜索量如 `500+`、`1000+`；Reddit 为综合频次打分） |
| `intent` | string | 搜索意图分类：`tool`（工具型）、`info`（资讯型）、`other`（其他） |
| `commercial_score` | int | 商业价值分（0-10）：基于高价值信号词、意图加权（工具型+3/资讯型+1）及八卦人名惩罚综合计算 |
| `event_type` | string / null | 事件类型标记：`product_launch`（新品/发布）、`policy`（政策/新规）、`gossip`（八卦/绯闻）或 `null` |
| `note` | string | 飙升原因推测、7天基线与峰值对比、相关新闻事件或 Reddit 原帖标题 |

### 输出排序规则

JSON 数组写入前按 `(event_type 优先级, commercial_score, heat)` 综合排序：
1. **事件类型优先级（升序）**：`product_launch` (0) > `policy` (1) > `gossip` (2) > `null` (3)。
2. **商业价值分（降序）**：`commercial_score` 高分优先。
3. **热度数值（降序）**：解析 `heat` 数字部分转为整型（如 `'500+'` → `500`，`'N/A'` → `0`）后降序排列。

### 已处理词去重机制

- **历史词库文件**：`~/workspace/trend-monitor/outputs/seen_terms.json`。
- **存储结构**：`{term_lower: {"first_seen": "ISO8601时间", "count": 推送次数}}`。
- **去重逻辑**：
  - 启动时加载 `seen_terms.json`。
  - 遍历抓取候选词，若小写词已存在于历史库中，则记录日志 `[去重] 跳过已推送词: xxx` 并从本次输出中排除。
  - 未推送过的新词加入本次输出结果，并在历史库中记录北京时间（UTC+8）ISO8601 时间戳及更新累计出现次数，监控流程结束后统一保存回磁盘。

### 示例输出：

```json
[
  {
    "term": "pdf converter online free",
    "source": "google_trends",
    "heat": "200+",
    "intent": "tool",
    "commercial_score": 6,
    "event_type": "product_launch",
    "note": "24小时内搜索量大幅异动；相关事件: New PDF tool launch"
  },
  {
    "term": "georgia tech football",
    "source": "google_trends",
    "heat": "500+",
    "intent": "info",
    "commercial_score": 1,
    "event_type": null,
    "note": "7天基线热度: 12.3, 24h峰值: 100 (确认突然冒头)；相关事件: Georgia Tech secures upset win in college football"
  }
]
```

---

## 5. 定时运行建议

建议通过 `cron` 每天定时运行 4 次（每 6 小时一次），以持续捕获全天不同时段的突发热词。

编辑 crontab：

```bash
crontab -e
```

添加以下定时任务示例（每天 02:00, 08:00, 14:00, 20:00 运行）：

```cron
0 2,8,14,20 * * * /usr/bin/python3 /home/hatch/workspace/trend-monitor/trend_watch.py >> /home/hatch/workspace/trend-monitor/trend_watch.log 2>&1
```

---

## 6. 已知限制

1. **X (Twitter) 趋势**：
   - X 官方当前无免费公开的趋势 API，为避免硬爬带来的 IP 风控与不稳定性，目前标记为 TODO 并跳过。
2. **人名过滤为启发式规则**：
   - 过滤基于“名+姓结构正则 + 常见姓名库 + 上下文职务称谓”，对于极生僻的外国名字且新闻标题中没有任何职务关键词的情况，可能存在边缘判断误差。
3. **Google Trends 频率限制**：
   - Google Trends `interest_over_time` 接口对高频请求会返回 429 (TooManyRequestsError)。脚本已内置 1.2 秒请求节流与 24h 热榜及 RSS 降级容错机制。
