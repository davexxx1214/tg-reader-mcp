#!/usr/bin/env python3
"""
从本地 SQLite 查询已同步的基本面数据。

支持：
1) 查询某只股票最新 overview 快照
2) 查询最近 N 个季度财务明细
3) 可选 JSON 输出，便于后续脚本消费
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SCRIPT_DIR.parent / "data" / "stock_daily.sqlite"


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def query_latest_overview(conn: sqlite3.Connection, symbol: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
        FROM fundamentals_overview_daily
        WHERE symbol = ?
        ORDER BY as_of_date DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def query_recent_quarterly(conn: sqlite3.Connection, symbol: str, limit: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM fundamentals_quarterly
        WHERE symbol = ?
        ORDER BY fiscal_date_ending DESC
        LIMIT ?
        """,
        (symbol, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def query_sync_audit(conn: sqlite3.Connection, symbol: str, limit: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM sync_audit
        WHERE symbol = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (symbol, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询 SQLite 中已同步的基本面数据")
    parser.add_argument("--symbol", required=True, help="股票代码，例如 AAPL")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help=f"SQLite 文件路径，默认 {DEFAULT_DB_PATH}")
    parser.add_argument("--quarters", type=int, default=8, help="返回最近季度条数，默认 8")
    parser.add_argument("--audit-limit", type=int, default=5, help="返回最近审计记录条数，默认 5")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbol = str(args.symbol).strip().upper()
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"❌ 数据库文件不存在: {db_path}")
        raise SystemExit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        overview = query_latest_overview(conn, symbol)
        quarterly = query_recent_quarterly(conn, symbol, max(1, int(args.quarters)))
        audit = query_sync_audit(conn, symbol, max(1, int(args.audit_limit)))

        output = {
            "symbol": symbol,
            "db_path": str(db_path),
            "overview_latest": overview,
            "quarterly_recent": quarterly,
            "sync_audit_recent": audit,
        }

        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return

        print(f"Symbol: {symbol}")
        print(f"DB: {db_path}")
        print("=" * 60)
        print("Latest overview:")
        if overview:
            print(
                f"  as_of_date={overview.get('as_of_date')} market_cap={overview.get('market_cap')} "
                f"pe={overview.get('pe_ratio')} short_ratio={overview.get('short_ratio')}"
            )
        else:
            print("  (no data)")

        print("\nRecent quarterly:")
        if quarterly:
            for row in quarterly:
                print(
                    f"  {row.get('fiscal_date_ending')} revenue={row.get('revenue')} "
                    f"op_income={row.get('operating_income')} net_income={row.get('net_income')} "
                    f"fcf={row.get('free_cashflow')}"
                )
        else:
            print("  (no data)")

        print("\nRecent sync audit:")
        if audit:
            for row in audit:
                print(
                    f"  id={row.get('id')} status={row.get('status')} "
                    f"inserted_rows={row.get('inserted_rows')} started={row.get('started_at_utc')}"
                )
        else:
            print("  (no data)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
