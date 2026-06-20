#!/usr/bin/env python3
"""Download TACO history and persist it into SQLite for live use/backtests."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping

from taco_strategy import DEFAULT_DASHBOARD_URL, fetch_taco_dashboard


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "taco_daily.sqlite"


def init_taco_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS taco_daily (
            trade_date TEXT PRIMARY KEY,
            taco_index REAL NOT NULL,
            event_strength_score REAL NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL
        )
        """
    )
    conn.commit()


@contextmanager
def open_taco_db(path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        init_taco_db(conn)
        yield conn
    finally:
        conn.close()


def upsert_taco_rows(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    written = 0
    for row in rows:
        trade_date = str(row.get("date") or "")
        if not trade_date:
            continue
        value = float(row.get("value"))
        event_score = float(row.get("event_strength_score") or 0.0)
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else dict(row)
        conn.execute(
            """
            INSERT INTO taco_daily (
                trade_date, taco_index, event_strength_score, raw_json, fetched_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                taco_index = excluded.taco_index,
                event_strength_score = excluded.event_strength_score,
                raw_json = excluded.raw_json,
                fetched_at_utc = excluded.fetched_at_utc
            """,
            (trade_date, value, event_score, json.dumps(raw, ensure_ascii=False), fetched_at),
        )
        written += 1
    conn.commit()
    return written


def load_taco_rows(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    start_date: str = "",
    end_date_inclusive: str = "",
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if start_date:
        clauses.append("trade_date >= ?")
        params.append(start_date)
    if end_date_inclusive:
        clauses.append("trade_date <= ?")
        params.append(end_date_inclusive)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            f"""
            SELECT trade_date, taco_index, event_strength_score
            FROM taco_daily
            {where}
            ORDER BY trade_date
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        {"date": str(row[0]), "value": float(row[1]), "event_strength_score": float(row[2])}
        for row in rows
    ]


def sync_taco(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    url: str = DEFAULT_DASHBOARD_URL,
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    rows = fetch_taco_dashboard(url, timeout_seconds=timeout_seconds)
    with open_taco_db(db_path) as conn:
        written = upsert_taco_rows(conn, rows)
        total = int(conn.execute("SELECT COUNT(*) FROM taco_daily").fetchone()[0])
        latest = conn.execute(
            "SELECT trade_date, taco_index FROM taco_daily ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
    return {
        "db": str(Path(db_path).resolve()),
        "fetched": len(rows),
        "written": written,
        "total_rows": total,
        "latest_date": str(latest[0]) if latest else None,
        "latest_taco": float(latest[1]) if latest else None,
        "source_url": url,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download TACO history into SQLite")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = sync_taco(
        db_path=Path(args.db),
        url=args.url,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
