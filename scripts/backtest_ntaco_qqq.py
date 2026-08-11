#!/usr/bin/env python3
"""Backtest the deployed causal nTACO 100/20 QQQ-only timing rule."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Mapping

from _config import get_ntaco_strategy_config, load_config
from sync_alpha_daily_to_sqlite import DEFAULT_DB_PATH as DEFAULT_PRICE_DB
from sync_taco_data import load_taco_rows
from taco_strategy import calculate_ntaco_signal


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "backtests" / "ntaco_qqq_100_20"


def _metrics(returns: List[float], exposures: List[float]) -> Dict[str, float | int]:
    clean = [float(value) for value in returns]
    if not clean:
        return {
            "annual_return": 0.0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "annual_volatility": 0.0,
            "sharpe_ratio": 0.0,
            "exposure": 0.0,
            "num_trades": 0,
        }
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in clean:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    total_return = equity - 1.0
    annual_return = equity ** (252.0 / len(clean)) - 1.0 if equity > 0 else -1.0
    volatility = pstdev(clean) * math.sqrt(252.0) if len(clean) > 1 else 0.0
    sharpe = mean(clean) / pstdev(clean) * math.sqrt(252.0) if len(clean) > 1 and pstdev(clean) > 0 else 0.0
    trades = 0
    previous = None
    for exposure in exposures:
        if previous is None or exposure != previous:
            trades += 1
        previous = exposure
    return {
        "annual_return": annual_return,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "annual_volatility": volatility,
        "sharpe_ratio": sharpe,
        "exposure": mean(exposures) if exposures else 0.0,
        "num_trades": trades,
    }


def load_price_rows(db_path: Path, symbol: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT trade_date, open, close
            FROM stock_daily
            WHERE symbol = ?
            ORDER BY trade_date
            """,
            (symbol,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"date": str(row[0]), "open": float(row[1]), "close": float(row[2])}
        for row in rows
        if row[1] is not None and row[2] is not None
    ]


def run_backtest_rows(
    *,
    price_rows: Iterable[Mapping[str, Any]],
    taco_rows: Iterable[Mapping[str, Any]],
    start_date: str,
    end_date: str,
    strategy_config: Mapping[str, Any],
) -> Dict[str, Any]:
    prices = sorted((dict(row) for row in price_rows), key=lambda row: str(row["date"]))
    tacos = [dict(row) for row in taco_rows]
    if len(prices) < 2:
        raise ValueError("At least two QQQ price rows are required")

    targets: Dict[str, float] = {}
    signals: Dict[str, Dict[str, Any]] = {}
    prior_target = 0.0
    for row in prices:
        trade_date = str(row["date"])
        try:
            signal = calculate_ntaco_signal(
                tacos,
                execution_date=trade_date,
                prior_exposure=prior_target,
                lower_threshold=float(strategy_config["lower_threshold"]),
                upper_threshold=float(strategy_config["upper_threshold"]),
                buy_exposure=float(strategy_config["buy_exposure"]),
                sell_fraction=float(strategy_config["sell_fraction"]),
                normalization_lookback=int(strategy_config["normalization_lookback"]),
                max_data_age_days=int(strategy_config["max_data_age_days"]),
            )
        except ValueError as exc:
            signal = {
                "signal_date": None,
                "raw_taco": None,
                "ntaco": None,
                "exposure": prior_target,
                "regime": "hold_data_unavailable",
                "action": "hold",
                "error": str(exc),
            }
        signals[trade_date] = signal
        targets[trade_date] = float(signal["exposure"])
        prior_target = targets[trade_date]

    cost_rate = float(strategy_config.get("transaction_cost_bps", 10.0)) / 10_000.0
    daily: List[Dict[str, Any]] = []
    strategy_returns: List[float] = []
    qqq_returns: List[float] = []
    exposures: List[float] = []
    strategy_equity = 1.0
    qqq_equity = 1.0

    for index in range(1, len(prices)):
        row = prices[index]
        previous = prices[index - 1]
        trade_date = str(row["date"])
        if not (start_date <= trade_date <= end_date) or trade_date not in targets:
            continue
        target = targets[trade_date]
        previous_target = targets.get(str(previous["date"]), target)
        previous_close = float(previous["close"])
        current_open = float(row["open"])
        current_close = float(row["close"])
        overnight_asset = current_open / previous_close - 1.0
        intraday_asset = current_close / current_open - 1.0
        overnight_portfolio = previous_target * overnight_asset
        overnight_factor = 1.0 + overnight_portfolio
        pre_open_weight = (
            previous_target * (1.0 + overnight_asset) / overnight_factor
            if overnight_factor != 0
            else 0.0
        )
        turnover = abs(target - pre_open_weight)
        strategy_return = (
            (1.0 + overnight_portfolio) * (1.0 + target * intraday_asset)
            - 1.0
            - turnover * cost_rate
        )
        qqq_return = current_close / previous_close - 1.0
        strategy_equity *= 1.0 + strategy_return
        qqq_equity *= 1.0 + qqq_return
        signal = signals[trade_date]
        daily.append(
            {
                "date": trade_date,
                "signal_date": signal.get("signal_date"),
                "signal_error": signal.get("error"),
                "raw_taco": signal.get("raw_taco"),
                "ntaco": signal.get("ntaco"),
                "regime": signal.get("regime"),
                "signal_action": signal.get("action"),
                "target_qqq": target,
                "turnover": turnover,
                "strategy_return": strategy_return,
                "qqq_return": qqq_return,
                "strategy_equity": strategy_equity,
                "qqq_equity": qqq_equity,
            }
        )
        strategy_returns.append(strategy_return)
        qqq_returns.append(qqq_return)
        exposures.append(target)

    if not daily:
        raise ValueError("No backtest rows were produced for the requested window")
    return {
        "start_date": start_date,
        "end_date": end_date,
        "trading_days": len(daily),
        "strategy": _metrics(strategy_returns, exposures),
        "qqq": _metrics(qqq_returns, [1.0] * len(qqq_returns)),
        "daily": daily,
    }


def _write_outputs(result: Mapping[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {key: value for key, value in result.items() if key != "daily"}
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    daily = list(result["daily"])
    with (output_dir / "daily.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(daily[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(daily)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest deployed nTACO 100/20 QQQ timing rule")
    parser.add_argument("--start", default="2025-02-19")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--price-db", default=str(DEFAULT_PRICE_DB))
    parser.add_argument("--taco-db", default="")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config()
    strategy = get_ntaco_strategy_config(config)
    taco_db = Path(args.taco_db) if args.taco_db else ROOT_DIR / strategy["taco_db"]
    result = run_backtest_rows(
        price_rows=load_price_rows(Path(args.price_db), strategy["symbol"]),
        taco_rows=load_taco_rows(taco_db),
        start_date=args.start,
        end_date=args.end,
        strategy_config=strategy,
    )
    _write_outputs(result, Path(args.output_dir))
    print(json.dumps({key: value for key, value in result.items() if key != "daily"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
