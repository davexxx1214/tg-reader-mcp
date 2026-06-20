# TG Reader MCP + TACO/Jin10 QQQ Strategy Skill

这个仓库有两项能力：

1. 只读 Telegram MCP，可读取频道、群组和私聊。
2. TACO + 金十bot QQQ 择时策略，包含数据下载、SQLite 落库、回测和 Alpaca 调仓。

Agent 处理策略任务时，以本文的“策略数据工作流”为准。旧的股票池筛选、基本面、Polymarket 和按 ticker 新闻打分不再是默认交易流程。

## 策略规则

策略只允许两种状态：100% QQQ 或 100% 现金。

```text
signal = 3日平滑TACO - 3.0 * 金十风险新闻强度 + 5.0 * 金十缓和新闻强度

signal <= -4.0  -> 持有 QQQ
signal >  -4.0  -> 空仓
```

约束：

- 信号只能用执行日前已经完成的数据。
- 不做空，不加杠杆。
- 回测交易成本为 10bps。
- TACO 或金十数据过期时，实盘流程必须停止。
- 默认只生成 dry-run 调仓计划，只有显式传入 `--execute-trades` 才能下单。

策略参数位于 `config.yaml -> taco_strategy`。默认配置：

```yaml
taco_strategy:
  enabled: true
  dashboard_url: "https://ocmacro.com/dashboard/trump"
  symbol: QQQ
  taco_db: "data/taco_daily.sqlite"
  jin10_db: "data/jin10_messages.sqlite"
  jin10_channel: "jinshishuju_bot"
  smoothing_days: 3
  news_half_life_days: 2
  risk_beta: -3.0
  relief_beta: 5.0
  buy_threshold: -4.0
  max_data_age_days: 7
  require_fresh_news: true
  transaction_cost_bps: 10.0
```

## Agent 开始工作前

先确认仓库和 Python 路径：

```powershell
git rev-parse --show-toplevel
Test-Path .\.venv\Scripts\python.exe
```

Windows 命令使用：

```powershell
$PYTHON = ".\.venv\Scripts\python.exe"
```

Linux 命令使用：

```bash
PYTHON=.venv/bin/python
```

首次部署需要：

- `config.yaml` 中配置 `telegram_api.api_id` 和 `telegram_api.api_hash`。
- 项目根目录存在已授权的 `tg_session.session`，也可以在 `telegram.session_path` 指定绝对路径。
- `config.yaml` 中配置 AlphaVantage API Key。
- 需要交易时配置 Alpaca API Key。下载消息、下载 TACO 和回测不要求 Alpaca 交易凭证。

没有 Telegram session 时运行：

```powershell
& $PYTHON login.py
```

`tg_session.session` 等同登录凭证，不得提交到 Git，不得写入日志或报告。

## 策略数据工作流

Agent 必须按以下顺序准备数据：

1. 下载或更新 TACO。
2. 首次运行时回填金十bot历史消息；日常运行时增量采集。
3. 更新 QQQ 日线。
4. 检查三个数据源的最新日期和行数。
5. 运行回测或 dry-run。

### 下载 TACO 到 SQLite

TACO 指数的权威来源：

```text
https://ocmacro.com/dashboard/trump
```

下载命令：

```powershell
& $PYTHON scripts\sync_taco_data.py
```

脚本行为：

- 请求 `https://ocmacro.com/dashboard/trump`。
- 解析页面中的 Next.js 数据。
- 写入 `data/taco_daily.sqlite`。
- 使用 `trade_date` 作为主键，重复运行会更新同一天，不会生成重复记录。

主要表：

```text
taco_daily
  trade_date
  taco_index
  event_strength_score
  raw_json
  fetched_at_utc
```

下载后检查：

```powershell
& $PYTHON -c "import sqlite3; c=sqlite3.connect('data/taco_daily.sqlite'); print(c.execute('select count(*), max(trade_date) from taco_daily').fetchone()); print(c.execute('select trade_date,taco_index from taco_daily order by trade_date desc limit 5').fetchall()); c.close()"
```

Agent 需要确认：

- 命令退出码为 0。
- 返回的 `fetched` 和 `total_rows` 大于 0。
- `latest_date` 接近当前日期。周末和节假日允许停留在最近交易日。
- 页面解析失败时不得手工编造 TACO 数据，也不得继续实盘。

### 下载金十bot历史消息到 SQLite

金十bot频道名：

```text
jinshishuju_bot
```

首次建立数据库时，用 `backfill` 下载完整日期窗口。日期按香港时区解释，`--end` 包含当天。

```powershell
& $PYTHON scripts\collect_jin10_messages.py backfill `
  --start 2026-04-18 `
  --end 2026-06-18 `
  --page-size 100
```

需要其他窗口时，Agent 应替换 `--start` 和 `--end`，不要修改脚本默认值。

默认数据库：

```text
data/jin10_messages.sqlite
```

主要表：

```text
jin10_messages
  channel
  message_id
  date_utc
  date_hk
  text
  views
  raw_json
  fetched_at_utc

jin10_collect_state
  channel
  latest_message_id
  latest_message_date_utc
  updated_at_utc
```

`jin10_messages` 使用 `(channel, message_id)` 作为主键，重复回填不会重复插入。

回填后检查：

```powershell
& $PYTHON -c "import sqlite3; c=sqlite3.connect('data/jin10_messages.sqlite'); print(c.execute(\"select count(*),min(substr(date_hk,1,10)),max(substr(date_hk,1,10)),max(message_id) from jin10_messages where channel='jinshishuju_bot'\").fetchone()); c.close()"
```

Agent 需要确认：

- 行数大于 0。
- 最早日期覆盖回测开始日期。
- 最新日期覆盖回测结束日期或最近可用日期。
- `latest_message_id` 已写入 `jin10_collect_state`。

### 增量采集金十bot消息

日常更新使用：

```powershell
& $PYTHON scripts\collect_jin10_messages.py incremental --limit 500
```

增量采集器从本地 `latest_message_id` 之后开始，按消息 ID 从旧到新写入。单次返回的 `fetched` 等于 `--limit` 时，可能还有积压，Agent 必须继续运行，直到 `fetched < limit`。

PowerShell 自动补齐示例：

```powershell
for ($i = 1; $i -le 20; $i++) {
  $out = & $PYTHON scripts\collect_jin10_messages.py incremental --limit 500
  $out
  $text = $out -join "`n"
  if ($text -match '"fetched":\s*(\d+)' -and [int]$Matches[1] -lt 500) { break }
}
```

不要用 MCP 的 `read_channel` 返回值代替 SQLite 历史库。MCP 适合临时读取；策略和回测需要可重复、可审计的 SQLite 数据。

### 更新 QQQ 日线

```powershell
& $PYTHON scripts\sync_alpha_daily_to_sqlite.py `
  --symbols QQQ `
  --max-calls-per-minute 75
```

数据写入：

```text
data/stock_daily.sqlite -> stock_daily
```

检查最新日期：

```powershell
& $PYTHON -c "import sqlite3; c=sqlite3.connect('data/stock_daily.sqlite'); print(c.execute(\"select count(*),max(trade_date) from stock_daily where symbol='QQQ'\").fetchone()); c.close()"
```

## 回测

三个数据库准备完成后运行：

```powershell
& $PYTHON scripts\backtest_taco_jin10_qqq.py `
  --start 2026-04-18 `
  --end 2026-06-18
```

默认输出：

```text
data/backtests/taco_jin10_qqq/summary.json
data/backtests/taco_jin10_qqq/daily.tsv
```

回测规则：

- 每个交易日只使用该日期之前的 TACO 和金十数据。
- 缺少可用信号时，当天按现金处理，并在 `daily.tsv -> signal_error` 记录原因。
- 不允许因为数据缺失而删除交易日或删除 QQQ 基准收益。
- `summary.json` 必须同时报告策略和 QQQ 的总收益、年化收益、最大回撤和暴露率。

回测完成后，Agent 至少检查：

```powershell
Get-Content data\backtests\taco_jin10_qqq\summary.json
Import-Csv data\backtests\taco_jin10_qqq\daily.tsv -Delimiter "`t" |
  Where-Object { $_.signal_error } |
  Select-Object date,target_qqq,signal_error
```

如果 `signal_error` 有记录，需要在结果中明确说明日期和处理方式。

## 默认交易流水线

默认 dry-run 会更新 TACO、金十消息和 QQQ 日线，然后读取账户快照并生成调仓计划：

```powershell
& $PYTHON scripts\run_analysis_trade_pipeline.py --skip-account-refresh
```

使用已有数据库做确定性 dry-run：

```powershell
& $PYTHON scripts\run_analysis_trade_pipeline.py `
  --skip-data-sync `
  --skip-account-refresh `
  --signal-date 2026-06-19
```

输出：

```text
data/taco_qqq_pipeline_latest.json
```

Agent 应检查：

- `mode` 必须是 `dry_run`，除非用户明确要求实盘。
- `signal.signal_date` 早于 `signal.execution_date`。
- `signal.data_age_days` 和 `signal.jin10_age_days` 没有超过配置上限。
- `target_weights` 只能是 `{"QQQ": 1.0}` 或空对象。
- `trade_results` 在 dry-run 中必须为空。

实盘命令：

```powershell
& $PYTHON scripts\run_analysis_trade_pipeline.py --execute-trades
```

实盘安全规则：

- `--execute-trades` 不能与 `--skip-account-refresh` 一起使用。
- 实盘不能传历史 `--signal-date`。
- 实盘必须取得 Alpaca 实时 QQQ 报价，不能用 SQLite 收盘价代替。
- 调仓计划先卖后买。
- 任一订单状态不是 `filled`，立即停止剩余订单。

## Telegram MCP

MCP 提供以下只读工具：

| 工具 | 说明 |
|------|------|
| `list_dialogs` | 列出频道、群组和私聊 |
| `read_channel` | 读取指定频道消息，支持 `since` 和 `offset_date` |
| `search_channel` | 搜索频道消息 |
| `mark_read` | 标记为已读 |

启动 MCP：

```powershell
$env:TG_SESSION_PATH = (Resolve-Path .\tg_session.session)
& $PYTHON server.py
```

Agent 接入后调用 `list_dialogs`，能返回对话列表表示连接正常。

## 文件位置

```text
scripts/collect_jin10_messages.py       金十回填和增量采集
scripts/sync_taco_data.py               TACO 下载和 SQLite 同步
scripts/sync_alpha_daily_to_sqlite.py   QQQ 日线同步
scripts/taco_strategy.py                TACO + 金十信号计算
scripts/backtest_taco_jin10_qqq.py      固定策略回测
scripts/run_analysis_trade_pipeline.py  dry-run 和实盘调仓入口

data/jin10_messages.sqlite              金十消息库
data/taco_daily.sqlite                  TACO 数据库
data/stock_daily.sqlite                 QQQ 日线数据库
```

## 故障排查

### Telegram session 不存在或未授权

```powershell
& $PYTHON login.py
```

确认 `tg_session.session` 位于项目根目录，或在 `config.yaml -> telegram.session_path` 配置绝对路径。

### TACO 页面解析失败

确认浏览器能访问：

```text
https://ocmacro.com/dashboard/trump
```

不要从搜索结果页或截图手工抄数。修复 `sync_taco_data.py` 的解析器后重新同步。

### 金十增量一直返回 500 条

说明本地积压超过单次限制。继续运行，直到 `fetched < 500`。采集器按旧到新推进，不会因为单次限制跳过中间消息。

### 回测交易日减少

这是错误行为。检查 `daily.tsv`，缺数据日必须保留并按现金处理，QQQ 基准也必须保留当天收益。

### 实盘被拒绝

检查以下字段：

- TACO 最新日期和 `data_age_days`
- 金十最新日期和 `jin10_age_days`
- Alpaca 账户快照是否刚刷新
- QQQ 实时报价是否成功
- 前一笔订单是否为 `filled`
