#!/usr/bin/env python3
"""Fetch current US equity snapshots from Alpaca only."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, Iterable, List, Optional

import requests

from _config import get_alpaca_credentials, load_config


DEFAULT_DATA_BASE_URL = "https://data.alpaca.markets"


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _chunks(values: List[str], size: int) -> Iterable[List[str]]:
    for index in range(0, len(values), max(int(size), 1)):
        yield values[index : index + max(int(size), 1)]


def _fetch_alpaca_snapshots(
    symbols: List[str], timeout_seconds: float = 30.0
) -> Dict[str, Dict[str, Any]]:
    config = load_config()
    api_key, secret_key, _ = get_alpaca_credentials(config)
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }
    feed = os.getenv("ALPACA_DATA_FEED", "iex").strip() or "iex"
    base_url = os.getenv("ALPACA_DATA_BASE_URL", DEFAULT_DATA_BASE_URL).rstrip("/")
    normalized = list(dict.fromkeys(str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()))
    snapshots: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunks(normalized, 200):
        response = requests.get(
            f"{base_url}/v2/stocks/snapshots",
            headers=headers,
            params={"symbols": ",".join(chunk), "feed": feed},
            timeout=max(float(timeout_seconds), 1.0),
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("snapshots", payload) if isinstance(payload, dict) else {}
        for symbol in chunk:
            row = rows.get(symbol) if isinstance(rows, dict) else None
            if not isinstance(row, dict):
                continue
            trade = row.get("latestTrade") if isinstance(row.get("latestTrade"), dict) else {}
            quote = row.get("latestQuote") if isinstance(row.get("latestQuote"), dict) else {}
            daily = row.get("dailyBar") if isinstance(row.get("dailyBar"), dict) else {}
            previous = row.get("prevDailyBar") if isinstance(row.get("prevDailyBar"), dict) else {}
            trade_price = _to_float(trade.get("p"))
            ask = _to_float(quote.get("ap"))
            bid = _to_float(quote.get("bp"))
            daily_close = _to_float(daily.get("c"))
            previous_close = _to_float(previous.get("c"))
            price = trade_price
            if price is None and ask is not None and bid is not None:
                price = (ask + bid) / 2.0
            price = price if price is not None else daily_close or previous_close
            if price is None or price <= 0:
                continue
            snapshots[symbol] = {
                "symbol": symbol,
                "price": float(price),
                "previous_close": previous_close,
                "volume": float(_to_float(daily.get("v")) or 0.0),
                "source": "alpaca",
                "feed": feed,
            }
    return snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="US equity tickers")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = _fetch_alpaca_snapshots(args.symbols)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    for symbol in args.symbols:
        row = result.get(symbol.upper())
        print(f"{symbol.upper()}: {row['price']:.4f}" if row else f"{symbol.upper()}: unavailable")


if __name__ == "__main__":
    main()
