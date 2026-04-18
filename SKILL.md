# TG Reader MCP + Alpaca Live Trading Skill

本项目包含两个核心能力：
1. **TG Reader MCP Server** — 只读 Telegram MCP 服务，让 AI agent 读取频道、群组、私聊消息
2. **Alpaca Live Trading** — 一组独立 Python 脚本，用于美股数据查询、分析和自动交易

---

## 第一部分：TG Reader MCP 部署与启动

### MCP 提供的工具

| 工具 | 说明 |
|------|------|
| `list_dialogs` | 列出频道/群组/私聊，支持过滤（`unread_dm`、`unread_channel`、关键词） |
| `read_channel` | 读取指定频道消息，支持 `since` 正向过滤和 `offset_date` 反向翻页 |
| `search_channel` | 在单个频道内搜索关键词 |
| `mark_read` | 标记对话为已读 |

### Linux 部署步骤

#### 1. 克隆并安装依赖

```bash
git clone https://github.com/runesleo/tg-reader-mcp.git
cd tg-reader-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pyyaml requests alpaca-py
```

#### 2. 配置 API 凭证

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，填入真实值：

```yaml
# Telegram API（必填，用于 MCP 读取 TG 消息）
# 申请地址: https://my.telegram.org/apps
telegram_api:
  api_id: 0
  api_hash: "your_telegram_api_hash"

# AlphaVantage API（交易功能需要）
alphavantage:
  api_key: "your_alphavantage_api_key"

# Alpaca Trading API（交易功能需要）
alpaca:
  api_key: "your_alpaca_api_key"
  secret_key: "your_alpaca_secret_key"
  paper: true
```

> `config.yaml` 包含真实凭证，已在 `.gitignore` 中，不会被提交。

#### 3. 获取 Telegram session 文件

`tg_session.session` 是 Telegram 登录凭证，有两种获取方式：

**方式 A：从已有机器复制（推荐，免交互登录）**

如果你已在其他机器上生成过 session 文件，直接复制到项目根目录即可：

```bash
# 从本地机器传到 Linux 服务器
scp tg_session.session user@linux-host:/path/to/tg-reader-mcp/

# 在 Linux 上设置权限
chmod 600 /path/to/tg-reader-mcp/tg_session.session
```

**方式 B：在当前机器上交互登录生成**

如果没有现成的 session 文件，需要运行登录脚本（需要交互输入手机号和验证码）：

```bash
source .venv/bin/activate
python login.py
# 按提示输入：手机号 → 验证码 → 两步验证密码（如有）
chmod 600 tg_session.session
```

> `tg_session.session` 等同登录密码，已被 `.gitignore` 忽略，不会提交到 Git。跨机器部署时需手动复制。

#### 4. 启动 MCP Server

**方式 A：直接运行（前台调试）**

```bash
TG_SESSION_PATH=$(pwd)/tg_session.session \
  .venv/bin/python server.py
```

**方式 B：配置为 MCP 客户端的 server（推荐）**

在 AI agent 的 MCP 配置中添加（以 Cursor 为例，编辑 `.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "tg-reader": {
      "command": "/path/to/tg-reader-mcp/.venv/bin/python",
      "args": ["/path/to/tg-reader-mcp/server.py"],
      "env": {
        "TG_SESSION_PATH": "/path/to/tg-reader-mcp/tg_session.session"
      }
    }
  }
}
```

**方式 C：Claude Code 接入**

```bash
claude mcp add tg-reader -s user \
  -e TG_SESSION_PATH=/path/to/tg-reader-mcp/tg_session.session \
  -- /path/to/tg-reader-mcp/.venv/bin/python /path/to/tg-reader-mcp/server.py
```

> 所有路径替换为 Linux 上的实际绝对路径。

#### 5. 验证连通

Agent 接入后，调用 `list_dialogs` 工具（参数 `limit: 5`），能返回对话列表即表示部署成功。

---

## 第二部分：Alpaca Live Trading Skill

一组独立的 Python 查询脚本，用于获取交易决策所需的各类数据。所有脚本可独立运行，不依赖 MCP 服务。

交易决策与执行所需数据：
1. **获取股价数据** - 通过 Alpaca Market Data 获取实时价格，并基于 SQLite 计算技术指标
2. **获取基本面数据** - 通过 AlphaVantage Fundamentals 获取近一年关键财务
3. **获取市场新闻** - 通过 AlphaVantage NEWS_SENTIMENT API 获取新闻与情绪分析
4. **获取市场情绪** - 通过 Polymarket 获取预测市场赔率指标
5. **查询账户状态** - 通过 Alpaca API 获取当前持仓和账户余额
6. **执行交易并落盘** - 每次交易后同步更新 `position.jsonl` 与 `balance.jsonl`

## 查询脚本

以下脚本均可独立运行，所有脚本位于 `./scripts/` 目录。

## 标准一体化流程（推荐）

默认流程（独立 Skill，不依赖 MCP）：

1. **先同步价格数据到 SQLite（默认自动执行）**  
   - 运行 `run_analysis_trade_pipeline.py` 时会先自动同步默认股票池（NASDAQ 100 + QQQ），并补齐本次分析用到的 benchmark 行情  
   - 若本地不存在历史数据：自动全量同步（`outputsize=full`）  
   - 若本地已有历史数据：自动增量同步（`outputsize=compact`，必要时 fallback full）  
   - 如需跳过可传 `--skip-default-pool-sync`（不建议）  
2. **再刷新 SQLite fundamentals（默认自动执行）**  
   - pipeline 会检查本次分析股票的 `fundamentals_overview_daily` 与 `fundamentals_quarterly`  
   - 默认当 overview 快照超过 `7` 天，或季度财务记录不足时，自动补拉并写回 SQLite  
   - 这一步会直接影响 `autoresearch_trend` 的 `quality_score`  
   - 如需跳过可传 `--skip-fundamentals-sync`；阈值可用 `--fundamentals-stale-days` 调整  
3. **再刷新 Alpaca 账户/持仓快照（默认自动执行）**  
   - pipeline 会在分析前主动拉取 Alpaca 账户与持仓，并追加写入 `position.jsonl` / `balance.jsonl`  
   - 如果 Alpaca 暂时不可用，会回退读取本地 JSONL 快照继续分析  
   - 如需跳过可传 `--skip-account-refresh`
4. 第一阶段（Universe -> TopK）：  
   - 基于策略（如 `autoresearch_trend` / `w_bottom_breakout`）在本地 SQLite 日线数据上做预筛选
5. 第二阶段（深度分析）：  
   - 对第一阶段候选 + `QQQ` + `SPY` 做深度分析  
   - 包含：基本面、新闻情绪、Polymarket 赔率、Alpaca 行情 + SQLite 技术面
6. 市场门控：使用 `QQQ/SPY` 与 Polymarket 信号判断是否允许执行交易
7. 若门控通过则执行交易（可选），并更新 `position.jsonl` + `balance.jsonl`

```bash
# 默认：pipeline 内部会先自动同步 101 股票池，再执行分析
python ./scripts/run_analysis_trade_pipeline.py

# 定时/后台跑时建议无缓冲，避免日志文件长时间为空
PYTHONUNBUFFERED=1 python ./scripts/run_analysis_trade_pipeline.py

# 可选：手动预同步（用于单独观测同步日志/审计）
python ./scripts/sync_alpha_daily_to_sqlite.py --default-pool --with-audit

# 指定股票池并输出结果文件
python ./scripts/run_analysis_trade_pipeline.py \
  --tickers NVDA,MSFT,AAPL \
  --days 365 \
  --news-limit 5 \
  --prefilter-top-k 10 \
  --benchmark-tickers QQQ,SPY \
  --av-calls-per-minute 75 \
  --output-file ./data/analysis_pipeline_latest.json
```

常用参数说明：

- `--prefilter-top-k`：第一阶段筛选数量（默认 10）
- `--benchmark-tickers`：市场门控基准；默认读取 `config.yaml -> market_gate.benchmark_tickers`
- `--market-gate-threshold`：门控阈值；默认读取 `config.yaml -> market_gate.threshold`
- `--skip-default-pool-sync`：跳过分析前默认101池同步（不建议）
- `--skip-fundamentals-sync`：跳过分析前 SQLite fundamentals 刷新（会让 `quality_score` 可能使用旧数据）
- `--fundamentals-stale-days`：控制 fundamentals 多久算过期，默认 `7`
- `--skip-account-refresh`：跳过分析前 Alpaca 账户/持仓快照刷新（会回退到本地 JSONL）
- `--skip-market-snapshot`：跳过实时行情快照拉取（会弱化技术面信号）

## 定时任务与日志（后台运行推荐）

Agent、cron 或 `nohup` 把 pipeline 放到后台并重定向到文件时，常出现「进程在跑、但日志一会儿还是空的」。多数情况是 **Python 标准输出默认缓冲**，不会立刻写入日志文件，不代表脚本已卡死。

建议：

1. **无缓冲运行（任选其一）**  
   - `PYTHONUNBUFFERED=1 python ./scripts/run_analysis_trade_pipeline.py ...`  
   - 或 `python -u ./scripts/run_analysis_trade_pipeline.py ...`  
   单独跑同步脚本时同样适用。

2. **分日志更易排查卡点**  
   - 日线同步：`logs/sync_YYYYMMDD.log`（单独执行 `sync_alpha_daily_to_sqlite.py` 时）  
   - 分析流水线：`logs/pipeline_YYYYMMDD_HHMM.log`  
   重定向示例：`PYTHONUNBUFFERED=1 python ./scripts/run_analysis_trade_pipeline.py ... > ./logs/pipeline.log 2>&1`

3. **长时间无输出时**  
   前几步可能是 SQLite 批量同步、实时行情快照或 AlphaVantage 网络 I/O，属正常现象；开启无缓冲后日志会按阶段陆续出现，便于调度 Agent 判断进度。

**执行交易（可选）**

先准备交易计划文件（JSON 列表）：

```json
[
  {"action": "buy", "symbol": "NVDA", "qty": 1},
  {"action": "sell", "symbol": "AAPL", "qty": 1}
]
```

然后执行：

```bash
python ./scripts/run_analysis_trade_pipeline.py \
  --trade-plan-file ./data/trade_plan.json \
  --execute-trades
```

**自动策略交易（新增）**

当 `config.yaml` 中设置 `strategy.enabled: true` 且未传 `--trade-plan-file` 时，pipeline 会自动：

1. 基于配置的 `strategy.name` 运行策略（未设置时回退 `strategy.names[0]`）
2. 生成 `generated_trade_plan`
3. 执行风控拦截（`risk.*`）
4. 若开启 `--execute-trades`，执行拦截后的交易计划

```bash
# 自动生成交易计划（仅分析不下单）
python ./scripts/run_analysis_trade_pipeline.py

# 自动生成交易计划并执行（受市场门控 + 风控限制）
python ./scripts/run_analysis_trade_pipeline.py --execute-trades
```

输出 JSON 中新增字段：
- `pipeline.market_gate_config`
  - 记录本次运行实际生效的 market gate 配置
- `pipeline.pre_run_sync`
  - 记录分析前价格同步、fundamentals 刷新、账户快照刷新的实际状态
- `strategy_config` / `risk_config`
- `generated_trade_plan`
- `strategy_decisions`
- `risk_rejections`
- `trade_plan_source`（`strategy_auto` 或 `manual_file`）

`analysis_pipeline_latest.json` 里最常看的结构示例：

```json
{
  "pipeline": {
    "market_gate_config": {
      "benchmark_tickers": ["QQQ", "SPY"],
      "threshold": -0.05
    },
    "pre_run_sync": {
      "daily_prices": {"status": "ok"},
      "fundamentals": {"status": "already_fresh"},
      "account_snapshot": {"status": "ok", "positions_count": 0}
    },
    "market_gate": {
      "benchmark_news_signal": 0.08,
      "polymarket_signal": -0.12,
      "market_gate_score": -0.02,
      "threshold": -0.05,
      "should_trade": true
    }
  },
  "trade_execution": {
    "strategy_config": {"name": "autoresearch_trend"},
    "risk_config": {"max_position_pct": 0.1, "max_positions": 6, "max_trade_notional": 10000},
    "generated_trade_plan": [{"action": "buy", "symbol": "ODFL", "qty": 22}],
    "risk_rejections": [],
    "trade_results": []
  }
}
```

- `market_gate_config`：确认本次到底用了哪组 benchmark 和 threshold
- `pre_run_sync`：确认分析前的数据同步是否成功
- `market_gate`：确认是不是被市场门控拦住了
- `generated_trade_plan`：确认最终计划单长什么样
- `trade_results`：只有带 `--execute-trades` 时才会产生真实执行结果

## 交易执行与记录规则（重要）

每次执行交易（buy/sell）时，必须遵循以下流程：

1. 如果这是账户的**第一次交易**，且本地 `position.jsonl` / `balance.jsonl` 还不存在：
   - agent 必须先记录当时的 `QQQ` 和 `SPY` 价格，作为账户启动 benchmark
   - 该 benchmark 主要用于未来与账户净值、策略收益做对比
   - 建议至少记录：`timestamp_et`、`timestamp_utc`、`QQQ price`、`SPY price`、数据来源
   - 推荐先执行：`python ./scripts/query_stock_prices.py QQQ SPY`
   - 这条目前是 **agent 操作要求**，不是现有交易脚本自动写入的字段
2. 下单（Alpaca）
3. 订单成交后，重新查询 Alpaca 账户真实状态（账户概览 + 全部持仓）
4. 读取并更新 `position.jsonl`
5. 读取并更新 `balance.jsonl`

其中：
- `position.jsonl`：记录每笔动作及交易后持仓快照（用于策略/回测一致性）
- `balance.jsonl`：记录交易后账户总览和每只持仓的成本、现价、市值、盈亏（用于资金追踪）

### 1. 查询股价数据 (Alpaca + SQLite 技术面)

```bash
# 查询 NASDAQ 100 + QQQ (共 101 只) 的实时价格
python ./scripts/query_stock_prices.py

# 查询指定股票
python ./scripts/query_stock_prices.py AAPL MSFT NVDA
```

**默认股票列表（NASDAQ 100 + QQQ，101 只）：**
```
NVDA, MSFT, AAPL, GOOG, GOOGL, AMZN, META, AVGO, TSLA, NFLX,
PLTR, COST, ASML, AMD, CSCO, AZN, TMUS, MU, LIN, PEP,
SHOP, APP, INTU, AMAT, LRCX, PDD, QCOM, ARM, INTC, BKNG,
AMGN, TXN, ISRG, GILD, KLAC, PANW, ADBE, HON, CRWD, CEG,
ADI, ADP, DASH, CMCSA, VRTX, MELI, SBUX, CDNS, ORLY, SNPS,
MSTR, MDLZ, ABNB, MRVL, CTAS, TRI, MAR, MNST, CSX, ADSK,
PYPL, FTNT, AEP, WDAY, REGN, ROP, NXPI, DDOG, AXON, ROST,
IDXX, EA, PCAR, FAST, EXC, TTWO, XEL, ZS, PAYX, WBD,
BKR, CPRT, CCEP, FANG, TEAM, CHTR, KDP, MCHP, GEHC, VRSK,
CTSH, CSGP, KHC, ODFL, DXCM, TTD, ON, BIIB, LULU, CDW, GFS,
QQQ
```

> 注意：该脚本使用 Alpaca Market Data（`config.yaml` 中 alpaca 凭证），并结合本地 SQLite 日线计算技术指标。

**查询结果会更新到：**
`./data/stock_prices_latest.json`

**输出示例：**
```
📈 股票实时价格查询
====================
获取 AAPL 价格... ✓
获取 MSFT 价格... ✓

📊 股票价格汇总
股票     当前价格         涨跌       涨跌幅
AAPL     $185.50      +1.30      +0.71%
MSFT     $420.30      +1.80      +0.43%
```

### 2. 查询市场新闻和情绪 (AlphaVantage NEWS_SENTIMENT)

```bash
# 查询最新金融市场新闻
python ./scripts/query_market_news.py

# 查询指定股票相关新闻
python ./scripts/query_market_news.py --tickers AAPL,NVDA

# 查询指定主题新闻
python ./scripts/query_market_news.py --topics technology

# 组合过滤 + 详细模式
python ./scripts/query_market_news.py --tickers AAPL --topics earnings --verbose

# 以 JSON 格式输出（方便程序解析）
python ./scripts/query_market_news.py --tickers NVDA --json

# 分析前置：按股票逐个查询最近 5 条新闻+情绪（推荐）
python ./scripts/query_market_news.py --per-ticker --tickers NVDA,MSFT,AAPL --per-ticker-limit 5 --json
```

**分析/交易前置要求（独立 Skill 场景）**

- 在分析每个股票前，先调用 AlphaVantage NEWS_SENTIMENT。
- 使用 `--per-ticker` 模式，确保每只股票单独拉取新闻（不要用 MCP search）。
- 默认每只股票取最近 `5` 条（`--per-ticker-limit 5`）。
- 可用 `--output-file` 将结果落盘给其他 agent 消费，例如：

```bash
python ./scripts/query_market_news.py \
  --per-ticker \
  --tickers NVDA,MSFT,AAPL \
  --per-ticker-limit 5 \
  --days 7 \
  --sort LATEST \
  --output-file ./data/market_news_per_ticker_latest.json \
  --json
```

### 2.1 查询近一年关键财务数据 (AlphaVantage Fundamentals)

```bash
# 查询单只股票近一年关键财务（公司概览 + 季度财务）
python ./scripts/query_fundamentals.py --tickers NVDA

# 查询多只股票并输出 JSON（供其他 agent 消费）
python ./scripts/query_fundamentals.py \
  --tickers NVDA,MSFT,AAPL \
  --days 365 \
  --output-file ./data/fundamentals_latest.json \
  --json
```

**数据内容：**
- `company_overview`：市值、PE、EPS(TTM)、利润率、ROE/ROA 等
- `quarterly_key_financials`（近一年）：Revenue、NetIncome、FCF、EPS、Debt/Equity 等关键指标

**支持的新闻主题：**
`blockchain`, `earnings`, `ipo`, `mergers_and_acquisitions`, `financial_markets`, `economy_fiscal`, `economy_monetary`, `economy_macro`, `energy_transportation`, `finance`, `life_sciences`, `manufacturing`, `real_estate`, `retail_wholesale`, `technology`

**输出示例：**
```
📰 市场新闻与情绪查询
============================================================
找到 10 篇新闻:

  1. NVIDIA Reports Record Revenue Amid AI Boom
     来源: Reuters | 时间: 2026-02-05 14:30:00
     情绪: 强烈看涨 (+0.456)
     摘要: NVIDIA reported record quarterly revenue driven by...

  2. Apple Announces New AI Features for iPhone
     来源: Bloomberg | 时间: 2026-02-05 12:15:00
     情绪: 看涨 (+0.234)
     摘要: Apple unveiled a suite of new artificial intelligence...
```

### 3. 查询 Polymarket 市场情绪

```bash
# 查询金融市场情绪指标
python ./scripts/query_polymarket_sentiment.py

# 查询热门预测市场
python ./scripts/query_polymarket_sentiment.py --trending
```

**输出示例：**
```
📊 Polymarket 金融市场实时情绪指标
数据时间: 2026-02-05 15:30:00 UTC

## Finance Daily (每日金融)
1. **S&P 500 up today?** | Yes: 65.2% | 24h Vol: $125,000
2. **NASDAQ up today?** | Yes: 58.3% | 24h Vol: $89,000

## Stocks (股票)
1. **AAPL above $185 EOD?** | Yes: 72.1% | 24h Vol: $45,000
2. **NVDA above $900 this week?** | Yes: 61.5% | 24h Vol: $156,000
```

### 4. 查询 Alpaca 账户状态

```bash
# 查询账户余额和持仓
python ./scripts/query_alpaca_account.py

# 同时显示最近订单
python ./scripts/query_alpaca_account.py --orders

# 以 JSON 格式输出
python ./scripts/query_alpaca_account.py --json
```

> 说明：该脚本现在不仅会读取 Alpaca 账户，还会把最新账户/持仓快照追加写入本地 `position.jsonl` 与 `balance.jsonl`，供 pipeline 与风控直接复用。

**输出示例：**
```
💰 Alpaca Paper Trading (模拟交易) 账户状态
============================================================
📊 账户概览
  账户号码: 123456789
  现金余额: $8,523.45
  买入能力: $17,046.90

📦 当前持仓:
  AAPL: 10 股
    成本价: $184.20 | 现价: $185.50 | 市值: $1,855.00
    盈亏: +$13.00 (+0.71%)
  NVDA: 5 股
    成本价: $875.50 | 现价: $900.00 | 市值: $4,500.00
    盈亏: +$122.50 (+2.80%)

总未实现盈亏: +$135.50
```

### 5. 执行交易并同步 `position.jsonl` / `balance.jsonl`

> 首次交易特别要求：如果本地 `position.jsonl` 与 `balance.jsonl` 还未建立，agent 应先抓取并记录 `QQQ` / `SPY` 当时价格，作为账户启动 benchmark，再进行第一笔交易。

```bash
# 首次交易前，先记录 benchmark
python ./scripts/query_stock_prices.py QQQ SPY

# 买入
python ./scripts/execute_alpaca_trade.py --action buy --symbol AAPL --qty 1

# 卖出
python ./scripts/execute_alpaca_trade.py --action sell --symbol AAPL --qty 1

# 输出 JSON
python ./scripts/execute_alpaca_trade.py --action buy --symbol NVDA --qty 2 --json
```

**交易后更新文件（skill 内部目录）：**
- `./data/position/position.jsonl`
- `./data/balance/balance.jsonl`

`balance.jsonl` 每条记录包含：
- `account`：账户总览（cash, buying_power, equity, portfolio_value 等）
- `positions`：每只持仓明细（symbol, qty, avg_entry_price, current_price, market_value, unrealized_pl）
- `trade`：本次交易信息（action, symbol, qty, filled_price, order_id）
- 时间字段：同时保留 `timestamp_et`（US/Eastern）和 `timestamp_utc`（UTC），用于跨机器时间同步与防漂移

### 6. 查询最近 N 条统一交易记录（默认 50 条）

```bash
# 默认最近 50 条
python ./scripts/query_trade_records.py

# 查询最近 20 条
python ./scripts/query_trade_records.py --limit 20

# 输出 JSON
python ./scripts/query_trade_records.py --json
```

该脚本会读取并统一展示：
- `position.jsonl`（动作 + 持仓快照）
- `balance.jsonl`（账户总览 + 持仓明细）
- 并优先按 `timestamp_utc` 排序（兼容旧数据）

### 7. 重置本地账户记录状态（清理 jsonl）

```bash
# 重置单 agent 记录文件（会二次确认）
python ./scripts/reset_account_state.py

# 跳过确认直接执行
python ./scripts/reset_account_state.py --yes
```

该指令会删除：
- `./data/position/position.jsonl`
- `./data/balance/balance.jsonl`

> 注意：只会清理本地记录文件，不会修改 Alpaca 真实账户持仓与余额。下次交易会自动重新创建这两个文件。

## 文件结构

```
./
├── SKILL.md                 # 本文档
├── config.yaml              # API Keys 配置（不提交到 Git）
├── config.example.yaml      # 配置模板
└── scripts/
    ├── _config.py                      # 共享配置加载模块
    ├── query_stock_prices.py           # 查询实时股价
    ├── query_fundamentals.py           # 查询近一年关键财务数据
    ├── query_market_news.py            # 查询市场新闻和情绪
    ├── query_polymarket_sentiment.py   # 查询 Polymarket 预测市场情绪
    ├── run_analysis_trade_pipeline.py  # 一体化流程：分析+可选交易
    ├── query_alpaca_account.py         # 查询 Alpaca 账户状态和持仓
    ├── execute_alpaca_trade.py         # 执行交易并更新 position/balance
    ├── query_trade_records.py          # 查询最近 N 条统一交易记录
    └── reset_account_state.py          # 重置本地账户记录（删除 jsonl）
```

## 故障排查

### 常见问题

1. **config.yaml 不存在**
   - 复制模板: `cp config.example.yaml config.yaml`
   - 填入真实的 API Key

2. **缺少 pyyaml**
   - 运行: `pip install pyyaml`

3. **AlphaVantage API 调用限制**
   - 本 Skill 默认按付费版节流：75 次/分钟（约 0.8 秒/次）
   - 若账号配额不同，可通过脚本参数调整（如 `--av-calls-per-minute`）
   - 遇到限制时等待后重试

4. **Alpaca API Key 无效**
   - 确认 config.yaml 中的 Key 正确
   - 确认使用的是 Paper Trading 账户的 Key

5. **alpaca-py 未安装**
   - 运行: `pip install alpaca-py`
