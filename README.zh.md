# tg-reader-mcp

一个**只读**的 Telegram MCP Server。给你的 AI agent 看 TG 的能力，但拿不到"发消息"这把刀。

[English](./README.md)

## 为什么坚持只读

给 bot 装"发消息"很常见，给 AI agent 装就是另一回事。prompt 滑一下、工具调用幻觉一次、频道 ID 抄错一位，下一秒 agent 就替你在别人群里说话了。

这个 MCP 直接把刀收走。只开放四个工具：`list_dialogs` / `read_channel` / `search_channel` / `mark_read`。没有 send、没有 edit、没有 delete，想用都没接口。

## 四个工具做什么

- **`list_dialogs`** — 列出频道、群组、私聊。支持按关键词、未读状态、类型组合过滤，比如 `unread_dm` 只给你未读私聊。
- **`read_channel`** — 读取指定频道/群的消息。`since` 按 ISO 时间戳正向过滤，`offset_date` 反向翻页。
- **`search_channel`** — 在单个频道里搜关键词。
- **`mark_read`** — 标记对话为已读。AI 消化完一批新消息后清掉"未读"状态，下次轮询不会再重复拉。

## 工作流长什么样

先用 Telethon 登一次你的 Telegram 账号（userbot），生成本地 `.session` 文件。MCP 启动时加载这个 session 读消息。每次请求前跑一次 `catch_up()` 保证不是缓存的陈旧数据。

```
你：看看 @durov 最近发了啥
AI：[调用 read_channel，channel="durov", limit=5]
    → Durov 过去 48 小时发了 3 条，核心内容：...
```

自用踩过一个坑：多个 MCP 进程连同一个 session 文件，SQLite 会锁冲突，轻则查询挂起重则 session 损坏。所以代码内置了**按 PID 隔离 session 副本**的机制，Claude Code 跟其他客户端同时跑也不打架。这是我自己用半年攒出来的点，也是这个 repo 跟其他 Telegram MCP 的主要差异。

## 安装和登录

### 1. 拉代码

```bash
git clone https://github.com/runesleo/tg-reader-mcp.git
cd tg-reader-mcp
uv venv
source .venv/bin/activate
uv pip install -e .
```

### 2. 获取 Telegram session 文件

需要一个 Telethon 的 `.session` 文件，有两种方式：

**方式 A：从已有机器复制（推荐，免交互登录）**

如果已在其他机器上生成过 `tg_session.session`，直接复制到项目根目录：

```bash
scp tg_session.session user@host:/path/to/tg-reader-mcp/
chmod 600 tg_session.session
```

**方式 B：在当前机器上交互登录生成**

先复制配置模板并填入 Telegram API 凭证：

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入 telegram_api.api_id 和 telegram_api.api_hash
# 申请地址: https://my.telegram.org/apps
```

然后运行登录脚本：

```bash
python login.py
# 输入手机号 + Telegram 发来的验证码 + 开启了 2FA 再输密码
chmod 600 tg_session.session
```

> `.session` 文件等同登录密码，已被 `.gitignore` 忽略。部署到新机器时需手动复制。
>
> `server.py` 中默认的 `API_ID` / `API_HASH` 是 Telegram Desktop 的公开凭证，MCP 服务本身可以直接用。`login.py` 从 `config.yaml` 读取，如果想用自己的凭证（提升限流配额或独立审计），去 [my.telegram.org](https://my.telegram.org) 申请即可。

### 3. 接入 Claude Code

```bash
claude mcp add tg-reader -s user \
  -e TG_SESSION_PATH=/absolute/path/to/tg_session.session \
  -- /absolute/path/to/.venv/bin/python /absolute/path/to/server.py
```

路径换成你本地的真实位置。

### 4. 接入其他 AI agent

任何兼容 MCP 协议的客户端都能用（Cursor / Claude Desktop / 自己写的 agent）。指向 `server.py`，用 `TG_SESSION_PATH` 告诉它 session 文件在哪就行。

## ⚠️ 用之前请读完这一段

这是 **userbot**，不是 bot token。两个概念差得远：

- 你是拿**自己的 Telegram 账号**登录的。每次读取对 Telegram 来说都是"你本人在读"。
- Telegram 的服务条款允许 userbot，但明确禁止骚扰、大规模抓取、冒充。别去大批量轮询、别比人工读更快、别把输出拼成垃圾信息管道。
- **推荐用专门的小号**跑自动化。一旦触发限制或封号，损失的是小号，不是你的主号。
- `.session` 文件**就是**你的登录凭证，按密码级别保管。别提交到 git、别发给别人。`.gitignore` 已经覆盖 `*.session` 和 `*.session-journal`。

Telegram ToS：[telegram.org/tos](https://telegram.org/tos) · API ToS：[core.telegram.org/api/terms](https://core.telegram.org/api/terms)

## 环境要求

- Python 3.10 以上
- `mcp>=1.0.0`、`telethon>=1.34.0`（`uv pip install -e .` 一条命令搞定）
- 一个能用 Telethon 登录的 Telegram 账号
- 就这些。不需要额外 API key、不连数据库、不走第三方服务。

## 支持的输入

| 参数 | 示例 | 解析方式 |
|------|------|---------|
| 频道 username | `durov` | 公开频道 |
| 群组 username | `runesgang` | 公开群组 |
| 频道完整名 | `Durov's Channel` | Telethon `get_entity` 模糊匹配 |
| ISO 时间戳 | `2026-04-13T00:00:00+08:00` | `since` / `offset_date` 用 |

## 典型用法

**每日摘要**：`list_dialogs` 拿 `filter="unread_channel"` → 逐个 `read_channel` → LLM 汇总 → `mark_read` 清未读队列。

**信号研究**：`search_channel` 在某个 alpha 频道搜关键词 → 把结果丢给 LLM 提炼观点。

**跨频道监听**：N 个频道循环 `read_channel`，`since=<上次轮询时间>`，只返回新消息。

## 真实示例频道

[@runesgangalpha](https://t.me/runesgangalpha) — 我的公开频道，用的就是这个 MCP 在做 Polymarket / AI / Crypto 信号的读取和消化，算是这个工作流的活样本。

## 当前默认策略：V4.6-R1 十股因子组合

交易流水线运行独立的 V4.6-R1。每月读取经过独立审批的时点截面产物，从 S&P 500 中选择10只股票，每只目标权重为策略资金袖套的10%。

Paper 账户的策略资金上限为10万美元。执行器只使用账户现金和策略自有多头资产，不使用 Alpaca buying power，不融资、不做空、不加杠杆。
因子买单使用提交前现金校验的限价单，不用市价买单追价；现金不足就拒绝。每笔卖单提交前也会重新核对当前多头数量，禁止超卖形成空头。

```text
五项个股信号 -> 对合格的 S&P 500 股票排序
行业上限     -> 每个 FF12 行业最多3只
最终组合     -> 10只 × 10%，总权重100%
```

TACO/nTACO 不是选股因子、仓位覆盖层或执行依赖。旧 TACO 同步和 QQQ 回测脚本仅作为独立历史研究工具保留。

### 生成并审批月度篮子

```powershell
.\.venv\Scripts\python.exe scripts\sync_fama_french_factors.py
.\.venv\Scripts\python.exe scripts\factor_portfolio.py
(Get-FileHash data\factor_portfolio_latest.json -Algorithm SHA256).Hash.ToLower()
```

人工检查篮子后，将 SHA-256 填入 `factor_execution.approved_target_sha256`。目标缺失、被修改、来自未来或过期时，执行器都会停止。

### 交易流水线

```powershell
# 默认 dry-run：同步十股价格并生成调仓计划
.\.venv\Scripts\python.exe scripts\run_analysis_trade_pipeline.py --strategy factor-v4.6-r1

# 使用已下载数据做确定性 dry-run
.\.venv\Scripts\python.exe scripts\run_analysis_trade_pipeline.py --strategy factor-v4.6-r1 --skip-data-sync --skip-account-refresh --execution-date 2026-08-12

# 提交 Alpaca Paper 订单：要求新鲜账户快照
.\.venv\Scripts\python.exe scripts\run_analysis_trade_pipeline.py --strategy factor-v4.6-r1 --execute-trades
```

订单仍然是显式开启，且默认仅限 Paper。流水线保留非本策略持仓，先写订单意图日志，策略自有卖单优先执行，任何订单未达到 `filled` 状态时立即停止。

## 集成：alpaca-live-trading skill（旧流程说明）

本 MCP 的核心用途之一是作为 [alpaca-live-trading](https://github.com/runesleo/alpaca-live-trading) 交易系统的**信号源**。典型流程：通过 `tg-reader-mcp` 读取 Telegram 金融新闻频道（如金十bot、alpha 信号群），将信息注入交易 pipeline 用于情绪分析和交易决策。

### 架构

```
Telegram 频道              tg-reader-mcp (MCP)         alpaca-live-trading
┌──────────────┐          ┌──────────────────┐         ┌──────────────────┐
│ 金十bot       │─────────▶│ list_dialogs     │         │ 第一阶段：预筛选   │
│ Alpha 信号群   │          │ read_channel     │────────▶│ 第二阶段：深度分析  │
│ Crypto 资讯   │          │ search_channel   │         │ 交易执行           │
└──────────────┘          │ mark_read        │         └──────────────────┘
                          └──────────────────┘
```

### 数据管道

alpaca-live-trading 运行两阶段分析 pipeline：

**第一阶段 — 预筛选**：用技术面策略（如 `w_bottom_breakout`、`autoresearch_trend`）筛选候选标的。数据源：本地 SQLite（日线 + 基本面）。

**第二阶段 — 深度分析**：对每个候选 + 基准 ETF（QQQ, SPY）采集：
- AlphaVantage 新闻情绪 + 基本面（OVERVIEW / INCOME_STATEMENT / BALANCE_SHEET / CASH_FLOW）
- Alpaca 行情 + SQLite 技术指标
- Polymarket 赔率用于市场门控
- **Telegram 频道信号**（通过本 MCP）—— 搜索个股提及、读取突发新闻、从金融新闻 bot 获取情绪

### 支持的 Telegram 信号源

| 频道 | 用户名 | 用途 |
|------|--------|------|
| 金十bot | `@jinshishuju_bot` | 实时财经新闻（地缘政治、宏观、大宗商品） |
| 自定义 alpha 频道 | 各异 | 通过 `search_channel` 做个股信号研究 |

### 示例工作流：新闻驱动的交易信号

```
1. Agent 调用 list_dialogs(filter="unread_dm") → 发现金十bot有11条未读
2. Agent 调用 read_channel(channel="jinshishuju_bot", limit=50) → 获取最新新闻
3. Agent 提取关键信号："伊拉克恢复南部石油出口" → 原油看涨信号
4. alpaca-live-trading pipeline 交叉验证：
   - AlphaVantage 石油板块基本面
   - Polymarket 伊朗/中东局势赔率
   - 本地 SQLite 技术指标
5. Pipeline 生成交易计划 → 风控校验 → 执行（需 --execute-trades 标志）
6. Agent 调用 mark_read(channel="jinshishuju_bot") → 清除未读队列
```

### alpaca-live-trading 数据基础设施

交易系统维护本地 SQLite 数据库：

**日线数据**（`data/stock_daily.sqlite`）：
- `stock_daily` — OHLCV，主键 `(symbol, trade_date)`
- 同步策略：首次 `outputsize=full`，后续 `outputsize=compact`，不足时回退 full

**基本面**（5年季度窗口）：
- `fundamentals_quarterly` — 收入、营业利润、净利润、现金流、资产负债
- `fundamentals_overview_daily` — 市值、PE、利润率、ROE/ROA、做空比率

**同步脚本**：

```bash
# 日线（单只 / 批量 / 默认池）
python scripts/sync_alpha_daily_to_sqlite.py --symbol AAPL
python scripts/sync_alpha_daily_to_sqlite.py --symbols AAPL,MSFT,NVDA --max-calls-per-minute 75

# 基本面
python scripts/sync_alpha_fundamentals_to_sqlite.py --symbol AAPL --years 5
python scripts/sync_alpha_fundamentals_to_sqlite.py --default-pool --years 5 --batch-size 20

# 查询
python scripts/query_fundamentals_sqlite.py --symbol BABA --quarters 8
python scripts/query_prices_sqlite.py --symbols AAPL,NVDA --days 60
```

### V4.6-R1 执行

```bash
# 仅 dry-run（不下单）
python scripts/run_analysis_trade_pipeline.py --strategy factor-v4.6-r1

# 显式提交 Alpaca Paper 订单
python scripts/run_analysis_trade_pipeline.py --strategy factor-v4.6-r1 --execute-trades
```

下面的 Telegram/新闻配置属于仓库其他旧组件，与 V4.6-R1 执行器相互独立。`run_analysis_trade_pipeline.py` 不接受 `--tg-news`，不使用 market gate，也不读取 TACO。

保留给旧组件的 `config.yaml` 配置：

```yaml
strategy:
  enabled: true
  name: w_bottom_breakout
  min_confidence: 0.6
  prefilter_top_k: 10

market_gate:
  benchmark_tickers: [QQQ, SPY]
  threshold: -0.05
```

### 旧版交易决策逻辑

1. **Round2 综合评分**：`fundamental_score`（50%）+ `technical_score`（30%）+ `news_score`（20%，带时效衰减）
2. **通过规则**：保留 `score >= 0.4` 的标的，若无达标则回退 top-k
3. **置信度融合**：`0.7 × stage1_confidence + 0.3 × round2_score`
4. **仓位计算**：`qty = floor(min(可用现金 × max_position_pct, max_trade_notional) / 价格)`
5. **风控拦截**：拒绝 `exceed_max_trade_notional` / `exceed_max_position_pct` / `exceed_max_positions`
6. **执行门控**：需要 `market_gate_score >= threshold` 且命令包含 `--execute-trades`

### Telegram 新闻集成（不属于 V4.6-R1）

Pipeline 支持将 Telegram 新闻合并到 AlphaVantage 新闻流中。TG 消息通过关键词映射表（`scripts/tg_ticker_map.yaml`）匹配 ticker，用中文财经关键词词典打分，然后转化为与 AlphaVantage 相同的 article schema —— 现有的 `_compute_news_rank` 和 `_compute_round2_scores` 无需改动。

在 `config.yaml` 中配置旧版 Telegram 采集：

```yaml
telegram:
  enabled: true
  session_path: "/path/to/tg_session.session"
  channels:
    - name: "jinshishuju_bot"
      type: "dm"
      limit: 50
  ticker_map: "scripts/tg_ticker_map.yaml"
  sentiment_mode: "keyword"
  tg_weight: 0.8
```

**独立测试：**

```bash
# 单独测试 TG 新闻采集
python scripts/query_tg_news.py --channels jinshishuju_bot --limit 30

# 只匹配指定 ticker
python scripts/query_tg_news.py --channels jinshishuju_bot --tickers USO,GLD,SPY --json
```

**中文新闻情绪打分原理：**
- 约 60 个看涨/看跌关键词，带强度权重
- 金十bot 标签（如 `【原油】`、`【伊朗局势】`）直接映射到 sector/ticker
- 星级（`★` / `★★`）作为重要性权重
- 分数乘以 `tg_weight`（默认 0.8）后再与 AV 数据合并

## 当前版本限制（0.1.0）

- **不下载媒体**——只读文本。图片、视频、语音返回的 text 字段是空的。
- **反应/转发链未暴露**——`views` 浏览量有，但 reactions、forward chain 拿不到。
- **`since` 模式的翻页上限 500 条**——日常够用，深度回溯要配合 `offset_date` 多轮拉取。

## Roadmap

**读的广度**
- [ ] 媒体下载（图片、文件、语音）
- [ ] Reactions 和转发链
- [ ] 论坛式群组的 topic/thread 支持

**性能**
- [ ] 跨请求的连接池复用
- [ ] 高频频道可选 Redis 缓存

**部署形态**
- [ ] Docker 镜像（挂载 session 文件）
- [ ] Remote MCP（HTTP 传输）多客户端方案

## 关于作者

*关于作者：Leo（[@runes_leo](https://x.com/runes_leo)），AI x Crypto 独立构建者。在 [Polymarket](https://polymarket.com/?via=runes-leo&r=runesleo&utm_source=github&utm_content=tg-reader-mcp) 做量化交易，用 Claude Code 搭建数据分析和自动化交易系统。更多实战分享：[leolabs.me](https://leolabs.me)*

## License

MIT — 详见 [LICENSE](./LICENSE)。
