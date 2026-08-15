# tg-reader-mcp

Read-only Telegram MCP server plus one trading workflow: the frozen V4.7 S&P 500
ten-stock factor strategy on a dedicated Alpaca Paper account.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -e .
Copy-Item config.example.yaml config.yaml
```

Fill the Telegram credentials when using the MCP and the Alpaca Paper credentials
when using V4.7. No Alpha Vantage or other market/news API is required.
SEC EDGAR needs no key, but `factor_data.sec_user_agent` must contain a real
contact email. The configured Alpaca account must have SIP historical-data access;
execution-time snapshots use IEX because this Paper key has no real-time SIP entitlement.

The third-party constituent file is never accepted implicitly. Review the CSV and
its diff, approve its exact SHA-256 in `factor_data.approved_constituent_sha256`,
and let the builder verify every ticker/CIK pair against SEC submissions.

```powershell
python login.py
python server.py
```

## V4.7

V4.7 ranks point-in-time S&P 500 candidates with Size 10%, Value 30%,
Profitability 10%, Investment 30%, and Momentum 20%. It keeps ten stocks and
allocates with score^6 under 5%-20% name bounds and a 35% FF12 industry cap.

```powershell
$PYTHON = ".\.venv\Scripts\python.exe"
# Read-only connectivity test; no target and no orders
& $PYTHON scripts\build_live_factor_signals.py --probe-sources
# inspect the CSV/diff, then approve the printed constituent_sha256 in config.yaml

# Run only on the final US market session, after 16:05 New York time
& $PYTHON scripts\sync_fama_french_factors.py
& $PYTHON scripts\build_live_factor_signals.py --decision-date YYYY-MM-DD
& $PYTHON scripts\factor_portfolio.py
(Get-FileHash data\factor_portfolio_v4_7_latest.json -Algorithm SHA256).Hash.ToLower()
```

After reviewing the target, manually place that hash in
`factor_execution.approved_target_sha256`.

The execution repository downloads only the current `sp500.csv` from
`fja05680/sp500`. It resolves `master` to an immutable commit, freezes the raw
file, updates current SEC filings, and downloads Alpaca SIP adjusted bars. It
does not reconstruct historical membership. Historical constituent research and
backtests remain in `D:\workspace\factor-model`.

The first valid month-end run freezes a universe capture ID. Per-issuer SEC checkpoints and
captured daily bars allow a failed job to resume without substituting a later
constituent list. Each source has its own retrieval time, and the five final hashes
form a distinct bundle ID. Signal CSVs and manifests are retained by date and content hash.

```powershell
# No orders
& $PYTHON scripts\run_analysis_trade_pipeline.py --strategy factor-v4.7

# Alpaca Paper orders
& $PYTHON scripts\run_analysis_trade_pipeline.py --strategy factor-v4.7 --execute-trades
```

The account is dedicated to V4.7, capped at $100,000, cash-only, long-only, and
unlevered, with a fixed $25 rounding reserve. All legs are submitted independently.
Partial fills remain in an authenticated journal and are reconciled by deterministic
client order ID on the next run. All in-flight buys share one cash reservation
budget, and an older monthly journal settles against its immutable archived target
before a new target is loaded. Never manually delete the journal or automatically
change the approval hash.

For a confirmed ticker change, split, or account-state recovery, first ensure there
are no open orders and every position belongs to the approved basket, then run:

```powershell
& $PYTHON scripts\recover_factor_execution_state.py `
  --confirm-target-sha256 "<full approved SHA-256>"
```

See [SKILL.md](SKILL.md) for the full operating contract.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
