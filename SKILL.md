---
name: tg-reader-mcp
description: Operate the Telegram reader and the repository's single frozen V4.7 S&P 500 ten-stock strategy on a dedicated Alpaca Paper account. Use for Fama-French synchronization, point-in-time factor scoring, monthly V4.7 target generation, approval, dry-runs, order execution, partial-fill recovery, and account/state audits.
---

# Telegram reader and V4.7

Treat V4.7 as the only trading strategy in this repository. Do not add market timing,
news sentiment, TACO, discretionary overlays, Alpha Vantage, leverage, shorting, or
manual positions to its Paper account.

## Frozen contract

- Universe: the current `sp500.csv` from `fja05680/sp500`, resolved from
  `master` to an immutable commit and observed after the live month-end close.
  Its exact CSV hash must be reviewed and explicitly approved; every ticker/CIK
  pair is then checked against SEC submissions. If submissions omit tickers, the
  pair must instead match `dei:TradingSymbol` in the latest causally available SEC
  primary filing; issuer-name matching is never sufficient.
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
- Alpaca SIP adjusted daily bars: market-cap price, exact-session Momentum, and ADV20.
- Alpaca IEX snapshots: execution-time estimates; the configured Paper key returns
  HTTP 403 for real-time SIP snapshots, so do not silently switch this field.
- SEC EDGAR: current issuer submissions, primary-filing ticker identity, accession
  timing, XBRL fundamentals, and SIC;
  no key, but a contact-email User-Agent is mandatory.
- `fja05680/sp500`: current constituents and CIKs; no GitHub token required.
- Dartmouth Fama-French Data Library: FF5 and Momentum downloads; no API key.
- Telegram credentials: only when operating the reader MCP.

No Alpha Vantage or other market/news API is required.

## Configuration

Keep only these top-level keys in `config.yaml`:

- `telegram_api`
- `alpaca`
- `factor_data`
- `factor_portfolio`
- `factor_execution`

Set `alpaca.paper: true`, `factor_execution.dedicated_account: true`, and
`capital_allocation_usd <= 100000`. Never put manual holdings in this account.
Keep `factor_data.universe_mode: latest_only`, historical `alpaca_feed: sip`,
`alpaca_snapshot_feed: iex`, and the frozen
coverage gates. This repository must not rebuild historical S&P 500 membership;
that belongs to `D:\workspace\factor-model`.

`--probe-sources` prints `constituent_sha256` and whether it matches
`factor_data.approved_constituent_sha256`. Inspect the captured CSV and its diff
from the previously approved list before copying that exact hash. Never let an
agent or cron update this approval automatically.

## Monthly target workflow

From the repository root:

```powershell
$PYTHON = ".\.venv\Scripts\python.exe"
& $PYTHON scripts\build_live_factor_signals.py --probe-sources
# inspect the current CSV/diff, then approve its exact constituent_sha256 in config.yaml
& $PYTHON scripts\sync_fama_french_factors.py
& $PYTHON scripts\build_live_factor_signals.py --decision-date YYYY-MM-DD
& $PYTHON scripts\factor_portfolio.py
(Get-FileHash data\factor_portfolio_v4_7_latest.json -Algorithm SHA256).Hash.ToLower()
```

`build_live_factor_signals.py` must run on the actual final US market session,
after 16:05 New York time. It refuses retroactive use of a later current list.
It requires 490-510 constituents, distinct source snapshots, 99% CIK, 80%
fundamental, 98% price, 95% FF12 industry, 60% complete-signal coverage, and at
least 450 cross-sectional rows. A failure produces no target.

The first valid month-end run freezes a deterministic universe capture ID. SEC
progress is checkpointed per issuer and daily bars are frozen, so an interrupted job may be
rerun later with the same decision date without downloading a later constituent
list. Every source records its own retrieval time; the five final source hashes
produce a separate bundle ID, so later SEC or adjusted-bar revisions cannot
masquerade as the original bundle. Completed signal CSVs and manifests are retained under
`data/factor_sources/signals/YYYY-MM-DD/` by content hash; the files under `data/`
are only latest pointers.

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
