# tg-reader-mcp

A **read-only** Telegram MCP server. Let your AI agent read channels, groups, and DMs — and do nothing else.

[中文版](./README.zh.md)

## Why read-only

Most Telegram bots can send, edit, delete, join, leave. That's fine for bots — it's dangerous for an AI agent with your own user session. A slip in a prompt, a hallucinated tool call, a mis-quoted channel ID, and suddenly your agent is posting in someone else's group.

This server removes the blast radius. The tools are: `list_dialogs`, `read_channel`, `search_channel`, `mark_read`. No send. No edit. No delete.

## What you get

- **`list_dialogs`** — list channels, groups, DMs. Filter by keyword, unread state, or type (`unread_dm` gives only unread private chats).
- **`read_channel`** — read recent messages from any channel or group. Paginate with `since` (ISO timestamp, forward) or `offset_date` (ISO timestamp, backward).
- **`search_channel`** — keyword search inside one channel.
- **`mark_read`** — mark a conversation as read. Useful when the agent has digested the new messages and should stop re-surfacing them.

## How it works

You log in once with your Telegram account (userbot, via Telethon). The session file lives locally. The MCP server reuses that session to read messages. Every request calls `catch_up()` first to avoid stale data.

```
You: What did @durov post recently?
AI:  [calls read_channel with channel="durov", limit=5]
     → Durov posted 3 messages in the last 48h. Summary: ...
```

Multi-process safety is built in. Each MCP process gets its own copy of the session file (keyed by PID) to avoid SQLite contention when multiple clients connect simultaneously — a real pain point if you run Claude Code and another MCP client against the same session.

## Setup

### 1. Install

```bash
git clone https://github.com/runesleo/tg-reader-mcp.git
cd tg-reader-mcp
uv venv
source .venv/bin/activate
uv pip install -e .
```

### 2. Get a Telegram session file

You need a Telethon `.session` file. Two options:

**Option A: Copy from another machine (recommended — no interactive login needed)**

If you already have `tg_session.session` from a previous setup, just copy it in:

```bash
scp tg_session.session user@host:/path/to/tg-reader-mcp/
chmod 600 tg_session.session
```

**Option B: Run the interactive login**

First, copy the config template and fill in your Telegram API credentials:

```bash
cp config.example.yaml config.yaml
# Edit config.yaml — fill in telegram_api.api_id and telegram_api.api_hash
# Get yours at https://my.telegram.org/apps
```

Then run the login script:

```bash
python login.py
# Enter your phone, the code Telegram sends you, and 2FA if enabled.
chmod 600 tg_session.session
```

> The `.session` file is your login credential — treat it like a password. It's already in `.gitignore`. When deploying to a new machine, copy it manually.
>
> The default `API_ID` / `API_HASH` in `server.py` are Telegram Desktop's public credentials — safe to use for the MCP server itself. `login.py` reads from `config.yaml` so you can use your own (to raise rate limits or separate audit trails). Get them at [my.telegram.org](https://my.telegram.org).

### 3. Wire into Claude Code

```bash
claude mcp add tg-reader -s user \
  -e TG_SESSION_PATH=/absolute/path/to/tg_session.session \
  -- /absolute/path/to/.venv/bin/python /absolute/path/to/server.py
```

Replace the paths with your actual locations.

### 4. Any other AI agent

Any MCP-compatible client works (Cursor, Claude Desktop, your own agent). Point it at `server.py` with `TG_SESSION_PATH` pointing to your `.session` file.

## ⚠️ Before you use

This is a **userbot**, not a bot-token bot. That has real consequences:

- You're logging in with your personal Telegram account. Every read looks like **you** are reading.
- Telegram's ToS permits userbots but bans automation that harasses, scrapes at scale, or impersonates. Don't mass-read, don't poll faster than a human would, don't feed the output into a spam pipeline.
- **Use a dedicated small account** if you'll be running heavy automation. If the account gets limited or banned, you lose access — not your main account.
- The `.session` file **is** your login. Treat it like a password. Don't commit it. Don't share it. The `.gitignore` already covers `*.session` and `*.session-journal`.

Telegram ToS: [telegram.org/tos](https://telegram.org/tos) · API ToS: [core.telegram.org/api/terms](https://core.telegram.org/api/terms)

## Requirements

- Python 3.10+
- `mcp>=1.0.0`, `telethon>=1.34.0` (both installed by `uv pip install -e .`)
- A Telegram account you can log into via Telethon
- That's it. No API keys beyond Telegram's, no database, no external services.

## Supported input

| Parameter | Example | Resolution |
|-----------|---------|-----------|
| Channel username | `durov` | Public channel |
| Group username | `runesgang` | Public group |
| Full channel name | `Durov's Channel` | Fuzzy match via Telethon `get_entity` |
| ISO timestamp | `2026-04-13T00:00:00+08:00` | Used by `since` / `offset_date` |

## Example workflows

**Daily digest**: `list_dialogs` with `filter="unread_channel"` → `read_channel` each → summarize → `mark_read` to clear the queue.

**Signal research**: `search_channel` for a keyword across one alpha channel → feed results into an LLM for thesis extraction.

**Cross-channel monitor**: loop `read_channel` with `since=<last_poll_time>` on N channels → only new messages come back.

## Real-world example channel

Follow [@runesgangalpha](https://t.me/runesgangalpha) — my public channel where I use this exact MCP to read and digest Polymarket, AI, and crypto signals. It's a live demo of the workflow.

## Integration: alpaca-live-trading skill

This MCP is designed to work as a **signal source** for the [alpaca-live-trading](https://github.com/runesleo/alpaca-live-trading) system. The typical flow: read financial news from Telegram channels (e.g. 金十bot, alpha signal groups) via `tg-reader-mcp`, feed them into the trading pipeline for sentiment analysis and trade decisions.

### Architecture

```
Telegram Channels          tg-reader-mcp (MCP)         alpaca-live-trading
┌──────────────┐          ┌──────────────────┐         ┌──────────────────┐
│ 金十bot       │─────────▶│ list_dialogs     │         │ Stage 1: Prefilter│
│ Alpha groups  │          │ read_channel     │────────▶│ Stage 2: Analysis │
│ Crypto feeds  │          │ search_channel   │         │ Trade Execution   │
└──────────────┘          │ mark_read        │         └──────────────────┘
                          └──────────────────┘
```

### Data pipeline

The alpaca-live-trading skill runs a two-stage analysis pipeline:

**Stage 1 — Prefilter**: Screen candidates using technical strategies (e.g. `w_bottom_breakout`, `autoresearch_trend`). Data sources: local SQLite (daily prices + fundamentals).

**Stage 2 — Deep analysis**: For each candidate + benchmark ETFs (QQQ, SPY), collect:
- AlphaVantage news sentiment + fundamentals (OVERVIEW / INCOME_STATEMENT / BALANCE_SHEET / CASH_FLOW)
- Alpaca quotes + SQLite technical indicators
- Polymarket odds for market gating
- **Telegram channel signals** via this MCP — search for ticker mentions, read breaking news, gauge sentiment from financial news bots

### Supported Telegram signal sources

| Channel | Username | Use case |
|---------|----------|----------|
| 金十bot | `@jinshishuju_bot` | Real-time financial news (geopolitics, macro, commodities) |
| Custom alpha channels | varies | Ticker-specific signal research via `search_channel` |

### Example workflow: news-driven trading signal

```
1. Agent calls list_dialogs(filter="unread_dm") → finds 11 unread from 金十bot
2. Agent calls read_channel(channel="jinshishuju_bot", limit=50) → gets latest news
3. Agent extracts: "伊拉克恢复南部石油出口" → bullish oil signal
4. alpaca-live-trading pipeline cross-references with:
   - AlphaVantage fundamentals for oil sector stocks
   - Polymarket odds on Iran/Middle East resolution
   - Technical indicators from local SQLite
5. Pipeline generates trade plan → risk guard validates → execute (if --execute-trades)
6. Agent calls mark_read(channel="jinshishuju_bot") → clears unread queue
```

### alpaca-live-trading data infrastructure

The trading system maintains local SQLite databases with the following schema:

**Daily prices** (`data/stock_daily.sqlite`):
- `stock_daily` — OHLCV with `PRIMARY KEY (symbol, trade_date)`
- Sync strategy: first run `outputsize=full`, subsequent `outputsize=compact`, fallback to full

**Fundamentals** (5-year quarterly window):
- `fundamentals_quarterly` — revenue, operating_income, net_income, cashflows, balance sheet
- `fundamentals_overview_daily` — market_cap, PE, margins, ROE/ROA, short interest

**Sync scripts**:

```bash
# Daily prices (single / batch / default pool)
python scripts/sync_alpha_daily_to_sqlite.py --symbol AAPL
python scripts/sync_alpha_daily_to_sqlite.py --symbols AAPL,MSFT,NVDA --max-calls-per-minute 75

# Fundamentals
python scripts/sync_alpha_fundamentals_to_sqlite.py --symbol AAPL --years 5
python scripts/sync_alpha_fundamentals_to_sqlite.py --default-pool --years 5 --batch-size 20

# Query
python scripts/query_fundamentals_sqlite.py --symbol BABA --quarters 8
python scripts/query_prices_sqlite.py --symbols AAPL,NVDA --days 60
```

### Running the pipeline

```bash
# Analysis only (no orders)
python scripts/run_analysis_trade_pipeline.py

# Analysis + auto-execute (with market gate + risk guard)
python scripts/run_analysis_trade_pipeline.py --execute-trades
```

Config in `config.yaml`:

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

### Trade decision logic

1. **Round 2 scoring**: `fundamental_score` (50%) + `technical_score` (30%) + `news_score` (20%, with recency decay)
2. **Pass rule**: keep candidates with `score >= 0.4`, fallback to top-k if none qualify
3. **Confidence fusion**: `0.7 × stage1_confidence + 0.3 × round2_score`
4. **Order sizing**: `qty = floor(min(cash × max_position_pct, max_trade_notional) / price)`
5. **Risk guard**: rejects `exceed_max_trade_notional` / `exceed_max_position_pct` / `exceed_max_positions`
6. **Execution gate**: requires `market_gate_score >= threshold` and `--execute-trades` flag

### Telegram news integration

The pipeline supports merging Telegram news into the AlphaVantage news stream. TG messages are matched to tickers via a keyword map (`scripts/tg_ticker_map.yaml`), scored using a Chinese financial keyword dictionary, then converted to the same article schema as AlphaVantage — so existing `_compute_news_rank` and `_compute_round2_scores` work unchanged.

**Enable with `--tg-news`:**

```bash
# Analysis with TG news from 金十bot (default channel)
python scripts/run_analysis_trade_pipeline.py --tg-news

# Custom channels and limits
python scripts/run_analysis_trade_pipeline.py --tg-news --tg-channels jinshishuju_bot --tg-limit 100

# With lower TG weight (default 0.8)
python scripts/run_analysis_trade_pipeline.py --tg-news --tg-weight 0.6
```

**Or configure in `config.yaml`:**

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

**Standalone testing:**

```bash
# Test TG news fetch independently
python scripts/query_tg_news.py --channels jinshishuju_bot --limit 30

# Filter to specific tickers
python scripts/query_tg_news.py --channels jinshishuju_bot --tickers USO,GLD,SPY --json
```

**How sentiment scoring works for Chinese news:**
- Keyword dictionary with ~60 bullish/bearish terms and intensity weights
- 金十bot tags (e.g. `【原油】`, `【伊朗局势】`) mapped directly to sector/ticker
- Star ratings (`★` / `★★`) used as importance weights
- Scores multiplied by `tg_weight` (default 0.8) before merging with AV data

## Known limitations (0.1.0)

- **No media download yet** — text only. Photos, videos, voice messages return empty text.
- **No reaction/view analytics beyond `views` count** — forwards, reactions not exposed.
- **Hard pagination limit at 500** when using `since` mode — enough for most use cases, but heavy backfills need multiple `offset_date` calls.

## Roadmap

**Read coverage**
- [ ] Media download (photos, documents, voice)
- [ ] Reactions and forward chain
- [ ] Topic/thread support in forum-style groups

**Performance**
- [ ] Persistent connection pooling across requests
- [ ] Optional Redis cache for frequently-read channels

**Deployment**
- [ ] Docker image with volume-mounted session file
- [ ] Remote MCP (HTTP transport) for multi-client setups

## About the author

Leo ([@runes_leo](https://x.com/runes_leo)) — AI × Crypto independent builder.

I use Claude Code to build two things:
- **Prediction market trading** on [Polymarket](https://polymarket.com/?via=runes-leo&r=runesleo&utm_source=github&utm_content=tg-reader-mcp) — quant strategies and market making.
- **Content automation** like this repo — pipelines that let one person ship at the pace of a team.

More at [leolabs.me](https://leolabs.me).

## License

MIT — see [LICENSE](./LICENSE).
