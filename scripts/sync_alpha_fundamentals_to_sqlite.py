#!/usr/bin/env python3
"""
同步 AlphaVantage 基本面到 SQLite（5年季度窗口）。

目标：
1) 拉取 overview + quarterly fundamentals（收入、利润、现金流、资产负债）
2) 入库到同一 SQLite（data/stock_daily.sqlite）
3) 支持单股票、批量股票、默认101池
4) 全局限速 75 次/分钟（可调）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import get_alphavantage_key, load_config  # noqa: E402
from query_stock_prices import DEFAULT_SYMBOLS  # noqa: E402


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SCRIPT_DIR.parent / "data" / "stock_daily.sqlite"
BASE_URL = "https://www.alphavantage.co/query"


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._timestamps: Deque[float] = deque()

    def wait_for_slot(self) -> None:
        now = time.time()
        while self._timestamps and now - self._timestamps[0] >= self.period_seconds:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_calls:
            sleep_seconds = self.period_seconds - (now - self._timestamps[0]) + 0.01
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        self._timestamps.append(time.time())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def parse_symbols_csv(csv_text: str) -> List[str]:
    return [normalize_symbol(x) for x in str(csv_text or "").split(",") if str(x).strip()]


def _to_float(value: Any) -> Optional[float]:
    if value in (None, "", "None", "null", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def ensure_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals_quarterly (
            symbol TEXT NOT NULL,
            fiscal_date_ending TEXT NOT NULL,
            reported_currency TEXT,
            revenue REAL,
            operating_income REAL,
            net_income REAL,
            operating_cashflow REAL,
            capital_expenditures REAL,
            free_cashflow REAL,
            change_in_receivables REAL,
            change_in_inventory REAL,
            total_assets REAL,
            total_liabilities REAL,
            total_shareholder_equity REAL,
            cash_and_short_term_investments REAL,
            current_debt REAL,
            long_term_debt REAL,
            source TEXT NOT NULL DEFAULT 'alphavantage',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (symbol, fiscal_date_ending)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fq_symbol_date
        ON fundamentals_quarterly(symbol, fiscal_date_ending)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals_overview_daily (
            symbol TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            currency TEXT,
            market_cap REAL,
            pe_ratio REAL,
            shares_outstanding REAL,
            beta REAL,
            analyst_target_price REAL,
            short_ratio REAL,
            shares_short REAL,
            shares_short_prior_month REAL,
            revenue_ttm REAL,
            profit_margin REAL,
            operating_margin_ttm REAL,
            roe_ttm REAL,
            roa_ttm REAL,
            source TEXT NOT NULL DEFAULT 'alphavantage',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (symbol, as_of_date)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fod_symbol_date
        ON fundamentals_overview_daily(symbol, as_of_date)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            inserted_rows INTEGER NOT NULL DEFAULT 0,
            fetch_mode TEXT,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sync_audit_job_symbol
        ON sync_audit(job_name, symbol)
        """
    )
    conn.commit()


def insert_sync_audit(
    conn: sqlite3.Connection,
    job_name: str,
    symbol: str,
    started_at_utc: str,
    finished_at_utc: str,
    status: str,
    inserted_rows: int,
    fetch_mode: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_audit (
            job_name, symbol, started_at_utc, finished_at_utc, status, inserted_rows, fetch_mode, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_name, symbol, started_at_utc, finished_at_utc, status, inserted_rows, fetch_mode, error_message),
    )
    conn.commit()


def fetch_alpha(function_name: str, symbol: str, api_key: str, limiter: RateLimiter, timeout: int = 30) -> Dict[str, Any]:
    limiter.wait_for_slot()
    params = {
        "function": function_name,
        "symbol": symbol,
        "apikey": api_key,
    }
    response = requests.get(BASE_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if "Error Message" in payload:
        raise RuntimeError(f"{function_name} error: {payload['Error Message']}")
    if "Note" in payload:
        raise RuntimeError(f"{function_name} rate-limit note: {payload['Note']}")
    return payload


def _merge_quarterly_rows(
    symbol: str,
    income_payload: Dict[str, Any],
    balance_payload: Dict[str, Any],
    cashflow_payload: Dict[str, Any],
    years: int,
) -> List[Dict[str, Any]]:
    cutoff = datetime.now() - timedelta(days=max(1, years) * 365)
    by_date: Dict[str, Dict[str, Any]] = {}

    for row in income_payload.get("quarterlyReports", []) or []:
        d = str(row.get("fiscalDateEnding", "")).strip()
        if not d:
            continue
        dt = _parse_date(d)
        if dt is None or dt < cutoff:
            continue
        item = by_date.setdefault(d, {"fiscal_date_ending": d})
        item["reported_currency"] = row.get("reportedCurrency")
        item["revenue"] = _to_float(row.get("totalRevenue"))
        item["operating_income"] = _to_float(row.get("operatingIncome"))
        item["net_income"] = _to_float(row.get("netIncome"))

    for row in balance_payload.get("quarterlyReports", []) or []:
        d = str(row.get("fiscalDateEnding", "")).strip()
        if not d:
            continue
        dt = _parse_date(d)
        if dt is None or dt < cutoff:
            continue
        item = by_date.setdefault(d, {"fiscal_date_ending": d})
        item["reported_currency"] = item.get("reported_currency") or row.get("reportedCurrency")
        item["total_assets"] = _to_float(row.get("totalAssets"))
        item["total_liabilities"] = _to_float(row.get("totalLiabilities"))
        item["total_shareholder_equity"] = _to_float(row.get("totalShareholderEquity"))
        item["cash_and_short_term_investments"] = _to_float(row.get("cashAndShortTermInvestments"))
        item["current_debt"] = _to_float(row.get("currentDebt"))
        item["long_term_debt"] = _to_float(row.get("longTermDebt"))

    for row in cashflow_payload.get("quarterlyReports", []) or []:
        d = str(row.get("fiscalDateEnding", "")).strip()
        if not d:
            continue
        dt = _parse_date(d)
        if dt is None or dt < cutoff:
            continue
        item = by_date.setdefault(d, {"fiscal_date_ending": d})
        item["reported_currency"] = item.get("reported_currency") or row.get("reportedCurrency")
        operating_cf = _to_float(row.get("operatingCashflow"))
        capex = _to_float(row.get("capitalExpenditures"))
        free_cf = None
        if operating_cf is not None and capex is not None:
            free_cf = operating_cf + capex  # capex 通常为负数
        item["operating_cashflow"] = operating_cf
        item["capital_expenditures"] = capex
        item["free_cashflow"] = free_cf
        item["change_in_receivables"] = _to_float(row.get("changeInReceivables"))
        item["change_in_inventory"] = _to_float(row.get("changeInInventory"))

    rows: List[Dict[str, Any]] = []
    now_iso = utc_now_iso()
    for fiscal_date in sorted(by_date.keys(), reverse=True):
        row = by_date[fiscal_date]
        rows.append(
            {
                "symbol": symbol,
                "fiscal_date_ending": fiscal_date,
                "reported_currency": row.get("reported_currency"),
                "revenue": row.get("revenue"),
                "operating_income": row.get("operating_income"),
                "net_income": row.get("net_income"),
                "operating_cashflow": row.get("operating_cashflow"),
                "capital_expenditures": row.get("capital_expenditures"),
                "free_cashflow": row.get("free_cashflow"),
                "change_in_receivables": row.get("change_in_receivables"),
                "change_in_inventory": row.get("change_in_inventory"),
                "total_assets": row.get("total_assets"),
                "total_liabilities": row.get("total_liabilities"),
                "total_shareholder_equity": row.get("total_shareholder_equity"),
                "cash_and_short_term_investments": row.get("cash_and_short_term_investments"),
                "current_debt": row.get("current_debt"),
                "long_term_debt": row.get("long_term_debt"),
                "source": "alphavantage",
                "created_at_utc": now_iso,
                "updated_at_utc": now_iso,
            }
        )
    return rows


def upsert_quarterly(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    payload = [
        (
            r["symbol"],
            r["fiscal_date_ending"],
            r.get("reported_currency"),
            r.get("revenue"),
            r.get("operating_income"),
            r.get("net_income"),
            r.get("operating_cashflow"),
            r.get("capital_expenditures"),
            r.get("free_cashflow"),
            r.get("change_in_receivables"),
            r.get("change_in_inventory"),
            r.get("total_assets"),
            r.get("total_liabilities"),
            r.get("total_shareholder_equity"),
            r.get("cash_and_short_term_investments"),
            r.get("current_debt"),
            r.get("long_term_debt"),
            r.get("source", "alphavantage"),
            r["created_at_utc"],
            r["updated_at_utc"],
        )
        for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO fundamentals_quarterly (
            symbol, fiscal_date_ending, reported_currency, revenue, operating_income, net_income,
            operating_cashflow, capital_expenditures, free_cashflow, change_in_receivables, change_in_inventory,
            total_assets, total_liabilities, total_shareholder_equity, cash_and_short_term_investments,
            current_debt, long_term_debt, source, created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, fiscal_date_ending) DO UPDATE SET
            reported_currency = excluded.reported_currency,
            revenue = excluded.revenue,
            operating_income = excluded.operating_income,
            net_income = excluded.net_income,
            operating_cashflow = excluded.operating_cashflow,
            capital_expenditures = excluded.capital_expenditures,
            free_cashflow = excluded.free_cashflow,
            change_in_receivables = excluded.change_in_receivables,
            change_in_inventory = excluded.change_in_inventory,
            total_assets = excluded.total_assets,
            total_liabilities = excluded.total_liabilities,
            total_shareholder_equity = excluded.total_shareholder_equity,
            cash_and_short_term_investments = excluded.cash_and_short_term_investments,
            current_debt = excluded.current_debt,
            long_term_debt = excluded.long_term_debt,
            source = excluded.source,
            updated_at_utc = excluded.updated_at_utc
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def upsert_overview(conn: sqlite3.Connection, symbol: str, overview: Dict[str, Any]) -> int:
    now = datetime.now(timezone.utc)
    as_of_date = now.date().isoformat()
    now_iso = now.replace(microsecond=0).isoformat()

    conn.execute(
        """
        INSERT INTO fundamentals_overview_daily (
            symbol, as_of_date, currency, market_cap, pe_ratio, shares_outstanding, beta, analyst_target_price,
            short_ratio, shares_short, shares_short_prior_month, revenue_ttm, profit_margin, operating_margin_ttm,
            roe_ttm, roa_ttm, source, created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, as_of_date) DO UPDATE SET
            currency = excluded.currency,
            market_cap = excluded.market_cap,
            pe_ratio = excluded.pe_ratio,
            shares_outstanding = excluded.shares_outstanding,
            beta = excluded.beta,
            analyst_target_price = excluded.analyst_target_price,
            short_ratio = excluded.short_ratio,
            shares_short = excluded.shares_short,
            shares_short_prior_month = excluded.shares_short_prior_month,
            revenue_ttm = excluded.revenue_ttm,
            profit_margin = excluded.profit_margin,
            operating_margin_ttm = excluded.operating_margin_ttm,
            roe_ttm = excluded.roe_ttm,
            roa_ttm = excluded.roa_ttm,
            source = excluded.source,
            updated_at_utc = excluded.updated_at_utc
        """,
        (
            symbol,
            as_of_date,
            overview.get("Currency"),
            _to_float(overview.get("MarketCapitalization")),
            _to_float(overview.get("PERatio")),
            _to_float(overview.get("SharesOutstanding")),
            _to_float(overview.get("Beta")),
            _to_float(overview.get("AnalystTargetPrice")),
            _to_float(overview.get("ShortRatio")),
            _to_float(overview.get("SharesShort")),
            _to_float(overview.get("SharesShortPriorMonth")),
            _to_float(overview.get("RevenueTTM")),
            _to_float(overview.get("ProfitMargin")),
            _to_float(overview.get("OperatingMarginTTM")),
            _to_float(overview.get("ReturnOnEquityTTM")),
            _to_float(overview.get("ReturnOnAssetsTTM")),
            "alphavantage",
            now_iso,
            now_iso,
        ),
    )
    conn.commit()
    return 1


def _chunk_symbols(symbols: List[str], batch_size: int) -> List[List[str]]:
    if batch_size <= 0:
        return [symbols]
    return [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]


def sync_symbol(
    conn: sqlite3.Connection,
    limiter: RateLimiter,
    symbol: str,
    api_key: str,
    years: int,
) -> Dict[str, Any]:
    symbol = normalize_symbol(symbol)
    overview = fetch_alpha("OVERVIEW", symbol, api_key=api_key, limiter=limiter)
    income = fetch_alpha("INCOME_STATEMENT", symbol, api_key=api_key, limiter=limiter)
    balance = fetch_alpha("BALANCE_SHEET", symbol, api_key=api_key, limiter=limiter)
    cashflow = fetch_alpha("CASH_FLOW", symbol, api_key=api_key, limiter=limiter)

    quarterly_rows = _merge_quarterly_rows(
        symbol=symbol,
        income_payload=income,
        balance_payload=balance,
        cashflow_payload=cashflow,
        years=years,
    )
    q_count = upsert_quarterly(conn, quarterly_rows)
    o_count = upsert_overview(conn, symbol=symbol, overview=overview)

    print(f"Symbol: {symbol}")
    print(f"Quarterly rows upserted: {q_count}")
    print(f"Overview rows upserted: {o_count}")
    print("-" * 60)
    return {"symbol": symbol, "quarterly_rows": q_count, "overview_rows": o_count}


def run_batch(
    symbols: List[str],
    db_path: Path,
    api_key: str,
    max_calls_per_minute: int,
    years: int,
    batch_size: int,
    with_audit: bool,
    job_name: str,
) -> None:
    symbols = [normalize_symbol(s) for s in symbols if str(s).strip()]
    if not symbols:
        raise ValueError("symbols is empty")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    ensure_db(conn)
    limiter = RateLimiter(max_calls=max_calls_per_minute, period_seconds=60.0)

    try:
        print(f"DB file: {db_path}")
        print(f"Symbols count: {len(symbols)}")
        print(f"Rate limit: {max_calls_per_minute} calls/min")
        print(f"Quarterly lookback years: {years}")
        print(f"Batch size: {batch_size if batch_size > 0 else 'no chunk'}")
        print(f"Audit enabled: {'yes' if with_audit else 'no'}")
        print("=" * 60)

        chunks = _chunk_symbols(symbols, batch_size)
        completed = 0
        total_quarterly = 0
        total_overview = 0

        for chunk_idx, chunk in enumerate(chunks, 1):
            if batch_size > 0:
                print(f"[Batch {chunk_idx}/{len(chunks)}] symbols={len(chunk)}")
            for symbol in chunk:
                completed += 1
                print(f"[{completed}/{len(symbols)}] Syncing {symbol} ...")
                started = utc_now_iso()
                try:
                    result = sync_symbol(
                        conn=conn,
                        limiter=limiter,
                        symbol=symbol,
                        api_key=api_key,
                        years=years,
                    )
                    total_quarterly += int(result.get("quarterly_rows", 0))
                    total_overview += int(result.get("overview_rows", 0))
                    if with_audit:
                        insert_sync_audit(
                            conn=conn,
                            job_name=job_name,
                            symbol=symbol,
                            started_at_utc=started,
                            finished_at_utc=utc_now_iso(),
                            status="ok",
                            inserted_rows=int(result.get("quarterly_rows", 0)) + int(result.get("overview_rows", 0)),
                            fetch_mode="OVERVIEW+INCOME_STATEMENT+BALANCE_SHEET+CASH_FLOW",
                            error_message=None,
                        )
                except Exception as exc:
                    print(f"Symbol: {symbol}")
                    print(f"Status: failed ({exc})")
                    print("-" * 60)
                    if with_audit:
                        insert_sync_audit(
                            conn=conn,
                            job_name=job_name,
                            symbol=symbol,
                            started_at_utc=started,
                            finished_at_utc=utc_now_iso(),
                            status="failed",
                            inserted_rows=0,
                            fetch_mode="OVERVIEW+INCOME_STATEMENT+BALANCE_SHEET+CASH_FLOW",
                            error_message=str(exc),
                        )

        print("Batch done.")
        print(f"Total quarterly rows upserted: {total_quarterly}")
        print(f"Total overview rows upserted: {total_overview}")
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步 AlphaVantage 基本面到 SQLite（5年季度窗口）")
    parser.add_argument("--symbol", help="单只股票代码，例如 AAPL")
    parser.add_argument("--symbols", help="多只股票代码，逗号分隔，例如 AAPL,MSFT,NVDA")
    parser.add_argument("--default-pool", action="store_true", help="使用默认101股票池（DEFAULT_SYMBOLS）")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help=f"SQLite 文件路径，默认 {DEFAULT_DB_PATH}")
    parser.add_argument("--max-calls-per-minute", type=int, default=75, help="AlphaVantage 每分钟最大调用数，默认75")
    parser.add_argument("--years", type=int, default=5, help="季度财务回溯年数，默认5")
    parser.add_argument("--batch-size", type=int, default=0, help="批次大小（0表示不分批）")
    parser.add_argument("--with-audit", action="store_true", help="写入 sync_audit 审计记录")
    parser.add_argument("--job-name", default="fundamentals_sync", help="审计任务名")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    api_key = get_alphavantage_key(config)

    symbols: List[str] = []
    if args.default_pool:
        symbols.extend(DEFAULT_SYMBOLS)
    if args.symbol:
        symbols.append(normalize_symbol(args.symbol))
    if args.symbols:
        symbols.extend(parse_symbols_csv(args.symbols))

    deduped: List[str] = []
    seen = set()
    for s in symbols:
        if s not in seen:
            deduped.append(s)
            seen.add(s)

    if not deduped:
        print("❌ 请提供 --symbol / --symbols / --default-pool")
        raise SystemExit(1)

    run_batch(
        symbols=deduped,
        db_path=Path(args.db_path),
        api_key=api_key,
        max_calls_per_minute=max(1, int(args.max_calls_per_minute)),
        years=max(1, int(args.years)),
        batch_size=max(0, int(args.batch_size)),
        with_audit=bool(args.with_audit),
        job_name=str(args.job_name),
    )


if __name__ == "__main__":
    main()
