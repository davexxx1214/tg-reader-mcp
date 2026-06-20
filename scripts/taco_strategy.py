#!/usr/bin/env python3
"""TACO + Jin10 QQQ-only timing strategy.

The deployed rule is intentionally binary: hold 100% QQQ when the lagged
combined signal is at or below the buy threshold, otherwise hold cash.
"""

from __future__ import annotations

import html as html_lib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests


DEFAULT_DASHBOARD_URL = "https://ocmacro.com/dashboard/trump"
DEFAULT_SYMBOL = "QQQ"

RISK_TERMS: Dict[str, float] = {
    "关税": 1.5,
    "制裁": 1.2,
    "战争": 1.5,
    "冲突": 1.2,
    "袭击": 1.3,
    "打击": 1.2,
    "威胁": 1.0,
    "霍尔木兹": 1.5,
    "伊朗": 0.8,
    "以色列": 0.8,
    "油价": 0.8,
    "原油": 0.5,
    "通胀": 0.8,
    "CPI": 0.8,
    "鲍威尔": 0.7,
    "美联储": 0.7,
    "收益率": 0.6,
    "加息": 0.9,
    "爆发": 1.2,
    "破裂": 1.2,
    "拒绝": 0.8,
}

RELIEF_TERMS: Dict[str, float] = {
    "停火": 1.5,
    "暂停": 1.2,
    "延期": 1.2,
    "豁免": 1.3,
    "谈判": 1.0,
    "达成": 1.1,
    "缓和": 1.2,
    "撤回": 1.2,
    "降级": 1.0,
    "同意": 0.8,
    "接触": 0.8,
    "恢复": 0.8,
    "协议": 1.0,
    "和平": 1.0,
    "斡旋": 1.0,
    "取消": 1.0,
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
        unique[trade_date] = {
            "date": trade_date,
            "value": _to_float(point.get("value")),
            "event_strength_score": _to_float(point.get("event_strength_score")),
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


def _star_weight(text: str) -> float:
    stars = text.count("★")
    if stars <= 0:
        return 0.6
    if stars == 1:
        return 0.8
    return 1.0


def _weighted_term_score(text: str, terms: Mapping[str, float]) -> float:
    return sum(float(weight) for term, weight in terms.items() if term in text)


def score_jin10_message(text: str) -> Tuple[float, float]:
    normalized = str(text or "")
    weight = _star_weight(normalized)
    return (
        weight * _weighted_term_score(normalized, RISK_TERMS),
        weight * _weighted_term_score(normalized, RELIEF_TERMS),
    )


def load_jin10_messages(
    db_path: Path,
    *,
    start_date: Optional[str] = None,
    end_date_inclusive: Optional[str] = None,
    channel: str = "jinshishuju_bot",
) -> List[Dict[str, Any]]:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Jin10 SQLite database not found: {db_path}")
    clauses = ["channel = ?"]
    params: List[Any] = [channel]
    if start_date:
        clauses.append("substr(date_hk, 1, 10) >= ?")
        params.append(str(start_date))
    if end_date_inclusive:
        clauses.append("substr(date_hk, 1, 10) <= ?")
        params.append(str(end_date_inclusive))
    query = f"""
        SELECT message_id, date_hk, text
        FROM jin10_messages
        WHERE {' AND '.join(clauses)}
        ORDER BY message_id
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [
        {"message_id": int(row[0]), "date_hk": str(row[1]), "text": str(row[2] or "")}
        for row in rows
    ]


def _daily_news_intensity(
    messages: Iterable[Mapping[str, Any]],
    *,
    through_date: date,
) -> Dict[date, Dict[str, float]]:
    totals: Dict[date, Dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, "risk_sum": 0.0, "relief_sum": 0.0}
    )
    for message in messages:
        message_date = _parse_date(message.get("date_hk") or message.get("date"))
        if message_date > through_date:
            continue
        risk, relief = score_jin10_message(str(message.get("text") or ""))
        totals[message_date]["count"] += 1.0
        totals[message_date]["risk_sum"] += risk
        totals[message_date]["relief_sum"] += relief

    daily: Dict[date, Dict[str, float]] = {}
    for message_date, values in totals.items():
        count = max(values["count"], 1.0)
        daily[message_date] = {
            "count": values["count"],
            "risk": values["risk_sum"] / count,
            "relief": values["relief_sum"] / count,
        }
    return daily


def _decayed_news_value(
    daily: Mapping[date, Mapping[str, float]],
    *,
    key: str,
    through_date: date,
    half_life_days: int,
) -> float:
    decay = math.log(2.0) / max(int(half_life_days), 1)
    total = 0.0
    for message_date, values in daily.items():
        age = max((through_date - message_date).days, 0)
        total += _to_float(values.get(key)) * math.exp(-decay * age)
    return total


def calculate_taco_jin10_signal(
    taco_rows: Iterable[Mapping[str, Any]],
    jin10_rows: Iterable[Mapping[str, Any]],
    *,
    execution_date: str,
    smoothing_days: int = 3,
    news_half_life_days: int = 2,
    risk_beta: float = -3.0,
    relief_beta: float = 5.0,
    buy_threshold: float = -4.0,
    max_data_age_days: int = 7,
    require_fresh_news: bool = False,
) -> Dict[str, Any]:
    execution_day = _parse_date(execution_date)
    eligible = []
    for row in taco_rows:
        row_date = _parse_date(row.get("date"))
        if row_date < execution_day:
            eligible.append((row_date, _to_float(row.get("value"))))
    eligible.sort(key=lambda item: item[0])
    if not eligible:
        raise ValueError("No lagged TACO data is available before execution date")

    signal_date, raw_taco = eligible[-1]
    data_age_days = (execution_day - signal_date).days
    if data_age_days > max(int(max_data_age_days), 0):
        raise ValueError(
            f"TACO data is stale: signal_date={signal_date.isoformat()} age_days={data_age_days}"
        )

    window = eligible[-max(int(smoothing_days), 1):]
    smoothed_taco = sum(value for _, value in window) / len(window)
    daily_news = _daily_news_intensity(jin10_rows, through_date=signal_date)
    latest_news_date = max(daily_news) if daily_news else None
    news_age_days = (signal_date - latest_news_date).days if latest_news_date else None
    if require_fresh_news:
        if latest_news_date is None:
            raise ValueError("No Jin10 data is available before the signal date")
        if news_age_days is not None and news_age_days > max(int(max_data_age_days), 0):
            raise ValueError(
                f"Jin10 data is stale: latest_date={latest_news_date.isoformat()} age_days={news_age_days}"
            )
    risk_intensity = _decayed_news_value(
        daily_news,
        key="risk",
        through_date=signal_date,
        half_life_days=news_half_life_days,
    )
    relief_intensity = _decayed_news_value(
        daily_news,
        key="relief",
        through_date=signal_date,
        half_life_days=news_half_life_days,
    )
    combined_signal = (
        smoothed_taco
        + float(risk_beta) * risk_intensity
        + float(relief_beta) * relief_intensity
    )
    exposure = 1.0 if combined_signal <= float(buy_threshold) else 0.0
    return {
        "execution_date": execution_day.isoformat(),
        "signal_date": signal_date.isoformat(),
        "data_age_days": data_age_days,
        "raw_taco": raw_taco,
        "smoothed_taco": smoothed_taco,
        "risk_intensity": risk_intensity,
        "relief_intensity": relief_intensity,
        "combined_signal": combined_signal,
        "buy_threshold": float(buy_threshold),
        "regime": "long_qqq" if exposure == 1.0 else "cash",
        "exposure": exposure,
        "taco_observations": len(eligible),
        "jin10_messages": int(sum(item.get("count", 0.0) for item in daily_news.values())),
        "latest_jin10_date": latest_news_date.isoformat() if latest_news_date else None,
        "jin10_age_days": news_age_days,
    }


def target_weights_for_exposure(symbol: str, exposure: float) -> Dict[str, float]:
    normalized = str(symbol or DEFAULT_SYMBOL).upper().strip()
    exposure_value = float(exposure)
    if exposure_value not in {0.0, 1.0}:
        raise ValueError("QQQ timing exposure must be exactly 0 or 1")
    return {normalized: 1.0} if exposure_value == 1.0 else {}


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
        target_weight = max(_to_float(target_weights.get(symbol)), 0.0)
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
                "reason": "taco_jin10_qqq_rebalance",
            }
        )
    return sorted(plan, key=lambda item: (item["action"] != "sell", item["symbol"]))
