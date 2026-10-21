# Trend Monitor (搜索新词监控)

监控刚冒头的新产品 / 新技术 / 新模型，自动完成「发现 → 过滤 → Jev 打分 → 变现分流 → 排序输出」的自动化工具，并追踪每个词的推送状态、命中率与已建页面的收益/止损。

---

## 1. 功能简介

### 发现源（按优先级）

1. **GitHub breakout**：抓取创建不足 14 天、日均涨星 200+ 或总星 400+ 的新仓库，仓库名即产品/技术新词；同一 org 一次推出或近 30 天已推送 ≥3 个仓库时合并为套件级候选，更新成员列表与热度，注明"同组织系列，疑似批量发布"。
2. **HuggingFace 新模型**：只用 `sort=trendingScore` 拉候选，只保留 `created_at` 在 7 天内的模型。对模型名做标准化剥离日期与格式后缀（`-dev`/`-comfy`/`-gguf`/`-fp8` 等），同一组织同族模型合并为一个条目（新增 `variants` 字段）；标准化后若为已知产品或在历史状态库中超过 7 天，标 `event_type=version_update` 并默认走 `watch` 观察；Comfy-Org 等转载账号优先从模型卡解析 `base_model` 去重。媒体生成类 pipeline + API 可用 + 许可证未禁商用 → 标记 `event_type=new_model`。
3. **API 平台与官方模型发布 (fal.ai / Replicate / OpenRouter / BFL)**：抓取 fal.ai、Replicate、OpenRouter 模型列表、Black Forest Labs (BFL) 官方博客/公告最近 7 天新上架的视频、图片、语音模型，先按模型族路径归组合并（例如 `blackforestlabs/flux-3-max` 去除端点名），term 输出模型族名并带 `variants` 各端点详情，优先抢 48 小时积分付费站窗口。拿不到真实热度时 `heat` 设为 `null`，带 `heat_source="none"`。每轮最多 5 条，单个来源失败静默跳过并记日志。
4. **Reddit rising**：抓取 `r/all/rising` 与技术区上升帖标题，提取 2~4 词高频短语。
5. **GSC first-impression**：各站点近 14 天第一次有展现的 query（真实搜索需求，长尾选题）。
6. **X (Twitter)**：Nitter 实例 / 社交搜索降级抓取，失败静默跳过。

Google Trends **只做验证**（14 天命中率核验），不参与发现；核验时丢弃最近 2 天未结算数据。

### 过滤与分类

- 人名/八卦过滤（名+姓结构 + 500+ 姓名库 + 职务称谓上下文，含工具/产品信号时豁免）、寒暄黑名单短语过滤。
- `intent` 意图判定（收紧规则）：`tool`（有明确工具信号词）、`info`（资讯信号词）、`other`。
- `commercial_score` 商业价值分（0-10）；`event_type` 事件类型（new_model / product_launch / policy / gossip）。

### Jev 打分层

用 TypeSafe Jev 结构化决策模型给 top 候选打三维分：`is_product_tech`（0-1）、`intent`（paid_tool / tech_term / company / media_generation / generic）、`newsworthiness`（0-1，原始分除以 legend 最大键归一化，换算前原始值保存为 `newsworthiness_raw`）。

- 打分前先按 `get_sort_key` 排序、再按来源分配名额（每来源至少 3 个，总数上限 `JEV_MAX_SCORE_PER_RUN=15`），避免靠后的来源整批拿不到分。
- 上下文不只传 term：拼接来源、GitHub 仓库简介、HF 的 pipeline_tag/license、note（Reddit 原帖标题）、URL，截断 1500 字符。
- **缓存仅用于记录**：打分结果存入 seen_terms 条目（`jev` / `jev_scored_at` / `jev_model`）仅作为历史追踪与审计记录。由于进入 Jev 打分阶段的候选词必须先经过 `should_notify` 筛选（具备首发、热度跃升、新来源或重大版本等新证据才通知），处于冷却期（cooldown）或无新证据的词此前已被跳过，因此常规运行不会触发 cooldown/repeat 缓存复用分支。
- 过滤：有有效评分且 `is_product_tech < JEV_PRODUCT_TECH_THRESHOLD`（0.6）的词丢弃；未打分的词照常放行，但带 `jev_status=unscored`。每条结果都标 `jev_status`（scored / cached / unscored）。
- 健康监控与告警：每轮统计 `jev_stats`（含 `succeeded_by_model` 与 `fallback_count`）打印在汇报里，并落 `outputs/jev_health.json`（最近 5 轮）。
  - 当连续 2 轮 succeeded=0 时汇报告警「Jev 打分失效，当前过滤为全部放行」。
  - 当 `JEV_PROVIDER=openrouter` 且环境配了 `OPENROUTER_API_KEY`，但 `typesafe/jev-1.14` 为 0 且全部靠 fallback 打分时，输出一行告警：`OpenRouter 不可用（余额/key/接口），当前走免费通道`。

### 变现分流（classify_monetization）

Jev 打分之后、排序之前，每个候选必须落到一条路线，结果写 `monetization_path` / `path_reason`，规则按顺序判断：

| 顺序 | 条件 | 路线 | 后续动作 |
| :--- | :--- | :--- | :--- |
| 0 | `event_type=version_update`（老产品版本更新/量化/衍生格式） | `watch` | 默认先观察 |
| 1 | `event_type=new_model` 或媒体生成模型，且 `api_available` | `quick_paid_site` | 抢 48 小时窗口：积分制付费站 |
| 2 | `intent=tool`，或 Jev intent ∈ {paid_tool, media_generation} | `tool_page` | 并入 yuliusbox 工具页 |
| 3 | `intent=info`，或 Jev intent = tech_term | `content` | 写 yuliuswiki 文章 |
| 4 | Jev intent = generic 且 newsworthiness < 0.4 | `reject` | 不输出，记入 last_filtered_reason |
| 5 | 其余 | `watch` | 先观察 |

汇报与看板里 quick_paid_site 单独列在最前，额外给出平台单价（拿不到标 null 且提示需人工确认）、许可证/服务条款链接、发布时间与距今小时数（判断 48 小时窗口）、Google 竞品检查链接（`<模型名> free online`）。

### 自动化输出

结果保存至 `outputs/YYYY-MM-DD-HH.json`（北京时间命名）。运行结束打印各来源条数、各路线条数与 jev_stats。

---

## 2. 依赖安装

环境要求：Python 3.8+（`huggingface_hub` 用于 HuggingFace 来源；api_available 依赖其 `inferenceProviderMapping` 展开，1.33.0 已实测支持，不支持时自动降级为 null）。

```bash
pip install pytrends requests pandas huggingface_hub
```

---

## 3. 运行方式

```bash
python3 trend_watch.py            # 全量运行(每 6 小时): 抓取全部发现源、Jev 打分、变现分流、写输出与更新状态
python3 trend_watch.py --dry-run  # 全量 dry-run: 只写输出 JSON, 不更新任何状态文件
python3 trend_watch.py --fast     # 高频检查(每 30 分钟): 只查官方平台(fal/Replicate/OpenRouter/BFL), 仅 diff+去重, 发现新模型立即打 stdout
python3 trend_watch.py --fast --dry-run  # fast dry-run: 发现新模型打印 stdout, 但不写 seen_terms / opportunities
python3 trend_watch.py --digest   # 每日日报(每天 9:00): 昨日新机会 Top5、待决定数、>48h未处理清单、今日复盘页面、路线命中率(无内容静默退出)
```

> [!NOTE]
> 脚本内置跨进程**并发锁**（文件锁 `hidden_files/.trend_watch.lock`），自动避免 `--fast` 高频任务与 6 小时全量任务或 CLI 命令同时写状态文件。

### 环境变量（Jev 与 API 平台配置）

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `JEV_PROVIDER` | `openrouter` | 打分通道：`openrouter` / `opencode_free` / `none` |
| `OPENROUTER_API_KEY` | 无 | OpenRouter 密钥，**只从环境变量读取**，不写进代码、日志或输出 JSON（仓库 `.gitignore` 已覆盖 `.env`） |
| `FAL_KEY` | 无 | fal.ai 密钥，**只从环境变量读取**；未配置时使用 fal.ai 公开免费搜索接口 |
| `REPLICATE_API_TOKEN` | 无 | Replicate API 密钥，**只从环境变量读取**；未配置时静默跳过 Replicate |

- `openrouter`：走 `POST https://openrouter.ai/api/alpha/decisions`，模型 `typesafe/jev-1.14`；**没有 key 或调用失败时自动回退 `opencode_free`**（OpenCode Zen 免费通道，模型 `jev-1.13-free`）；两者都失败时该条视为未打分（放行 + `jev_status=unscored`）。
- `none`：完全不打分。

---

## 4. 输出格式说明

输出文件为 JSON 数组（控制台 dashboard 依赖顶层数组结构），每个元素字段：

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `term` | string | 候选新词 / 模型族名 / 仓库名 |
| `source` | string | 数据源：`github_breakout` / `huggingface` / `api_platform` / `reddit` / `gsc_first_impression` / `x` |
| `heat` | int / null | 热度数值（GitHub 为日均星数、HF 为 likes 与 downloads 综合、API 平台为使用量，拿不到真实热度时为 `null` 且看板显示"—"） |
| `heat_source` | string | 热度计算来源：`huggingface` / `stars_per_day` / `reddit_score` / `x_posts` / `gsc_impressions` / `run_count` / `none` |
| `intent` | string | 本地意图：`tool` / `info` / `other` |
| `commercial_score` | int | 商业价值分（0-10） |
| `event_type` | string / null | `new_model`（-1）/ `product_launch`（0）/ `version_update`（1）/ `policy`（2）/ `gossip`（3）/ null（4），括号内为排序优先级 |
| `note` | string | 证据说明（平台/仓库星数、单价、模型 pipeline/license、原帖标题等） |
| `source_url` | string | 来源链接 / 模型主页 |
| `description` | string | （GitHub/API平台）模型与仓库简介，供 Jev 上下文与人工判断 |
| `variants` | array | （API平台 / HF）同一模型族的各端点/变体列表（包含 endpoint/name, category, url 等） |
| `jev` | object / 缺省 | Jev 打分：`{is_product_tech, intent, intent_confidence, newsworthiness, newsworthiness_raw}`，未打分时无此字段 |
| `jev_status` | string | `scored` / `cached`（复用缓存）/ `unscored`（未打分） |
| `monetization_path` | string | 变现路线：`quick_paid_site` / `tool_page` / `content` / `watch` |
| `path_reason` | string | 路线判定理由 |
| `unit_price` | string / null | （quick_paid_site）平台单价，无法获取时为 `null`（提示需人工确认） |
| `license_url` | string | （quick_paid_site）许可证 / 服务条款链接 |
| `published_at` | string | （quick_paid_site）模型平台上架时间（ISO格式） |
| `hours_since_release` | float / null | （quick_paid_site）距平台上架已过小时数（判断是否在 48 小时窗口内） |
| `competitor_search_url` | string | （quick_paid_site）Google 竞品搜索链接（`<模型名> free online`） |
| `hf_model_id` / `pipeline_tag` / `license` / `api_available` | | （HuggingFace / API平台）模型标识、任务类型、许可证、API 是否可用 |

### 输出排序规则

1. **变现路线优先级**：`quick_paid_site` (0) > `tool_page` (1) > `content` (2) > `watch` (3)。
2. 路线内按 `get_sort_key`：热度有效性（`heat=null` 排在同路线最后，不参与与其他来源比较）→ `event_type` 优先级升序 → `commercial_score` 降序 → `heat` 降序。

---

## 5. 已处理词状态管理（seen_terms v2）

- **状态库文件**：`outputs/seen_terms.json`，不再是黑名单而是状态库；同名不同产品按 `project_url` 拆成 `term`、`term#2` 独立条目。
- 每条记录：`first_seen_at` / `last_seen_at` / `seen_count` / `sources` / `project_url` / `last_heat` / `last_version`（观测字段，每轮都更新）。
- **推送落标时机**：`last_notified_at` / `last_notified_reason` 只在「Jev 过滤 + 变现分流 + 排序完成且输出 JSON 写入成功」之后，给最终输出的词写。被过滤的词写 `last_filtered_at` / `last_filtered_reason`，**不算推送**——下一轮凭新证据仍可再提醒（旧版本曾在过滤前就误标推送、吃掉 7 天冷却期，已修复）。
- **推送判定**（`should_notify`）：普通重复不推送；首次发现直接推送；重大版本（主版本号变化）/ 热度跃升（≥ 上次 2 倍）/ 新增独立来源证据，且距上次推送超过冷却期（`RENOTIFY_COOLDOWN_DAYS = 7` 天）时再提醒。
- **Jev 记录与缓存失效**：条目内 `jev` / `jev_scored_at` / `jev_model` 存最近一次打分结果（仅供记录与历史追溯）。旧版未归一化的 Jev 缓存已在加载/迁移时通过版本与 schema 校验自动失效并清除。
- `decision`（null / build / watch / reject）与 `published_url` 由人工/上层流程回填，脚本不覆盖。
- **迁移**：旧结构在 load 时自动迁移/补默认值，迁移前原文件备份为 `seen_terms.json.bak-<时间戳>`。

---

## 6. 命中率追踪（按路线区分）

- **追踪库**：`hidden_files/word_tracking.json`，记录每个输出词的 `monetization_path`、发现时热度与热度历史。
- **历史数据标记 (legacy)**：Google Trends 降为纯验证源前写入的 `google_trends` 条目及旧版短语提取产生的 HF 泛词（如 "image 2.1"、"2.1 comfyui"）标记为 `legacy=true`。原始数据予以保留，命中率核验（`check_hit_rate`）和控制台看板「14 天追踪中」默认排除 legacy 条目；控制台提供「显示旧数据」开关供随时查阅。
- 14 天后核验（`check_hit_rate`），报告落 `outputs/hit_rate_report.json` / `.md`：
  - `content` / `tool_page`：沿用「14 天 Trends 热度翻倍」标准。
  - `quick_paid_site`：**不用 Trends 判定**，报告里标注「以 page_tracking 第 7 天净收入为准」，不自动标记核验。
  - 无路线的旧记录归入 `legacy` 组按原标准统计。
- 报告含 `sources`（按来源）与 `paths`（按路线）两套分组。

---

## 7. 页面效果与收益追踪（page_tracking.py）

- **文件**：`hidden_files/page_tracking.json`。每发布一个页面记一条：选题、URL、决策理由、投入、收录状态（`deploying` → `sitemap_updated` → `index_checked` → `indexed`，只能人工/GSC 核实后推进）、GSC 快照，以及变现字段：
  - `site_type`：quick_paid_site / tool_page / content（旧记录默认 content）
  - `costs`：`[{item, amount, date}]`；`revenue`：`[{date, gross, api_cost, fees, refunds}]`（本版手动录入，不接 Stripe API）
  - `kill_by`：上线后第 7 天日期，`add` 时自动生成；`outcome`：running / killed / merged / kept；`merged_into`：并入目标 URL
- 命令：
  ```bash
  python3 page_tracking.py add --term X --url URL --reason "..." [--site-type content]
  python3 page_tracking.py update --term X [--status indexed] [--outcome kept] [--merged-into URL] [--site-type tool_page]
  python3 page_tracking.py gsc --term X --date 2026-10-27 --impressions 120 --clicks 3 --position 8.5
  python3 page_tracking.py revenue --term X --date 2026-10-27 --gross 99 --api-cost 12 --fees 3 --refunds 0
  python3 page_tracking.py cost --term X --item 域名 --amount 68 [--date 2026-10-21]
  python3 page_tracking.py pnl    # 按 site_type 汇总第 1/7/28 天净收入
  python3 page_tracking.py list   # kill_by 到期且 running 的会提示到期复核
  python3 page_tracking.py show --term X
  ```
- **pnl 口径**：净收入 = gross − api_cost − fees − refunds − 窗口内成本（上线后累计）。窗口内无收入、或从未录过成本的，显示「未知」，**不当作 0**。
- **到期复核**：`list` 里 kill_by 已到期且 outcome=running 的页面会提示：净收入覆盖不了成本就 `killed`；搜索仍持续的就 `merged` 到 yuliusbox（用 update --outcome merged --merged-into 落标）。

---

## 8. 机会看板与状态管理 (opportunities.json & opps.py)

- **状态文件**：`outputs/opportunities.json`（JSON 数组），统一承接所有来源新发现的机会词，支持变现路线、人工决策、上线状态与止损复盘全生命周期跟踪。
- **字段表**：

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `term` | string | 候选词 / 模型名 / 项目名 |
| `source` | string | 发现来源 (`api_platform` / `huggingface` / `github_breakout` / `reddit` / `gsc_first_impression` / `x`) |
| `discovered_at` | string | 首次发现时间（ISO 8601，北京时间） |
| `score` | number | 商业价值分或热度分 |
| `suggested_route`| string | 建议路线：`wiki_article` / `box_tool` / `quick_paid_site` / `ignore` |
| `decision` | string/null | 决策：`build` / `watch` / `drop` 等 |
| `status` | string | 状态流转：`new` → `evaluating` → `decided` → `building` → `live` → `review` → `dropped` |
| `outcome` | string/null | 结局：`running` / `merged` / `killed` |
| `page_url` | string | 已上线页面 URL（与 page_tracking 关联） |
| `deadline` | string/null | 决策截止日（默认发现后 48 小时）或上线后复盘截止日（kill_by） |
| `notes` | string | 备注 / 证据补充 |
| `drop_reason` | string/null | 放弃原因 |
| `updated_at` | string | 最后更新时间（ISO 8601） |

- **自动写入与关联**：
  - 全量运行与 `--fast` 高频运行发现的新词，自动录入 `outputs/opportunities.json`（`status=new`，48小时决策 deadline）。
  - 已上线的词与 `hidden_files/page_tracking.json` 关联：`page_url` 或 `term` 对上时，自动同步 `status`（`live` / `review`）、`outcome`（`running` / `merged` / `killed`）与 `deadline`（`kill_by`）。
- **命令行工具 (CLI)**：
  ```bash
  # 1. 查看机会看板与筛选
  python3 opps.py list                                      # 查看全部
  python3 opps.py list --status new,evaluating              # 按状态筛选
  python3 opps.py list --view pending                       # 内置视图: 待决定
  python3 opps.py list --view overdue                       # 内置视图: 超过 48 小时未处理
  python3 opps.py list --view review                        # 内置视图: 已上线待复盘

  # 2. 修改机会状态
  python3 opps.py set "wan2-tool" --decision build --status building --note "开始立项制作积分站"
  python3 opps.py set "wan2-tool" --url "https://yuliusbox.com/wan2"  # 上线自动设 status=live, outcome=running
  python3 opps.py set "solidcraft" --drop-reason "竞品过多且热度下降"    # 自动标记 status=dropped

  # 3. 与 page_tracking 同步
  python3 opps.py sync

  # 也支持作为 trend_watch.py 子命令调用:
  python3 trend_watch.py opps list --view pending
  ```
- **Web 控制台机会看板 (trend-dash.pages.dev/opportunities.html)**：
  - 表格按状态筛选，内置视图一键切换：`待决定`、`超过48小时未处理`、`已上线待复盘`。
  - 看板**只读**，修改状态走 CLI，控制台通过 `dashboard/deploy_dashboard.py` 自动化部署发布。

---

## 9. 每日机会与复盘日报 (--digest)

- **命令**：
  ```bash
  python3 trend_watch.py --digest
  # 或独立执行:
  python3 digest.py
  ```
- **日报内容**：
  1. 昨日新机会（Top 5，按商业分/热度降序）
  2. 待决定概况（当前待决定数量、超过 48 小时未处理清单）
  3. 今日到复盘日（kill_by）的页面清单与处置建议
  4. 各变现路线命中率（quick_paid_site / tool_page / content / legacy）
- **静默规则**：若昨日无新机会且无待决定、无超时未处理、无今日复盘页面，**输出为空并静默退出**，避免推送空日报干扰。

---

## 10. 定时任务配置建议 (Crontab)

> [!IMPORTANT]
> 定时任务由调度方配置，代码库不擅自修改系统 crontab。以下为建议的 crontab 配置行：

```cron
# 1. 高频官方模型平台检查 (--fast 每 30 分钟一次，仅 diff+去重，有新模型时打印 stdout 可管道转发推送)
*/30 * * * * cd ~/workspace/trend-monitor && python3 trend_watch.py --fast >> ~/workspace/trend-monitor/logs/cron_fast.log 2>&1

# 2. 全量发现、Jev 打分与变现分流 (保持每 6 小时一次)
0 */6 * * * cd ~/workspace/trend-monitor && python3 trend_watch.py >> ~/workspace/trend-monitor/logs/cron.log 2>&1

# 3. 每日机会与复盘日报 (每天早 9:00 Asia/Shanghai 运行，无内容时静默退出不推送)
0 9 * * * cd ~/workspace/trend-monitor && python3 trend_watch.py --digest >> ~/workspace/trend-monitor/logs/cron_digest.log 2>&1
```

---

## 11. 测试

```bash
python3 -m unittest discover -s tests
```

覆盖：推送状态延后写入、Jev 解析/通道回退/名额分配/缓存/健康告警、HuggingFace 新模型规则、变现分流每条规则、page_tracking 迁移与 pnl、命中率路线分组、seen/page 旧结构迁移、跨进程并发文件锁、官方模型平台抓取与 `--fast` 去重输出、`opportunities.json` 结构迁移与状态流转同步、`--digest` 内容生成与空内容静默退出。

---

## 12. 已知限制

1. **人名过滤为启发式规则**：极生僻外国名且标题无职务关键词时可能有边缘误差。
2. **Google Trends 频率限制**：命中率核验内置 1.2 秒节流；Trends 返回空时该词计未命中。
3. **OpenCode 免费通道限流**：429 时该轮部分词未打分（unscored 放行），属预期降级；有 OpenRouter key 时优先走 OpenRouter。
4. **X 信号源不稳定**：依赖公开 Nitter 实例，不可用时静默跳过。
