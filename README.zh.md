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
& $PYTHON scripts\sync_fama_french_factors.py
& $PYTHON scripts\factor_portfolio.py
(Get-FileHash data\factor_portfolio_v4_7_latest.json -Algorithm SHA256).Hash.ToLower()
```

人工检查目标后，把哈希手动写入 `factor_execution.approved_target_sha256`。

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
