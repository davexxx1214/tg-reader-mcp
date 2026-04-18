#!/usr/bin/env python3
"""
将策略信号转换为可执行交易计划。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalize_symbol(symbol: Any) -> str:
    raw = str(symbol or "").upper().strip()
    return raw.split(":")[-1] if raw else ""


def _extract_positions_map(positions: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for pos in positions or []:
        symbol = _normalize_symbol(pos.get("symbol"))
        if not symbol:
            continue
        qty = _to_int(pos.get("qty"), 0)
        if qty > 0:
            out[symbol] = qty
    return out


def _select_best_signal_per_symbol(signals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    selected: Dict[str, Dict[str, Any]] = {}
    for signal in signals:
        symbol = _normalize_symbol(signal.get("symbol"))
        if not symbol:
            continue
        candidate = dict(signal)
        candidate["symbol"] = symbol
        confidence = _to_float(candidate.get("confidence"), 0.0)
        prev = selected.get(symbol)
        if prev is None:
            selected[symbol] = candidate
            continue
        prev_conf = _to_float(prev.get("confidence"), 0.0)
        if confidence > prev_conf:
            selected[symbol] = candidate
        elif confidence == prev_conf:
            # 置信度相同时优先卖出，偏保守
            if str(candidate.get("action", "")).lower() == "sell":
                selected[symbol] = candidate
    return selected


def build_trade_plan(
    signals: List[Dict[str, Any]],
    risk_config: Dict[str, Any],
    account_snapshot: Dict[str, Any] | None = None,
    positions_snapshot: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """
    根据策略信号生成初始 trade plan（不含全量风控拦截逻辑）。
    """
    account = account_snapshot or {}
    positions = positions_snapshot or []
    positions_map = _extract_positions_map(positions)

    cash = _to_float(account.get("cash"), 0.0)
    buying_power = _to_float(account.get("buying_power"), cash)
    available_cash = max(cash, buying_power, 0.0)

    max_position_pct = _to_float(risk_config.get("max_position_pct"), 0.1)
    max_trade_notional = _to_float(risk_config.get("max_trade_notional"), 2000.0)

    per_trade_budget = min(available_cash * max_position_pct, max_trade_notional)

    selected = _select_best_signal_per_symbol(signals)
    trade_plan: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for symbol, signal in selected.items():
        action = str(signal.get("action", "")).lower().strip()
        if action not in {"buy", "sell"}:
            skipped.append({"symbol": symbol, "reason": "invalid_action", "signal": signal})
            continue

        price = _to_float(signal.get("price"), 0.0)
        if price <= 0:
            skipped.append({"symbol": symbol, "reason": "invalid_price", "signal": signal})
            continue

        if action == "buy":
            qty = int(per_trade_budget // price)
            if qty <= 0:
                skipped.append({"symbol": symbol, "reason": "insufficient_budget_for_buy", "signal": signal})
                continue
        else:
            holding_qty = positions_map.get(symbol, 0)
            if holding_qty <= 0:
                skipped.append({"symbol": symbol, "reason": "no_position_to_sell", "signal": signal})
                continue
            # 首版卖出按持仓的一半，至少 1 股
            qty = max(int(holding_qty * 0.5), 1)
            qty = min(qty, holding_qty)

        trade_plan.append(
            {
                "action": action,
                "symbol": symbol,
                "qty": qty,
                "price_ref": round(price, 4),
                "notional_estimate": round(price * qty, 2),
                "strategy": signal.get("strategy"),
                "confidence": _to_float(signal.get("confidence"), 0.0),
                "reason": signal.get("reason", ""),
            }
        )

    return {
        "trade_plan": trade_plan,
        "skipped_signals": skipped,
        "assumptions": {
            "cash": available_cash,
            "per_trade_budget": per_trade_budget,
            "positions_count": len(positions_map),
        },
    }
