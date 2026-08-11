#!/usr/bin/env python3
"""Run the causal nTACO 100/20 QQQ data, signal, and rebalance pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from _config import get_ntaco_strategy_config, load_config
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
    calculate_ntaco_signal,
    current_symbol_exposure,
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
        job_name="ntaco_qqq_100_20_pipeline",
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


def _load_strategy_state_exposure(path: Path, symbol: str, fallback: float) -> float:
    """Read the last committed nTACO target from its versioned state contract."""
    if not path.exists():
        return fallback
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if payload.get("schema_version") != 1 or payload.get("strategy") != "ntaco_qqq_100_20":
        return fallback
    if str(payload.get("symbol", "")).upper() != str(symbol).upper():
        return fallback
    try:
        exposure = float(payload["target_exposure"])
    except (KeyError, TypeError, ValueError):
        return fallback
    if not 0.0 <= exposure <= 1.0:
        return fallback
    return exposure


def _trade_execution_succeeded(
    trade_plan: List[Dict[str, Any]],
    trade_results: List[Dict[str, Any]],
) -> bool:
    if not trade_plan:
        return True
    if len(trade_results) != len(trade_plan):
        return False
    return all(
        isinstance(item, dict)
        and item.get("status") == "ok"
        and str(item.get("trade", {}).get("status", "")).lower() == "filled"
        for item in trade_results
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_strategy_state(
    path: Path,
    *,
    symbol: str,
    target_exposure: float,
    signal_date: str,
) -> None:
    _atomic_write_json(
        path,
        {
            "schema_version": 1,
            "strategy": "ntaco_qqq_100_20",
            "symbol": str(symbol).upper(),
            "target_exposure": float(target_exposure),
            "signal_date": str(signal_date),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


@contextmanager
def _exclusive_run_lock(state_path: Path):
    """Prevent overlapping dry/live runs from producing duplicate orders or state races."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"nTACO pipeline is already running; lock exists: {lock_path}") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _enforce_low_signal_no_buy(signal: Mapping[str, Any], *, current_exposure: float) -> Dict[str, Any]:
    """A low nTACO signal may trim exposure, but must never create a QQQ purchase."""
    guarded = dict(signal)
    actual = min(max(float(current_exposure), 0.0), 1.0)
    if guarded.get("regime") == "low" and float(guarded.get("exposure", 0.0)) > actual:
        guarded["exposure"] = actual
        guarded["action"] = "hold_low_no_buy"
    return guarded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run nTACO 100/20 QQQ-only timing pipeline")
    parser.add_argument("--execute-trades", action="store_true", help="Submit live Alpaca orders; default is dry-run")
    parser.add_argument("--skip-account-refresh", action="store_true")
    parser.add_argument("--skip-data-sync", action="store_true", help="Use existing TACO and QQQ data")
    parser.add_argument("--skip-taco-sync", action="store_true")
    parser.add_argument("--skip-price-sync", action="store_true")
    parser.add_argument("--av-calls-per-minute", type=int, default=75)
    parser.add_argument("--signal-date", default="", help="Execution date for deterministic dry-run only")
    parser.add_argument(
        "--output-file",
        default="data/taco_qqq_pipeline_latest.json",
        help="JSON audit output path",
    )
    return parser


def _run_pipeline(
    args: argparse.Namespace,
    *,
    config: Mapping[str, Any],
    strategy: Mapping[str, Any],
    state_path: Path,
) -> tuple[Dict[str, Any], Path]:
    validate_run_options(
        execute_trades=bool(args.execute_trades),
        skip_account_refresh=bool(args.skip_account_refresh),
        signal_date=str(args.signal_date or ""),
    )
    if not strategy["enabled"]:
        raise RuntimeError("ntaco_strategy.enabled must be true")

    symbol = strategy["symbol"]
    taco_db = _resolve_root_path(strategy["taco_db"])
    execution_date = _execution_date(args.signal_date)
    output_path = _resolve_root_path(args.output_file)
    skip_all = bool(args.skip_data_sync)
    sync_results: Dict[str, Any] = {}

    if skip_all or args.skip_taco_sync:
        sync_results["taco"] = {"status": "skipped", "db": str(taco_db)}
    else:
        sync_results["taco"] = {
            "status": "ok",
            **sync_taco(db_path=taco_db, url=strategy["dashboard_url"]),
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
    account, positions, account_refresh = _load_account(
        skip_refresh=bool(args.skip_account_refresh),
        execute_trades=bool(args.execute_trades),
    )
    if float(account.get("equity") or account.get("portfolio_value") or 0.0) <= 0:
        raise RuntimeError("No valid Alpaca account snapshot is available")
    current_exposure = current_symbol_exposure(symbol, account=account, positions=positions)
    prior_exposure = (
        current_exposure
        if args.signal_date
        else _load_strategy_state_exposure(state_path, symbol, current_exposure)
    )
    signal = calculate_ntaco_signal(
        taco_rows,
        execution_date=execution_date,
        prior_exposure=prior_exposure,
        lower_threshold=strategy["lower_threshold"],
        upper_threshold=strategy["upper_threshold"],
        buy_exposure=strategy["buy_exposure"],
        sell_fraction=strategy["sell_fraction"],
        normalization_lookback=strategy["normalization_lookback"],
        max_data_age_days=strategy["max_data_age_days"],
    )
    signal = _enforce_low_signal_no_buy(signal, current_exposure=current_exposure)
    target_weights = target_weights_for_exposure(symbol, signal["exposure"])
    prices = _load_prices(symbol, positions, require_live=bool(args.execute_trades))
    trade_plan = build_rebalance_plan(
        account=account,
        positions=positions,
        prices=prices,
        target_weights=target_weights,
    )
    trade_results = _execute_trade_plan(trade_plan) if args.execute_trades else []
    if args.execute_trades and _trade_execution_succeeded(trade_plan, trade_results):
        _write_strategy_state(
            state_path,
            symbol=symbol,
            target_exposure=float(signal["exposure"]),
            signal_date=str(signal["signal_date"]),
        )

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
    _atomic_write_json(output_path, output)
    return output, output_path


def main() -> None:
    args = build_parser().parse_args()
    config = load_config()
    strategy = get_ntaco_strategy_config(config)
    state_path = _resolve_root_path(strategy["state_file"])
    with _exclusive_run_lock(state_path):
        output, output_path = _run_pipeline(
            args,
            config=config,
            strategy=strategy,
            state_path=state_path,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Result written to: {output_path}")


if __name__ == "__main__":
    main()
