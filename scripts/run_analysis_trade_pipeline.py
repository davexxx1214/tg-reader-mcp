#!/usr/bin/env python3
"""Run the TACO + Jin10 QQQ-only data, signal, and rebalance pipeline."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from _config import get_taco_strategy_config, load_config
from collect_jin10_messages import run_incremental
from query_alpaca_account import (
    get_account_info,
    get_alpaca_client,
    get_positions,
    persist_account_snapshot,
)
from query_stock_prices import _fetch_alpaca_snapshots
from sync_alpha_daily_to_sqlite import DEFAULT_DB_PATH as DEFAULT_PRICE_DB
from sync_alpha_daily_to_sqlite import sync_symbols
from sync_taco_data import load_taco_rows, sync_taco
from taco_strategy import (
    build_rebalance_plan,
    calculate_taco_jin10_signal,
    load_jin10_messages,
    target_weights_for_exposure,
)


def _resolve_root_path(value: str) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else ROOT_DIR / path


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _latest_local_account() -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    balance_path = ROOT_DIR / "data" / "balance" / "balance.jsonl"
    for row in reversed(_read_jsonl(balance_path)):
        account = row.get("account")
        positions = row.get("positions")
        if isinstance(account, dict) and isinstance(positions, list):
            return dict(account), list(positions)
    return {}, []


def validate_run_options(*, execute_trades: bool, skip_account_refresh: bool, signal_date: str = "") -> None:
    if execute_trades and skip_account_refresh:
        raise ValueError("Live execution requires a fresh Alpaca account snapshot")
    if execute_trades and signal_date:
        raise ValueError("Live execution cannot use a manually backdated signal date")


def _execute_trade_plan(trade_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    exec_script = SCRIPT_DIR / "execute_alpaca_trade.py"
    results: List[Dict[str, Any]] = []
    for item in trade_plan:
        action = str(item.get("action", "")).lower().strip()
        symbol = str(item.get("symbol", "")).upper().strip()
        qty = int(item.get("qty", 0))
        if action not in {"buy", "sell"} or not symbol or qty <= 0:
            results.append({"status": "skipped", "input": item, "reason": "invalid action/symbol/qty"})
            continue

        completed = subprocess.run(
            [
                sys.executable,
                str(exec_script),
                "--action",
                action,
                "--symbol",
                symbol,
                "--qty",
                str(qty),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            results.append(
                {
                    "status": "failed",
                    "input": item,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            break
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            results.append(
                {
                    "status": "failed_non_json",
                    "input": item,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            break
        results.append({"status": "ok", "trade": payload})
        if str(payload.get("status", "")).lower() != "filled":
            break
    return results


def _select_fundamentals_sync_symbols(
    *,
    db_path: Path,
    symbols: List[str],
    stale_after_days: int = 7,
    min_quarterly_rows: int = 5,
    as_of_date: Optional[date] = None,
) -> List[str]:
    """Compatibility helper retained for the repository's legacy strategy tests."""
    normalized = list(dict.fromkeys(str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()))
    if not normalized or not db_path.exists():
        return normalized
    today = as_of_date or datetime.now().date()
    stale: List[str] = []
    conn = sqlite3.connect(str(db_path))
    try:
        for symbol in normalized:
            overview = conn.execute(
                "SELECT as_of_date FROM fundamentals_overview_daily WHERE symbol = ? ORDER BY as_of_date DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM fundamentals_quarterly WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            try:
                overview_date = date.fromisoformat(str(overview[0])) if overview and overview[0] else None
            except ValueError:
                overview_date = None
            quarterly_count = int(count[0]) if count else 0
            is_stale = overview_date is None or (today - overview_date).days >= max(int(stale_after_days), 0)
            if is_stale or quarterly_count < max(int(min_quarterly_rows), 1):
                stale.append(symbol)
    finally:
        conn.close()
    return stale


def _execution_date(value: str) -> str:
    if value:
        return date.fromisoformat(value).isoformat()
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    return datetime.now().date().isoformat()


def _sync_jin10(*, db_path: Path, channel: str, limit: int, session_path: str) -> Dict[str, Any]:
    args = SimpleNamespace(
        db=str(db_path),
        channel=channel,
        session=session_path or None,
        limit=max(int(limit), 1),
    )
    return run_incremental(args)


def _sync_qqq_price(*, symbol: str, config: Mapping[str, Any], calls_per_minute: int) -> Dict[str, Any]:
    api_key = str(config.get("alphavantage", {}).get("api_key", "")).strip()
    if not api_key or api_key.startswith("your_"):
        raise RuntimeError("alphavantage.api_key is required to sync QQQ prices")
    inserted = sync_symbols(
        symbols=[symbol],
        db_path=Path(DEFAULT_PRICE_DB),
        api_key=api_key,
        max_calls_per_minute=max(int(calls_per_minute), 1),
        batch_size=0,
        with_audit=False,
        job_name="taco_jin10_qqq_pipeline",
    )
    return {"symbol": symbol, "inserted_rows": inserted, "db": str(Path(DEFAULT_PRICE_DB).resolve())}


def _latest_sqlite_price(db_path: Path, symbol: str) -> Optional[float]:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT close FROM stock_daily WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0]) if row and row[0] is not None else None


def _load_prices(
    symbol: str,
    positions: Iterable[Mapping[str, Any]],
    *,
    require_live: bool = False,
) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    try:
        snapshot = _fetch_alpaca_snapshots([symbol], timeout_seconds=30.0)
        if symbol in snapshot and float(snapshot[symbol].get("price") or 0.0) > 0:
            prices[symbol] = float(snapshot[symbol]["price"])
    except Exception:
        pass
    if require_live and symbol not in prices:
        raise RuntimeError(f"Live execution requires a live Alpaca quote for {symbol}")
    if symbol not in prices:
        cached = _latest_sqlite_price(Path(DEFAULT_PRICE_DB), symbol)
        if cached:
            prices[symbol] = cached
    for position in positions:
        position_symbol = str(position.get("symbol", "")).upper().strip()
        current_price = float(position.get("current_price") or 0.0)
        if position_symbol and current_price > 0:
            prices.setdefault(position_symbol, current_price)
    return prices


def _load_account(*, skip_refresh: bool, execute_trades: bool) -> tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    if skip_refresh:
        account, positions = _latest_local_account()
        return account, positions, {"status": "local_snapshot"}
    try:
        client = get_alpaca_client()
        if client is None:
            raise RuntimeError("Alpaca client unavailable")
        account = get_account_info(client)
        positions = get_positions(client)
        records = persist_account_snapshot(
            account,
            positions,
            source="run_analysis_trade_pipeline",
            action="pre_taco_qqq_snapshot",
        )
        return account, positions, {"status": "fresh", "records": records}
    except Exception:
        if execute_trades:
            raise
        account, positions = _latest_local_account()
        return account, positions, {"status": "fallback_local"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run TACO + Jin10 QQQ-only timing pipeline")
    parser.add_argument("--execute-trades", action="store_true", help="Submit live Alpaca orders; default is dry-run")
    parser.add_argument("--skip-account-refresh", action="store_true")
    parser.add_argument("--skip-data-sync", action="store_true", help="Use existing TACO, Jin10, and QQQ data")
    parser.add_argument("--skip-taco-sync", action="store_true")
    parser.add_argument("--skip-jin10-sync", action="store_true")
    parser.add_argument("--skip-price-sync", action="store_true")
    parser.add_argument("--jin10-limit", type=int, default=500)
    parser.add_argument("--av-calls-per-minute", type=int, default=75)
    parser.add_argument("--signal-date", default="", help="Execution date for deterministic dry-run only")
    parser.add_argument(
        "--output-file",
        default="data/taco_qqq_pipeline_latest.json",
        help="JSON audit output path",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    validate_run_options(
        execute_trades=bool(args.execute_trades),
        skip_account_refresh=bool(args.skip_account_refresh),
        signal_date=str(args.signal_date or ""),
    )
    config = load_config()
    strategy = get_taco_strategy_config(config)
    if not strategy["enabled"]:
        raise RuntimeError("taco_strategy.enabled must be true")

    symbol = strategy["symbol"]
    taco_db = _resolve_root_path(strategy["taco_db"])
    jin10_db = _resolve_root_path(strategy["jin10_db"])
    execution_date = _execution_date(args.signal_date)
    skip_all = bool(args.skip_data_sync)
    sync_results: Dict[str, Any] = {}

    if skip_all or args.skip_taco_sync:
        sync_results["taco"] = {"status": "skipped", "db": str(taco_db)}
    else:
        sync_results["taco"] = {
            "status": "ok",
            **sync_taco(db_path=taco_db, url=strategy["dashboard_url"]),
        }

    if skip_all or args.skip_jin10_sync:
        sync_results["jin10"] = {"status": "skipped", "db": str(jin10_db)}
    else:
        telegram = config.get("telegram", {}) if isinstance(config, dict) else {}
        sync_results["jin10"] = {
            "status": "ok",
            **_sync_jin10(
                db_path=jin10_db,
                channel=strategy["jin10_channel"],
                limit=args.jin10_limit,
                session_path=str(telegram.get("session_path") or ""),
            ),
        }

    if skip_all or args.skip_price_sync:
        sync_results["qqq_price"] = {"status": "skipped", "db": str(Path(DEFAULT_PRICE_DB))}
    else:
        sync_results["qqq_price"] = {
            "status": "ok",
            **_sync_qqq_price(
                symbol=symbol,
                config=config,
                calls_per_minute=args.av_calls_per_minute,
            ),
        }

    taco_rows = load_taco_rows(taco_db)
    jin10_rows = load_jin10_messages(
        jin10_db,
        end_date_inclusive=(date.fromisoformat(execution_date)).isoformat(),
        channel=strategy["jin10_channel"],
    )
    signal = calculate_taco_jin10_signal(
        taco_rows,
        jin10_rows,
        execution_date=execution_date,
        smoothing_days=strategy["smoothing_days"],
        news_half_life_days=strategy["news_half_life_days"],
        risk_beta=strategy["risk_beta"],
        relief_beta=strategy["relief_beta"],
        buy_threshold=strategy["buy_threshold"],
        max_data_age_days=strategy["max_data_age_days"],
        require_fresh_news=True,
    )
    target_weights = target_weights_for_exposure(symbol, signal["exposure"])

    account, positions, account_refresh = _load_account(
        skip_refresh=bool(args.skip_account_refresh),
        execute_trades=bool(args.execute_trades),
    )
    if float(account.get("equity") or account.get("portfolio_value") or 0.0) <= 0:
        raise RuntimeError("No valid Alpaca account snapshot is available")
    prices = _load_prices(symbol, positions, require_live=bool(args.execute_trades))
    trade_plan = build_rebalance_plan(
        account=account,
        positions=positions,
        prices=prices,
        target_weights=target_weights,
    )
    trade_results = _execute_trade_plan(trade_plan) if args.execute_trades else []

    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "live" if args.execute_trades else "dry_run",
        "data_sync": sync_results,
        "account_refresh": account_refresh,
        "strategy_config": strategy,
        "signal": signal,
        "target_weights": target_weights,
        "prices": prices,
        "trade_plan": trade_plan,
        "trade_results": trade_results,
    }
    output_path = _resolve_root_path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Result written to: {output_path}")


if __name__ == "__main__":
    main()
