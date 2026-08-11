---
name: tg-reader-mcp
description: Operate the read-only Telegram MCP, the causal nTACO exposure strategy, and the V4.6-R1 S&P 500 ten-stock factor portfolio. Use for TACO data sync, Fama-French factor acquisition, point-in-time stock scoring, factor-weight or parameter research, backtests, dry-run portfolio generation, and Alpaca rebalance preparation.
---
# TG Reader MCP + nTACO × V4.6-R1 Factor Portfolio

这个仓库有两项能力：

1. 只读 Telegram MCP，可读取频道、群组和私聊。
2. nTACO 100%/80% 风险暴露策略，包含数据下载、SQLite 落库、回测和 Alpaca 调仓。
3. V4.6-R1 S&P 500 十股因子篮子，用五个横截面信号选股，并用 FF6 做风险研究。

Agent 处理策略任务时，以本文的“组合策略契约”和“策略数据工作流”为准。不要把 TACO 当作第六个股票评分因子：TACO 控制总暴露，五个股票信号负责选股，FF6 只用于风险模型。

## 策略规则

策略采用80%/100% QQQ目标仓位，并保留冷启动时的实际仓位状态。

```text
nTACO = 六因子各自相对严格历史42个观测的压力百分位 × 发布权重

nTACO >= 49%  -> 目标100% QQQ
nTACO <= 30%  -> 最多减至80% QQQ，不从现金反向买入
30%—49%       -> 保持上一目标仓位
```

约束：

- 信号只能用执行日前已经完成的数据。
- 不做空，不加杠杆。
- 回测交易成本为单边5bps。
- TACO 数据过期时，实盘流程必须停止。
- 默认只生成 dry-run 调仓计划，只有显式传入 `--execute-trades` 才能下单。

策略参数位于 `config.yaml -> ntaco_strategy`。默认配置：

```yaml
ntaco_strategy:
  enabled: true
  dashboard_url: "https://ocmacro.com/dashboard/trump"
  symbol: QQQ
  taco_db: "data/taco_daily.sqlite"
  state_file: "data/ntaco_strategy_state.json"
  normalization_lookback: 42
  lower_threshold: 0.30
  upper_threshold: 0.49
  buy_exposure: 1.0
  sell_fraction: 0.20
  max_data_age_days: 7
  transaction_cost_bps: 5.0
```

## 组合策略契约

默认研究组合分两层：

```text
股票层：V4.6-R1 每月从当期 S&P 500 中选 10 只，等权
暴露层：nTACO 每日决定组合总暴露为 100% 或最多降至 80%

最终单股目标权重 = nTACO 目标暴露 ÷ 10
100% 暴露 -> 每股 10%，现金 0%
 80% 暴露 -> 每股  8%，现金 20%
```

保持以下边界：

- nTACO 不进入个股综合得分，也不改变五因子权重。
- 月内只按 nTACO 对十股篮子同比例缩放；股票名单只在月度再平衡日变化。
- V4.6-R1 的已冻结冠军是等权组合。FF6 最小方差仅作风险对照，不得替代冠军。
- 当前 `run_analysis_trade_pipeline.py` 仍是 QQQ-only 下单入口。`factor_portfolio.py` 只生成可审计目标权重，不自动下单；接入十股实盘前必须单独验证报价、碎股、换手和订单失败处理。

默认因子配置：

```yaml
factor_portfolio:
  enabled: true
  mode: ntaco_exposure_overlay
  parameter_mode: frozen
  research_id: v4_6_r1_0001
  factor_db: "data/fama_french_daily.sqlite"
  signal_input: "data/factor_signal_input.csv"
  signal_manifest: "data/factor_signal_input.manifest.json"
  ntaco_signal_path: "data/taco_qqq_pipeline_latest.json"
  output_path: "data/factor_portfolio_latest.json"
  holdings: 10
  max_names_per_industry: 3
  minimum_industry_count: 10
  minimum_adv20_usd: 10000000
  winsor_lower: 0.01
  winsor_upper: 0.99
  factor_lag_months: 2
  allocation_method: equal_weight
  rebalance_frequency: monthly
  weights:
    size: 0.10
    value: 0.30
    profitability: 0.10
    investment: 0.30
    momentum: 0.20
```

## 如何获取六因子风险数据

FF6 风险模型包含 `Mkt-RF, SMB, HML, RMW, CMA, Mom`；`RF` 用于计算超额收益。官方源是 Kenneth French Data Library：

- FF5 daily：`https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip`
- Momentum daily：`https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip`

同步命令：

```powershell
& $PYTHON scripts\sync_fama_french_factors.py
```

输出：

```text
data/fama_french_daily.sqlite -> fama_french_daily
data/fama_french_manifest.json
data/fama_french_vintages/<vintage_id>/{ff5.zip,momentum.zip,ff5.csv,momentum.csv,manifest.json}
```

脚本必须：

- 只接受官方 ZIP 中唯一 CSV，并验证列、日期、重复值和缺失哨兵。
- 将官方百分数除以100，SQLite 中保存小数简单收益。
- 保存源 URL、HTTP 元数据、ZIP/CSV SHA-256、覆盖日期和发布月份；每次下载写入不可覆盖的 `vintage_id`，SQLite 主键为 `(vintage_id,trade_date)`，`fama_french_latest` 仅是最新版本视图。
- 因子更新是阶段性发布，不要求最新交易日等于今天。

因子发布滞后固定为 `t−2` 月末；决策在月份 `t` 时最多使用 `t−2` 月末数据。还必须选择 `fetched_at_utc <= decision_date` 的已归档 vintage；事后首次下载的文件不能冒充历史时点已保存版本。

即使官方文件已含6月数据，也不能把它用于6月底当时的风险估计。`factor_portfolio.py -> conservative_factor_cutoff` 用于复核该边界。

## 如何获取五个股票选择信号

五个选择信号不是从 Kenneth French 文件直接取得；它们必须对当月、当时可投资的 S&P 500 股票逐只计算：

```text
Size          = −ln(决策时点市值)
Value         = 当时已公开的 TTM 净利润 ÷ 决策时点市值
Profitability = 当时已公开的 TTM 营业利润 ÷ 平均总资产
Investment    = −(最新总资产 ÷ 上年同期总资产 − 1)
Momentum      = P(t−21交易日) ÷ P(t−252交易日) − 1
```

数据优先级：

1. S&P 500 成分：实盘使用当期正式名单；历史研究必须使用 point-in-time 成分变更链，不能用今天的名单回填历史。
2. 基本面：历史研究优先 SEC EDGAR filing/acceptance 时间和历史版本；只允许使用决策时点前已公开的四个离散季度。
3. 市值与动量：使用 Alpaca 或已审计日线；调整后价格必须覆盖精确252/21交易日端点。
4. 行业：用当时 SIC 映射到 FF12；历史回测不能用今天行业覆盖过去。
5. AlphaVantage：可用于当前快照和数据补充，但其现有 SQLite 表没有精确首次公开时间，不能单独作为无后视偏差的历史基本面证据。

`factor_signal_input.csv` 接受两类输入：

- 必填身份与时点：`security_id,ticker,ff_industry_12,membership_date,decision_date,constituent_as_of_date,fundamental_available_date,price_as_of_date,industry_as_of_date`；所有可用日期不得晚于决策日，一个文件只能有一个月度截面。
- 直接信号：`size_raw,value_raw,profitability_raw,investment_raw,momentum_raw`；或组件 `market_cap,net_income_ttm,operating_income_ttm,assets_current,assets_lag_4q,momentum_12_1`。
- 风险门槛：`risk_eligible,adv20_usd`；缺少 `risk_eligible` 时脚本按不合格处理，默认最低 ADV20 为1000万美元。

每一行必须代表同一个月末的时点快照。不得在一个文件中混合多个决策月份；缺任一信号的股票不能靠剩余四项平均进入候选池。

CSV 必须配套 `factor_signal_input.manifest.json`：记录 `research_id`、截面日期、CSV SHA-256，以及 constituents/fundamentals/prices/industries 四个源快照各自的本地 `path`、`available_through` 和 SHA-256。脚本会读取源文件逐项验哈希；缺失、变化或未来日期均停止。

## 股票得分与十股选择

运行：

```powershell
& $PYTHON scripts\factor_portfolio.py
```

冻结模式不接受手工暴露：脚本读取 `data/taco_qqq_pipeline_latest.json`，要求 nTACO `signal_date < execution_date = factor decision_date`，并把决策文件哈希写入输出。允许状态为0%、80%或100%；0%用于原先在现金且低信号禁止买入的情形。

评分步骤：

1. 四个基本面原始值在当月全市场做1%/99%缩尾。
2. 每个 FF12 行业有效样本至少10只时，在行业内做升序平均百分位；不足10只则回退全市场。
3. Momentum 在 FF12 行业内按原始动量降序排名，`security_id` 升序打破并列。
4. 五项百分位按冻结权重相加；综合分是0—1横截面排序分，不是预期收益率。
5. 按综合分降序、`security_id` 升序选择；每个 FF12 行业最多3只，直到10只。
6. 对10只股票等权，再乘 nTACO 总暴露。

冻结公式：`score = 10% Size + 30% Value + 10% Profitability + 30% Investment + 20% Momentum`。

输出 `data/factor_portfolio_latest.json` 后至少检查：

- `selected` 恰好10只且 ticker/security_id 唯一。
- 每行业不超过3只。
- `score` 五项完整，目标权重之和等于已验证的 nTACO 暴露。
- `cash_weight = 1 − exposure`。
- 没有使用决策日之后的成分、财报、价格或因子数据。

## 如何调整权重

不要在实盘配置里随手微调。任何权重变化都必须建立新的研究版本，并在看校验集之前冻结候选集合。

默认候选规则：Momentum 固定20%；Size、Value、Profitability、Investment 各只能取10%、20%或30%，四项合计80%。该网格正好19组：

```powershell
& $PYTHON scripts\factor_portfolio.py --list-candidates
```

研究新权重时按以下顺序执行：

1. 冻结训练区间、校验区间、候选19组、成本和选择顺序。
2. 只用训练集跑完全部候选；要求完整月份，并先淘汰累计净收益不超过 SPY 的候选。
3. 剩余候选按累计净收益降序、年化波动升序、最大回撤较浅、换手较低、候选ID升序排序。
4. 冻结训练冠军后只发布一次校验结果。
5. 若想改变 Momentum 20%、扩大权重网格或加入 TACO 分数，必须另建研究版本，不能称为 V4.6-R1。

试验参数只能显式切到 `parameter_mode: research` 并使用新的 `research_id`；输出会改标为 research candidate，记录基线差异与配置哈希。冻结模式对任何权重/持股数/行业上限/缩尾/滞后变化直接报错。研究模式复算可用：

```powershell
& $PYTHON scripts\factor_portfolio.py `
  --weights "size=.1,value=.3,profitability=.1,investment=.3,momentum=.2" `
  --exposure 1.0
```

## 如何调整其他参数

把参数分开治理，禁止同时搜索所有参数：

| 参数组 | 冻结基线 | 调整方法 |
|---|---:|---|
| nTACO 归一窗口 | 42个严格历史观测 | 只在独立 TACO 训练窗口比较少量预注册值 |
| nTACO 阈值 | 30% / 49% | 保持迟滞结构，先冻结阈值网格再做 walk-forward |
| 总暴露 | 80% / 100% | 不加杠杆、不做空；单独评估回撤与换手 |
| 持股数 | 10 | 新版本比较5/10/15时保持权重与时期不变 |
| 行业上限 | 3只 | 检查集中度与候选不足失败率，不得事后放宽 |
| 缩尾 | 1% / 99% | 只在训练集比较稳健性，校验集不再选择 |
| Momentum窗口 | 252/21交易日 | V4.6-R1 固定；改变即新版本 |
| FF6发布滞后 | 至少2个月 | 只能更保守，不能缩短 |
| FF6风险窗口 | 756日，至少504日 | 仅影响最小方差对照和风险诊断；等权冠军不因此改权重 |
| 交易成本 | nTACO 5bps；因子研究10bps | 同时报5/10/20bps敏感性，不用低成本挽救失败策略 |

每次参数实验必须记录：研究ID、配置哈希、输入哈希、训练/校验边界、候选数、成本、胜者、全部失败门槛和是否曾看过校验数据。

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

Agent 必须按以下顺序准备策略数据：

1. 下载或更新 TACO。
2. 下载或更新官方 Fama–French FF5 + Momentum。
3. 更新 QQQ 与股票池日线。
4. 准备单一月末的 point-in-time S&P 500 信号 CSV。
5. 先生成十股目标，再运行 nTACO 回测或 dry-run。

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

两个数据库准备完成后运行：

```powershell
& $PYTHON scripts\backtest_ntaco_qqq.py `
  --start 2025-02-19
```

默认输出：

```text
data/backtests/ntaco_qqq_100_20/summary.json
data/backtests/ntaco_qqq_100_20/daily.tsv
```

回测规则：

- 每个交易日只使用该日期之前的 TACO 六因子数据，并以严格历史百分位计算 nTACO。
- 缺少可用信号时保持前一目标仓位，并在 `daily.tsv -> signal_error` 记录原因。
- 不允许因为数据缺失而删除交易日或删除 QQQ 基准收益。
- `summary.json` 必须同时报告策略和 QQQ 的总收益、年化收益、最大回撤和暴露率。

回测完成后，Agent 至少检查：

```powershell
Get-Content data\backtests\ntaco_qqq_100_20\summary.json
Import-Csv data\backtests\ntaco_qqq_100_20\daily.tsv -Delimiter "`t" |
  Where-Object { $_.signal_error } |
  Select-Object date,target_qqq,signal_error
```

如果 `signal_error` 有记录，需要在结果中明确说明日期和处理方式。

## 默认交易流水线

默认 dry-run 会更新 TACO 和 QQQ 日线，然后读取账户快照并生成调仓计划：

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
- `signal.data_age_days` 没有超过配置上限。
- `signal.ntaco` 在0—100之间，`signal.signal_date` 严格早于执行日。
- `target_weights` 只能持有 QQQ，目标权重在0—1之间；正常触发状态为0.8或1.0。
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
scripts/collect_jin10_messages.py       通用Telegram消息采集（不参与nTACO策略）
scripts/sync_taco_data.py               TACO 下载和 SQLite 同步
scripts/sync_fama_french_factors.py     官方FF5+Momentum下载、哈希和SQLite同步
scripts/sync_alpha_daily_to_sqlite.py   QQQ 日线同步
scripts/taco_strategy.py                nTACO归一化和100/20仓位信号
scripts/factor_portfolio.py             V4.6-R1评分、19组候选和十股目标生成
scripts/backtest_ntaco_qqq.py           nTACO 100/20固定策略回测
scripts/run_analysis_trade_pipeline.py  dry-run 和实盘调仓入口

data/jin10_messages.sqlite              通用Telegram消息库（不参与nTACO策略）
data/taco_daily.sqlite                  TACO 数据库
data/fama_french_daily.sqlite           官方FF6日频风险因子数据库
data/fama_french_manifest.json          官方下载元数据、覆盖范围和哈希
data/fama_french_vintages/              不可覆盖的官方ZIP、CSV与版本清单
data/factor_signal_input.csv            当月point-in-time股票信号输入
data/factor_signal_input.manifest.json  输入与四类源快照的日期/哈希清单
data/factor_portfolio_latest.json       十股目标权重输出
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

### 回测交易日减少

这是错误行为。检查 `daily.tsv`，缺数据日必须保留并按现金处理，QQQ 基准也必须保留当天收益。

### 实盘被拒绝

检查以下字段：

- TACO 最新日期和 `data_age_days`
- Alpaca 账户快照是否刚刷新
- QQQ 实时报价是否成功
- 前一笔订单是否为 `filled`
