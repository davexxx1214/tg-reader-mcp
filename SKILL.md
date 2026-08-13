---
name: tg-reader-mcp
description: Operate the Telegram reader and the repository's single frozen V4.7 S&P 500 ten-stock strategy on a dedicated Alpaca Paper account. Use for Fama-French synchronization, point-in-time factor scoring, monthly V4.7 target generation, approval, dry-runs, order execution, partial-fill recovery, and account/state audits.
---

# Telegram reader and V4.7

Treat V4.7 as the only trading strategy in this repository. Do not add market timing,
news sentiment, TACO, discretionary overlays, Alpha Vantage, leverage, shorting, or
manual positions to its Paper account.

## Frozen contract

- Universe: point-in-time S&P 500 input supplied by the research workflow.
- Holdings: 10.
- Signal weights: Size 10%, Value 30%, Profitability 10%, Investment 30%, Momentum 20%.
- Allocation: normalized composite score to power 6, projected to 5%-20% per stock.
- Industry cap: FF12 industry weight at most 35%; selection at most 3 names per industry.
- Rebalance: monthly.
- Factor availability: conservative `t-2` month-end cutoff.
- Account: one dedicated Alpaca Paper account, maximum strategy capital $100,000.
- Orders: fractional shares, long-only, cash-only limit buys; retain a fixed $25
  rounding reserve and never use buying power.

The generated equal-weight V4.6-R1 JSON is only a membership anchor proving that
V4.7 changed allocation but not the selected Top 10. It is not an executable strategy.

## Dependencies

- Alpaca Paper credentials: account, positions, snapshots, orders, and recovery.
- Dartmouth Fama-French Data Library: FF5 and Momentum downloads; no API key.
- Telegram credentials: only when operating the reader MCP.

No Alpha Vantage or other market/news API is required.

## Configuration

Keep only these top-level keys in `config.yaml`:

- `telegram_api`
- `alpaca`
- `factor_portfolio`
- `factor_execution`

Set `alpaca.paper: true`, `factor_execution.dedicated_account: true`, and
`capital_allocation_usd <= 100000`. Never put manual holdings in this account.

## Monthly target workflow

From the repository root:

```powershell
$PYTHON = ".\.venv\Scripts\python.exe"
& $PYTHON scripts\sync_fama_french_factors.py
& $PYTHON scripts\factor_portfolio.py
(Get-FileHash data\factor_portfolio_v4_7_latest.json -Algorithm SHA256).Hash.ToLower()
```

`factor_portfolio.py` writes both:

- `factor_portfolio_v4_6_r1_YYYYMMDD.json`: immutable membership anchor;
- `factor_portfolio_v4_7_latest.json`: executable score-tilted target.

Review the V4.7 JSON, then manually copy its exact SHA-256 to
`factor_execution.approved_target_sha256`. Never update the approval hash merely
because execution reports a mismatch.

## Dry-run and execution

```powershell
# Fresh Alpaca account and quote validation; submits nothing
& $PYTHON scripts\run_analysis_trade_pipeline.py --strategy factor-v4.7

# Submit approved Alpaca Paper orders
& $PYTHON scripts\run_analysis_trade_pipeline.py --strategy factor-v4.7 --execute-trades
```

The executor submits every basket leg even if an earlier leg is `new` or
`partially_filled`. Before submission it writes an authenticated journal containing
deterministic `fv47-...` client order IDs and an account fingerprint.

On a later run:

- existing broker orders are reconciled by client order ID;
- existing or partially filled legs are never duplicated;
- journal legs never submitted are resumed;
- all open buy legs and newly submitted buys share one cash-reservation budget;
- an older monthly journal uses its immutable archived target and settles before a
  newly approved target is loaded;
- when every leg is terminal, actual Alpaca positions are written to signed state;
- canceled or rejected residuals appear in the next rebalance plan.

Never delete `factor_execution_journal.json` manually. Never edit signed state.

## Rotated Alpaca keys

The signed state is account-bound. Rotating keys for the same account continues to
work. For a reset Paper account, ticker change, split, merger conversion, or other
confirmed ownership migration, do not delete state. First verify that there is no
active journal or open order and that every broker position belongs to the currently
approved basket, then run:

```powershell
& $PYTHON scripts\recover_factor_execution_state.py `
  --confirm-target-sha256 "<full approved SHA-256>"
```

The command refreshes Alpaca, requires exact hash confirmation, rejects shorts and
foreign positions, binds the recovered state to that Paper account, and re-signs it.

## Verification

```powershell
& $PYTHON -m unittest discover -s tests
python -X utf8 C:\Users\davex\.codex\skills\.system\skill-creator\scripts\quick_validate.py .
rg -n -i "alphavantage|ntaco|taco|polymarket|factor-v4\.6" scripts config.example.yaml SKILL.md
```

The final search must return no trading dependency references. References to the
V4.6 membership-anchor method inside V4.7 validation are expected and must remain.
