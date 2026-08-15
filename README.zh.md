# tg-reader-mcp

只读 Telegram MCP，并只保留一套交易流程：使用专用 Alpaca Paper 账户运行冻结的
V4.7 S&P 500 十股因子策略。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -e .
Copy-Item config.example.yaml config.yaml
```

使用 Telegram MCP 时填写 Telegram 凭证；运行 V4.7 时填写 Alpaca Paper 凭证。
不再需要 Alpha Vantage 或其他行情、新闻 API。
SEC EDGAR 不需要 API key，但 `factor_data.sec_user_agent` 必须包含真实联系邮箱；
Alpaca 账户必须具备 SIP 历史行情权限；该 Paper key 没有 SIP 实时权限，因此下单前
snapshot 明确使用 IEX。

```powershell
python login.py
python server.py
```

## V4.7

V4.7 使用 Size 10%、Value 30%、Profitability 10%、Investment 30%、Momentum 20%
对时点一致的 S&P 500 候选股票打分。最终持有十只，按综合分六次方倾斜，单股权重
限制在 5%-20%，FF12 行业权重不超过 35%。

```powershell
$PYTHON = ".\.venv\Scripts\python.exe"
# 只读检查数据源，不生成目标、不下单
& $PYTHON scripts\build_live_factor_signals.py --probe-sources
# 检查当前成分 CSV 及与上次名单的差异，再把精确 constituent_sha256 写入 config.yaml

# 只能在当月最后一个美股交易日、纽约时间 16:05 后运行
& $PYTHON scripts\sync_fama_french_factors.py
& $PYTHON scripts\build_live_factor_signals.py --decision-date YYYY-MM-DD
& $PYTHON scripts\factor_portfolio.py
(Get-FileHash data\factor_portfolio_v4_7_latest.json -Algorithm SHA256).Hash.ToLower()
```

人工检查目标后，把哈希手动写入 `factor_execution.approved_target_sha256`。

执行仓库只读取 `fja05680/sp500` 的最新 `sp500.csv`。程序先把 `master`
解析成不可变 commit，再冻结原始成分文件、更新当前 SEC 财报并下载 Alpaca SIP
调整后日线。这里不重建历史成分；历史成分研究和回测继续保留在
`D:\workspace\factor-model`。

第三方名单不会直接进入交易：成分文件的精确 SHA-256 必须人工批准，且每个
ticker/CIK 都会再用 SEC submissions 核对。月末任务中断后可凭同一成分 capture ID、
逐发行人 checkpoint 和冻结日线续跑；五类来源分别记录下载时间，最终哈希组成独立
bundle ID，完整信号与 manifest 按日期和内容哈希归档。

```powershell
# 不下单
& $PYTHON scripts\run_analysis_trade_pipeline.py --strategy factor-v4.7

# 提交 Alpaca Paper 订单
& $PYTHON scripts\run_analysis_trade_pipeline.py --strategy factor-v4.7 --execute-trades
```

该 Paper 账户只能运行 V4.7，资金上限 10 万美元，固定保留 $25 舍入缓冲，
不融资、不加杠杆、不做空。
十个订单相互独立提交；部分成交不会阻止其他股票下单。未完成订单保存在认证 journal
中，下次运行会按确定性的 client order ID 自动对账和续跑。所有在途买单共享现金预留
预算；旧月份 journal 会先按其不可变目标归档完成对账，再载入新的月度目标。不要手动删除 journal，
也不要让 agent 自动修改批准哈希。

发生股票代码变更、拆股或明确的账户状态恢复时，先确认账户没有未完成订单且持仓都属于
当前批准篮子，再运行显式恢复命令：

```powershell
& $PYTHON scripts\recover_factor_execution_state.py `
  --confirm-target-sha256 "<当前批准的完整 SHA-256>"
```

完整操作契约见 [SKILL.md](SKILL.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
