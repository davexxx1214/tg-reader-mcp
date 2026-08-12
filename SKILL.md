---
name: tg-reader-mcp
description: Operate this repository's Telegram reader, standalone V4.6-R1 S&P 500 ten-stock factor portfolio, and legacy TACO tools. Use for point-in-time factor scoring, Fama-French data acquisition, monthly basket generation, parameter research, audit, Alpaca Paper dry-runs, and approved rebalances. Never combine TACO/nTACO with V4.6-R1 unless the user explicitly creates a new research version.
---

# TG Reader MCP and V4.6-R1

Run commands from the repository root. On Windows PowerShell:

```powershell
$PYTHON = ".\.venv\Scripts\python.exe"
```

## Strategy boundary

Treat these as independent systems:

- `V4.6-R1`: select 10 S&P 500 stocks monthly and hold each at 10% of the strategy sleeve.
- `nTACO/QQQ`: legacy, separate research and backtest code in `taco_strategy.py` and `backtest_ntaco_qqq.py`.
- Telegram MCP: message ingestion and querying; it does not determine V4.6-R1 positions.

For V4.6-R1, never download or read TACO data, never calculate nTACO, never scale the ten-stock basket to 80% or 0%, and never use QQQ as the execution target. Do not import `sync_taco_data.py` or `calculate_ntaco_signal` in the V4.6-R1 execution path.

## Frozen V4.6-R1 contract

Use these production parameters:

```yaml
factor_portfolio:
  enabled: true
  mode: v4_6_r1_top10
  parameter_mode: frozen
  research_id: v4_6_r1_0001
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

The target always contains exactly 10 unique securities with `target_weight: 0.1`; total target weight is 1.0.

## Why five stock scores appear in a six-factor model

The Fama-French five factors plus Momentum contain six return factors: market, size, value, profitability, investment, and momentum. V4.6-R1 uses five cross-sectional stock-selection signals because the market factor is common market exposure rather than a stock-ranking characteristic. Store and audit FF6 data for risk attribution; do not add the market factor to an individual stock's composite score.

## Point-in-time input contract

Build one monthly cross-section in `data/factor_signal_input.csv`. Every row must contain:

- stable `security_id`, current `ticker`, `ff_industry_12`;
- `membership_date`, `decision_date`, `constituent_as_of_date`;
- `fundamental_available_date`, `price_as_of_date`, `industry_as_of_date`;
- either the five `*_raw` signals or fields needed to derive them;
- `risk_eligible` and `adv20_usd`.

All availability dates must be on or before `decision_date`. The CSV must contain exactly one decision cross-section, with no duplicate security ID or ticker.

Accompany the CSV with `data/factor_signal_input.manifest.json`. The manifest must record immutable SHA-256 hashes and `available_through` dates for constituent, fundamental, price, and industry snapshots. `factor_portfolio.py` fails closed when a source is missing, modified, or dated in the future.

## Raw signal formulas

Use supplied point-in-time `*_raw` values when present. Otherwise derive:

```text
Size          = -ln(market_cap)
Value         = net_income_ttm / market_cap
Profitability = operating_income_ttm / average(assets_current, assets_lag_4q)
Investment    = -(assets_current / assets_lag_4q - 1)
Momentum      = momentum_12_1
```

The signs are intentional: smaller size, larger earnings yield, higher profitability, lower asset growth, and stronger 12–1 momentum receive higher scores.

## Percentiles and composite score

For each monthly cross-section:

1. Winsorize Size, Value, Profitability, and Investment at the 1st and 99th percentiles.
2. Rank each fundamental signal ascending within FF12 industry when the industry has at least 10 valid names; otherwise rank across the full market.
3. Convert average ranks to `[0, 1]` percentiles. A larger percentile is better.
4. Rank Momentum descending within FF12 industry, giving the strongest value `1.0`.
5. Calculate:

```text
score = 0.10 × Size_pct
      + 0.30 × Value_pct
      + 0.10 × Profitability_pct
      + 0.30 × Investment_pct
      + 0.20 × Momentum_pct
```

6. Require all five percentiles, `risk_eligible=true`, and ADV20 of at least $10 million.
7. Sort by score descending, breaking ties with `security_id`.
8. Select 10 names with at most 3 from one FF12 industry.
9. Assign every selected name 10%.

Do not optimize execution weights. The score chooses stocks; equal weights build the portfolio.

## Data acquisition

Use the configured providers only for their intended purpose:

- Alpaca: recent prices, quotes, account state, positions, and Paper orders.
- Alpha Vantage: price/fundamental cache where needed.
- SEC EDGAR: point-in-time filing facts and filing timestamps; it needs a compliant User-Agent, not an API key.
- Fama-French Data Library: FF6 risk-factor observations.
- Point-in-time constituent/identity/industry snapshots: supply and hash them in the signal manifest.

Sync Fama-French data:

```powershell
& $PYTHON scripts\sync_fama_french_factors.py
```

Apply a conservative two-month publication lag. For a decision date in July, use FF observations only through May month-end. The factor database is append-only by vintage; use only a vintage fetched no later than the decision date.

Sync stock data only when needed:

```powershell
& $PYTHON scripts\sync_alpha_daily_to_sqlite.py
& $PYTHON scripts\sync_alpha_fundamentals_to_sqlite.py
```

Do not claim that Alpha Vantage or Alpaca alone provides survivorship-bias-free historical constituents, permanent IDs, exact historical filing versions, or reliable delisting consideration.

## Generate and approve the monthly basket

Generate the frozen target:

```powershell
& $PYTHON scripts\factor_portfolio.py
```

Validate the output before approval:

- `method == v4_6_r1_factor_selection`;
- `research_id == v4_6_r1_0001` and `parameter_mode == frozen`;
- exactly 10 unique tickers and security IDs;
- consecutive selection ranks 1–10;
- every target weight equals 0.1;
- manifest hashes and FF6 risk audit pass;
- no input availability date exceeds the decision date.

After human review, calculate the exact artifact hash and place it in `factor_execution.approved_target_sha256`:

```powershell
(Get-FileHash data\factor_portfolio_latest.json -Algorithm SHA256).Hash.ToLower()
```

Any change to the JSON invalidates approval. Never fill the hash automatically and immediately trade; approval is an independent gate.

## Execution configuration

Keep research selection and broker execution separate:

```yaml
factor_execution:
  enabled: true
  target_path: data/factor_portfolio_latest.json
  approved_target_sha256: ""
  state_path: data/factor_execution_state.json
  state_key_path: data/factor_execution_state.key
  journal_path: data/factor_execution_journal.json
  maximum_target_age_days: 40
  legacy_managed_symbols: []
  preserve_unmanaged_positions: true
  paper_only: true
  capital_allocation_usd: 100000
```

`capital_allocation_usd` is the hard $100,000 maximum cash-plus-owned-assets sleeve. Investable notional is `min($100,000, account equity, cash + strategy-owned market value)`. Never use Alpaca `buying_power`, margin, short sales, or leverage. If equity falls below $100,000, reduce the investable base; if it rises above $100,000, do not automatically enlarge this strategy's principal limit.

Leave `legacy_managed_symbols` empty by default. Add a symbol only when the user explicitly transfers ownership of that existing position to this strategy. Never infer ownership because a manual holding later enters the selected basket; reject the collision instead.

## Dry-run and Paper execution

Run a deterministic dry-run:

```powershell
& $PYTHON scripts\run_analysis_trade_pipeline.py `
  --strategy factor-v4.6-r1 `
  --execution-date 2026-08-12 `
  --skip-account-refresh `
  --skip-data-sync
```

For a current-date dry-run with fresh account and prices:

```powershell
& $PYTHON scripts\run_analysis_trade_pipeline.py --strategy factor-v4.6-r1
```

Only after reviewing the dry-run, execute on Alpaca Paper:

```powershell
& $PYTHON scripts\run_analysis_trade_pipeline.py `
  --strategy factor-v4.6-r1 `
  --execute-trades
```

Never pass a manual execution date to execution. V4.6-R1 is unconditionally Paper-only: `paper_only: false` is rejected, even if the user changes the YAML. Any future live version requires a different strategy identifier and separately reviewed entry point.

## Execution safety contract

The executor must:

- load only the approved, non-stale frozen artifact;
- produce exactly ten target weights of 10%;
- keep gross and net target exposure at or below 100%, with no short positions;
- submit factor buys as cash-checked limit orders, never market buys; reject a buy when `qty × limit_price` exceeds current broker cash;
- recheck the current long quantity immediately before each sell and reject any oversell;
- preserve all unmanaged account positions;
- reject an existing manual position that collides with a new target;
- use the authenticated ownership ledger, not ticker membership, to determine sell authority;
- protect the ledger signing key with current-user Windows DPAPI (or POSIX `0600` outside Windows);
- use fractional shares to approach equal weights;
- persist an order-intent journal before submission;
- query every open Alpaca order before journaling, and reject conflicts on target or strategy-owned tickers;
- attach deterministic Alpaca `client_order_id` values;
- stop after an order fails or remains unfilled;
- block a rerun while an unresolved journal exists;
- refresh Alpaca positions after all fills, but advance ownership only by this journal's confirmed `filled_qty`; never infer ownership from aggregate broker quantity;
- retain residual old positions in strategy ownership until Alpaca confirms zero quantity.

The output `data/factor_alpaca_pipeline_latest.json` must not contain `signal`, `ntaco`, `taco`, or a TACO data-sync result. `target_weights` must sum to 1.0 and must not contain QQQ unless QQQ independently ranks into the approved S&P 500 basket, which ordinarily cannot occur because it is an ETF.

## Parameter research

Do not silently tune the frozen production contract. To test weights or constraints:

1. set `parameter_mode: research`;
2. use a new `research_id`, never `v4_6_r1_0001`;
3. preregister the candidate grid and cost assumptions;
4. use 2021–2025 only for training/research;
5. use 2026 YTD only as a validation set, never feed it back into tuning;
6. report return, volatility, Sharpe, drawdown, turnover, cost sensitivity, and comparison with SPY;
7. promote only through a new named version and a new frozen contract.

Do not add TACO as a score, exposure overlay, or tuning variable under the V4.6-R1 name.

## Independent legacy TACO tools

Use `scripts/sync_taco_data.py`, `scripts/taco_strategy.py`, and `scripts/backtest_ntaco_qqq.py` only when the user explicitly asks about the separate TACO/QQQ strategy. Their configuration may remain in `config.yaml`, but V4.6-R1 code must not read it.

## Validation

After any implementation change, run:

```powershell
& $PYTHON -m unittest tests.test_factor_portfolio tests.test_factor_execution tests.test_taco_strategy
& $PYTHON -m unittest discover -s tests
```

Also verify the dependency boundary:

```powershell
rg -n "ntaco|nTACO|TACO|sync_taco|calculate_ntaco" `
  scripts\factor_portfolio.py scripts\run_analysis_trade_pipeline.py
```

The final command must produce no matches. Do not place trades as part of validation.
