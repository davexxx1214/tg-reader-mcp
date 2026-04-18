#!/usr/bin/env python3
"""
从本地 SQLite 查询历史日线价格。

支持：
1) 单只/多只股票
2) 最近 N 天
3) 指定日期区间
4) JSON 输出
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SCRIPT_DIR.parent / "data" / "stock_daily.sqlite"


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def parse_symbols_csv(csv_text: str) -> List[str]:
    return [normalize_symbol(x) for x in str(csv_text or "").split(",") if str(x).strip()]


def resolve_date_window(days: int, start_date: str, end_date: str) -> tuple[str, str]:
    if start_date and end_date:
        return start_date, end_date

    today = date.today()
    if not end_date:
        end_date = today.isoformat()
    if not start_date:
        d = max(1, int(days))
        start_date = (today - timedelta(days=d)).isoformat()
    return start_date, end_date


def query_prices(
    conn: sqlite3.Connection,
    symbols: List[str],
    start_date: str,
    end_date: str,
    limit_per_symbol: int,
) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    conn.row_factory = sqlite3.Row
    for symbol in symbols:
        rows = conn.execute(
            """
            SELECT symbol, trade_date, open, high, low, close, volume, source, updated_at_utc
            FROM stock_daily
            WHERE symbol = ?
              AND trade_date >= ?
              AND trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (symbol, start_date, end_date, limit_per_symbol),
        ).fetchall()
        out[symbol] = [{k: row[k] for k in row.keys()} for row in rows]
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询 SQLite 历史日线价格")
    parser.add_argument("--symbol", help="单只股票，例如 AAPL")
    parser.add_argument("--symbols", help="多只股票，逗号分隔，例如 AAPL,NVDA,BABA")
    parser.add_argument("--days", type=int, default=30, help="最近 N 天（默认 30）")
    parser.add_argument("--start-date", default="", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default="", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--limit-per-symbol", type=int, default=200, help="每只股票最多返回条数，默认 200")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help=f"SQLite 文件路径，默认 {DEFAULT_DB_PATH}")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols: List[str] = []
    if args.symbol:
        symbols.append(normalize_symbol(args.symbol))
    if args.symbols:
        symbols.extend(parse_symbols_csv(args.symbols))

    deduped: List[str] = []
    seen = set()
    for s in symbols:
        if s and s not in seen:
            deduped.append(s)
            seen.add(s)
    if not deduped:
        print("❌ 请提供 --symbol 或 --symbols")
        raise SystemExit(1)

    start_date, end_date = resolve_date_window(
        days=max(1, int(args.days)),
        start_date=str(args.start_date).strip(),
        end_date=str(args.end_date).strip(),
    )

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"❌ 数据库文件不存在: {db_path}")
        raise SystemExit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        data = query_prices(
            conn=conn,
            symbols=deduped,
            start_date=start_date,
            end_date=end_date,
            limit_per_symbol=max(1, int(args.limit_per_symbol)),
        )
    finally:
        conn.close()

    output = {
        "db_path": str(db_path),
        "start_date": start_date,
        "end_date": end_date,
        "symbols": deduped,
        "prices": data,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    print(f"DB: {db_path}")
    print(f"Range: {start_date} ~ {end_date}")
    print("=" * 60)
    for symbol in deduped:
        rows = data.get(symbol, [])
        print(f"\n{symbol} (rows={len(rows)})")
        if not rows:
            print("  (no data)")
            continue
        for row in rows:
            print(
                f"  {row['trade_date']}  O:{row['open']} H:{row['high']} "
                f"L:{row['low']} C:{row['close']} V:{row['volume']}"
            )


if __name__ == "__main__":
    main()
