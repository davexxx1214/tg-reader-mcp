#!/usr/bin/env python3
"""Causal normalized-TACO 100/20 position strategy for QQQ only.

Each factor is ranked against its own strictly-prior 42-observation history.
The published factor weights then produce nTACO on a 0..100 scale. A high
signal targets 100% QQQ; a low signal caps exposure at 80% and never buys from
cash. Values between the thresholds preserve the prior target exposure.
"""

from __future__ import annotations

import html as html_lib
import json
import math
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import requests


DEFAULT_DASHBOARD_URL = "https://ocmacro.com/dashboard/trump"
DEFAULT_SYMBOL = "QQQ"
FACTOR_WEIGHTS: Dict[str, float] = {
    "approval": 0.25,
    "dgs10": 0.15,
    "move": 0.10,
    "sp500": 0.15,
    "vix": 0.10,
    "cpi_nowcast": 0.25,
}
FACTOR_SOURCE_KEYS: Dict[str, Sequence[str]] = {
    "approval": ("approval",),
    "dgs10": ("dgs10",),
    "move": ("move",),
    "sp500": ("sp500",),
    "vix": ("vix",),
    "cpi_nowcast": ("cpi_nowcast", "bkevenpy02"),
}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> date:
    raw = str(value or "").strip()
    if len(raw) >= 10:
        return date.fromisoformat(raw[:10])
    raise ValueError(f"Invalid date: {value}")


def _extract_json_objects(text: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for match in re.finditer(r'\{"date":"20', text):
        start = match.start()
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        item = json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if (
                        isinstance(item, dict)
                        and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", str(item.get("date", "")))
                        and item.get("value") is not None
                    ):
                        results.append(item)
                    break
    return results


def _decode_next_payloads(page_html: str) -> str:
    payloads = re.findall(
        r'self\.__next_f\.push\(\[\d+,\s*"(.*?)"\s*\]\)',
        page_html,
        flags=re.DOTALL,
    )
    decoded: List[str] = []
    for payload in payloads:
        try:
            decoded.append(json.loads(f'"{payload}"'))
        except json.JSONDecodeError:
            decoded.append(payload.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\"))
    return "\n".join(decoded)


def parse_taco_dashboard(page_html: str) -> List[Dict[str, Any]]:
    points = _extract_json_objects(_decode_next_payloads(page_html))
    if not points:
        points = _extract_json_objects(html_lib.unescape(page_html))
    if not points:
        raise ValueError("No TACO daily data found in dashboard response")

    unique: Dict[str, Dict[str, Any]] = {}
    for point in points:
        trade_date = str(point["date"])
        contributions = point.get("contributions")
        if not isinstance(contributions, dict):
            raise ValueError(f"TACO factor contributions missing for {trade_date}")
        unique[trade_date] = {
            "date": trade_date,
            "value": _to_float(point.get("value")),
            "event_strength_score": _to_float(point.get("event_strength_score")),
            "contributions": dict(contributions),
            "raw": point,
        }
    return [unique[key] for key in sorted(unique)]


def fetch_taco_dashboard(
    url: str = DEFAULT_DASHBOARD_URL,
    *,
    timeout_seconds: float = 30.0,
    session: Any = requests,
) -> List[Dict[str, Any]]:
    response = session.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131 Safari/537.36"
            )
        },
        timeout=max(float(timeout_seconds), 1.0),
    )
    response.raise_for_status()
    return parse_taco_dashboard(response.text)


def _factor_pressures(row: Mapping[str, Any]) -> Dict[str, float]:
    contributions = row.get("contributions")
    if not isinstance(contributions, Mapping):
        raw = row.get("raw")
        contributions = raw.get("contributions") if isinstance(raw, Mapping) else None
    if not isinstance(contributions, Mapping):
        raise ValueError(f"TACO factor contributions missing for {row.get('date')}")

    pressures: Dict[str, float] = {}
    for factor, weight in FACTOR_WEIGHTS.items():
        source_value = None
        for source_key in FACTOR_SOURCE_KEYS[factor]:
            if source_key in contributions:
                source_value = contributions[source_key]
                break
        if source_value is None:
            raise ValueError(f"TACO factor {factor} missing for {row.get('date')}")
        numeric = float(source_value)
        if not math.isfinite(numeric):
            raise ValueError(f"TACO factor {factor} is non-finite for {row.get('date')}")
        pressures[factor] = numeric / weight
    return pressures


def build_ntaco_rows(
    taco_rows: Iterable[Mapping[str, Any]],
    *,
    lookback: int = 42,
) -> List[Dict[str, Any]]:
    """Build nTACO with an expanding cold start and prior-only rolling window."""
    ordered = sorted((dict(row) for row in taco_rows), key=lambda row: _parse_date(row.get("date")))
    pressures = [_factor_pressures(row) for row in ordered]
    output: List[Dict[str, Any]] = []
    window_size = max(int(lookback), 1)
    for index, row in enumerate(ordered):
        history = pressures[max(0, index - window_size):index]
        ntaco = None
        factor_percentiles: Dict[str, float] = {}
        if history:
            for factor in FACTOR_WEIGHTS:
                current = pressures[index][factor]
                values = [item[factor] for item in history]
                below = sum(value < current for value in values)
                equal = sum(value == current for value in values)
                factor_percentiles[factor] = 100.0 * (below + 0.5 * equal) / len(values)
            ntaco = sum(
                factor_percentiles[factor] * FACTOR_WEIGHTS[factor]
                for factor in FACTOR_WEIGHTS
            )
        output.append(
            {
                "date": _parse_date(row.get("date")).isoformat(),
                "raw_taco": _to_float(row.get("value")),
                "ntaco": ntaco,
                "factor_percentiles": factor_percentiles,
            }
        )
    return output


def _threshold_percent(value: float) -> float:
    number = float(value)
    return number * 100.0 if 0.0 <= number <= 1.0 else number


def target_exposure_for_ntaco(
    ntaco: float,
    prior_exposure: float,
    *,
    lower_threshold: float = 0.30,
    upper_threshold: float = 0.49,
    buy_exposure: float = 1.0,
    sell_fraction: float = 0.20,
) -> tuple[float, str]:
    lower = _threshold_percent(lower_threshold)
    upper = _threshold_percent(upper_threshold)
    if not 0.0 <= lower < upper <= 100.0:
        raise ValueError("nTACO thresholds must satisfy 0 <= lower < upper <= 100")
    previous = min(max(float(prior_exposure), 0.0), 1.0)
    full_target = min(max(float(buy_exposure), 0.0), 1.0)
    low_cap = min(max(1.0 - float(sell_fraction), 0.0), full_target)
    value = float(ntaco)
    if value >= upper:
        return full_target, "raise_to_100" if previous < full_target else "hold_100"
    if value <= lower:
        target = min(previous, low_cap)
        if target < previous:
            return target, "trim_to_80"
        if target == 0.0:
            return target, "hold_cash"
        return target, "hold_low"
    return previous, "hold"


def calculate_ntaco_signal(
    taco_rows: Iterable[Mapping[str, Any]],
    *,
    execution_date: str,
    prior_exposure: float,
    lower_threshold: float = 0.30,
    upper_threshold: float = 0.49,
    buy_exposure: float = 1.0,
    sell_fraction: float = 0.20,
    normalization_lookback: int = 42,
    max_data_age_days: int = 7,
) -> Dict[str, Any]:
    execution_day = _parse_date(execution_date)
    eligible = [dict(row) for row in taco_rows if _parse_date(row.get("date")) < execution_day]
    eligible.sort(key=lambda row: _parse_date(row.get("date")))
    if not eligible:
        raise ValueError("No lagged TACO data is available before execution date")
    signal_date = _parse_date(eligible[-1].get("date"))
    data_age_days = (execution_day - signal_date).days
    if data_age_days > max(int(max_data_age_days), 0):
        raise ValueError(
            f"TACO data is stale: signal_date={signal_date.isoformat()} age_days={data_age_days}"
        )

    normalized = build_ntaco_rows(eligible, lookback=normalization_lookback)
    latest = normalized[-1]
    if latest["ntaco"] is None:
        raise ValueError("At least two prior TACO factor observations are required")
    target, action = target_exposure_for_ntaco(
        float(latest["ntaco"]),
        prior_exposure,
        lower_threshold=lower_threshold,
        upper_threshold=upper_threshold,
        buy_exposure=buy_exposure,
        sell_fraction=sell_fraction,
    )
    lower = _threshold_percent(lower_threshold)
    upper = _threshold_percent(upper_threshold)
    regime = "high" if float(latest["ntaco"]) >= upper else "low" if float(latest["ntaco"]) <= lower else "middle"
    return {
        "execution_date": execution_day.isoformat(),
        "signal_date": signal_date.isoformat(),
        "data_age_days": data_age_days,
        "raw_taco": latest["raw_taco"],
        "ntaco": float(latest["ntaco"]),
        "ntaco_pct": float(latest["ntaco"]) / 100.0,
        "factor_percentiles": latest["factor_percentiles"],
        "lower_threshold": float(lower_threshold),
        "upper_threshold": float(upper_threshold),
        "prior_exposure": float(prior_exposure),
        "exposure": target,
        "regime": regime,
        "action": action,
        "taco_observations": len(eligible),
        "normalization_lookback": max(int(normalization_lookback), 1),
    }


def target_weights_for_exposure(symbol: str, exposure: float) -> Dict[str, float]:
    normalized = str(symbol or DEFAULT_SYMBOL).upper().strip()
    exposure_value = float(exposure)
    if not 0.0 <= exposure_value <= 1.0:
        raise ValueError("QQQ timing exposure must be between 0 and 1")
    return {normalized: exposure_value} if exposure_value > 0.0 else {}


def current_symbol_exposure(
    symbol: str,
    *,
    account: Mapping[str, Any],
    positions: Iterable[Mapping[str, Any]],
) -> float:
    equity = _to_float(account.get("equity") or account.get("portfolio_value"))
    if equity <= 0:
        return 0.0
    normalized = str(symbol).upper().strip()
    market_value = 0.0
    for position in positions:
        if str(position.get("symbol", "")).upper().strip() != normalized:
            continue
        value = _to_float(position.get("market_value"))
        if value == 0.0:
            value = _to_float(position.get("qty")) * _to_float(position.get("current_price"))
        market_value += max(value, 0.0)
    return min(max(market_value / equity, 0.0), 1.0)


def build_rebalance_plan(
    *,
    account: Mapping[str, Any],
    positions: Iterable[Mapping[str, Any]],
    prices: Mapping[str, Any],
    target_weights: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    equity = _to_float(account.get("equity") or account.get("portfolio_value"))
    if equity <= 0:
        raise ValueError("Account equity must be positive")

    position_map: Dict[str, Dict[str, Any]] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper().strip()
        if not symbol:
            continue
        qty = abs(_to_float(position.get("qty")))
        signed_qty = -qty if str(position.get("side", "long")).lower() == "short" else qty
        position_map[symbol] = {
            "qty": signed_qty,
            "current_price": _to_float(position.get("current_price")),
        }

    normalized_prices = {str(symbol).upper(): _to_float(price) for symbol, price in prices.items()}
    all_symbols = set(position_map) | {str(symbol).upper() for symbol in target_weights}
    plan: List[Dict[str, Any]] = []
    for symbol in sorted(all_symbols):
        current_qty = position_map.get(symbol, {}).get("qty", 0.0)
        price = normalized_prices.get(symbol) or position_map.get(symbol, {}).get("current_price", 0.0)
        if price <= 0:
            raise ValueError(f"Missing valid price for {symbol}")
        target_weight = min(max(_to_float(target_weights.get(symbol)), 0.0), 1.0)
        target_qty = math.floor((equity * target_weight) / price)
        delta_qty = target_qty - current_qty
        whole_qty = math.floor(abs(delta_qty) + 1e-9)
        if whole_qty <= 0:
            continue
        action = "buy" if delta_qty > 0 else "sell"
        plan.append(
            {
                "action": action,
                "symbol": symbol,
                "qty": whole_qty,
                "price": price,
                "estimated_notional": round(whole_qty * price, 2),
                "current_qty": current_qty,
                "target_qty": target_qty,
                "target_weight": target_weight,
                "reason": "ntaco_qqq_100_20_rebalance",
            }
        )
    return sorted(plan, key=lambda item: (item["action"] != "sell", item["symbol"]))
