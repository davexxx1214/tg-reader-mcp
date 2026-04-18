#!/usr/bin/env python3
"""
同步 AlphaVantage 日线到 SQLite（支持单标的与批量增量）。

功能：
1) 使用 AlphaVantage TIME_SERIES_DAILY 获取美股日线
2) 数据落地到 data/stock_daily.sqlite
3) 基于数据库最新日期做增量更新
4) 自动识别最新可用交易日（周末/节假日无数据时不会误报）
5) 内置 75 次/分钟 API 限速（批量模式共享同一限速器）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import get_alphavantage_key, load_config  # noqa: E402
from query_stock_prices import DEFAULT_SYMBOLS  # noqa: E402


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SCRIPT_DIR.parent / "data" / "stock_daily.sqlite"
BASE_URL = "https://www.alphavantage.co/query"


class RateLimiter:
    """
    简单滑动窗口限速器：max_calls / period_seconds。
    """

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
    return symbol.strip().upper()


def ensure_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_daily (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'alphavantage',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stock_daily_symbol_date
        ON stock_daily(symbol, trade_date)
        """
    )
    conn.commit()
    return conn


def ensure_sync_audit_table(conn: sqlite3.Connection) -> None:
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


def get_latest_db_trade_date(conn: sqlite3.Connection, symbol: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(trade_date) FROM stock_daily WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return str(row[0])


def fetch_alpha_daily(
    symbol: str,
    api_key: str,
    rate_limiter: RateLimiter,
    outputsize: str = "full",
    timeout: int = 30,
) -> Dict[str, Any]:
    rate_limiter.wait_for_slot()
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": outputsize,  # full/compact
        "apikey": api_key,
    }
    response = requests.get(BASE_URL, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    if "Error Message" in data:
        raise RuntimeError(f"AlphaVantage error: {data['Error Message']}")
    if "Note" in data:
        raise RuntimeError(f"AlphaVantage rate-limit note: {data['Note']}")
    if "Time Series (Daily)" not in data:
        raise RuntimeError(f"Unexpected AlphaVantage payload keys: {list(data.keys())}")
    return data


def parse_daily_rows(symbol: str, payload: Dict[str, Any]) -> Tuple[List[Tuple[Any, ...]], Optional[str]]:
    series = payload.get("Time Series (Daily)", {}) or {}
    if not isinstance(series, dict) or not series:
        return [], None

    now_iso = utc_now_iso()
    rows: List[Tuple[Any, ...]] = []
    latest_available_date: Optional[str] = None

    # AlphaVantage 日期字符串天然可按字典序排序
    for trade_date in sorted(series.keys()):
        bar = series.get(trade_date, {})
        try:
            o = float(bar["1. open"])
            h = float(bar["2. high"])
            l = float(bar["3. low"])
            c = float(bar["4. close"])
            v = int(float(bar["5. volume"]))
        except (KeyError, TypeError, ValueError):
            continue

        rows.append(
            (
                symbol,
                trade_date,
                o,
                h,
                l,
                c,
                v,
                "alphavantage",
                now_iso,
                now_iso,
            )
        )
        latest_available_date = trade_date

    return rows, latest_available_date


def upsert_incremental_rows(
    conn: sqlite3.Connection,
    symbol: str,
    rows: List[Tuple[Any, ...]],
    latest_db_date: Optional[str],
) -> int:
    if not rows:
        return 0

    if latest_db_date:
        rows = [r for r in rows if str(r[1]) > latest_db_date]
    if not rows:
        return 0

    conn.executemany(
        """
        INSERT INTO stock_daily (
            symbol, trade_date, open, high, low, close, volume, source, created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, trade_date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            source = excluded.source,
            updated_at_utc = excluded.updated_at_utc
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _count_incremental_rows(
    rows: List[Tuple[Any, ...]],
    latest_db_date: Optional[str],
) -> int:
    if not rows:
        return 0
    if latest_db_date is None:
        return len(rows)
    return sum(1 for r in rows if str(r[1]) > latest_db_date)


def sync_symbol_with_resources(
    conn: sqlite3.Connection,
    limiter: RateLimiter,
    symbol: str,
    api_key: str,
) -> Dict[str, Any]:
    symbol = normalize_symbol(symbol)
    latest_db_date = get_latest_db_trade_date(conn, symbol)
    # 策略：
    # - 首次同步（无历史） -> full
    # - 后续增量（有历史） -> compact
    outputsize = "full" if latest_db_date is None else "compact"
    payload = fetch_alpha_daily(symbol, api_key=api_key, rate_limiter=limiter, outputsize=outputsize)
    rows, latest_available_date = parse_daily_rows(symbol, payload)
    pending_incremental = _count_incremental_rows(rows, latest_db_date)

    # 兜底：若本地有历史，且 API 显示有更新，但 compact 未覆盖到缺失区间，则自动回退 full 一次
    if (
        latest_db_date is not None
        and latest_available_date is not None
        and latest_available_date > latest_db_date
        and pending_incremental == 0
        and outputsize == "compact"
    ):
        payload = fetch_alpha_daily(symbol, api_key=api_key, rate_limiter=limiter, outputsize="full")
        rows, latest_available_date = parse_daily_rows(symbol, payload)
        pending_incremental = _count_incremental_rows(rows, latest_db_date)
        outputsize = "full(fallback)"

    inserted = upsert_incremental_rows(conn, symbol, rows, latest_db_date)

    latest_after = get_latest_db_trade_date(conn, symbol)
    print(f"Symbol: {symbol}")
    print(f"Fetch mode: {outputsize}")
    print(f"Latest DB before: {latest_db_date or 'N/A'}")
    print(f"Latest API trading date: {latest_available_date or 'N/A'}")
    print(f"Inserted/updated rows: {inserted}")
    print(f"Latest DB after: {latest_after or 'N/A'}")

    if latest_db_date and latest_available_date and latest_db_date >= latest_available_date:
        print("Status: up-to-date (weekend/holiday without new bar is expected)")
    elif inserted == 0:
        print("Status: no new rows")
    else:
        print("Status: incremental update applied")
    print("-" * 60)
    return {
        "status": "ok",
        "symbol": symbol,
        "inserted_rows": inserted,
        "fetch_mode": outputsize,
        "error_message": None,
    }


def _chunk_symbols(symbols: List[str], batch_size: int) -> List[List[str]]:
    if batch_size <= 0:
        return [symbols]
    return [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]


def sync_symbols(
    symbols: List[str],
    db_path: Path,
    api_key: str,
    max_calls_per_minute: int,
    batch_size: int = 0,
    with_audit: bool = False,
    job_name: str = "daily_sync",
) -> int:
    symbols = [normalize_symbol(s) for s in symbols if str(s).strip()]
    if not symbols:
        raise ValueError("symbols is empty")

    conn = ensure_db(db_path)
    limiter = RateLimiter(max_calls=max_calls_per_minute, period_seconds=60.0)
    if with_audit:
        ensure_sync_audit_table(conn)

    total_inserted = 0
    try:
        print(f"DB file: {db_path}")
        print(f"Symbols count: {len(symbols)}")
        print(f"Rate limit: {max_calls_per_minute} calls/min")
        if batch_size > 0:
            print(f"Batch size: {batch_size}")
        print(f"Audit enabled: {'yes' if with_audit else 'no'}")
        print("=" * 60)

        chunks = _chunk_symbols(symbols, batch_size)
        completed = 0
        for chunk_idx, chunk in enumerate(chunks, 1):
            if batch_size > 0:
                print(f"[Batch {chunk_idx}/{len(chunks)}] symbols={len(chunk)}")
            for symbol in chunk:
                completed += 1
                print(f"[{completed}/{len(symbols)}] Syncing {symbol} ...")
                started = utc_now_iso()
                try:
                    result = sync_symbol_with_resources(
                        conn=conn,
                        limiter=limiter,
                        symbol=symbol,
                        api_key=api_key,
                    )
                    inserted_rows = int(result.get("inserted_rows", 0))
                    total_inserted += inserted_rows
                    if with_audit:
                        insert_sync_audit(
                            conn=conn,
                            job_name=job_name,
                            symbol=symbol,
                            started_at_utc=started,
                            finished_at_utc=utc_now_iso(),
                            status="ok",
                            inserted_rows=inserted_rows,
                            fetch_mode=str(result.get("fetch_mode") or ""),
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
                            fetch_mode=None,
                            error_message=str(exc),
                        )
        print(f"Batch done. Total inserted/updated rows: {total_inserted}")
        return total_inserted
    finally:
        conn.close()


def parse_symbols_csv(csv_text: str) -> List[str]:
    return [normalize_symbol(x) for x in str(csv_text or "").split(",") if str(x).strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步 AlphaVantage 日线到 SQLite（支持单标的与批量增量）")
    parser.add_argument("--symbol", help="单只股票代码，例如 AAPL")
    parser.add_argument("--symbols", help="多只股票代码，逗号分隔，例如 AAPL,MSFT,NVDA")
    parser.add_argument("--default-pool", action="store_true", help="使用默认101股票池（DEFAULT_SYMBOLS）")
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite 文件路径，默认 {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--max-calls-per-minute",
        type=int,
        default=75,
        help="AlphaVantage 每分钟最大调用数，默认 75",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="批次大小（0表示不分批），示例 20",
    )
    parser.add_argument("--with-audit", action="store_true", help="将每个symbol同步结果写入sync_audit表")
    parser.add_argument("--job-name", default="daily_sync", help="审计任务名（with-audit时使用）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    api_key = get_alphavantage_key(config)

    db_path = Path(args.db_path)
    max_calls = max(1, int(args.max_calls_per_minute))
    single = normalize_symbol(args.symbol) if args.symbol else ""
    batch = parse_symbols_csv(args.symbols) if args.symbols else []
    pool = DEFAULT_SYMBOLS.copy() if args.default_pool else []

    symbols: List[str] = []
    symbols.extend(pool)
    if single:
        symbols.append(single)
    symbols.extend(batch)
    # 去重并保序
    deduped: List[str] = []
    seen = set()
    for s in symbols:
        if s not in seen:
            deduped.append(s)
            seen.add(s)

    if not deduped:
        print("❌ 请提供 --symbol / --symbols / --default-pool")
        raise SystemExit(1)

    sync_symbols(
        deduped,
        db_path=db_path,
        api_key=api_key,
        max_calls_per_minute=max_calls,
        batch_size=max(0, int(args.batch_size)),
        with_audit=bool(args.with_audit),
        job_name=str(args.job_name),
    )


if __name__ == "__main__":
    main()
