#!/usr/bin/env python3
"""
交易计划风控拦截。
"""

from __future__ import annotations

from typing import Any, Dict, List, Set


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


def _existing_position_symbols(positions: List[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for pos in positions or []:
        symbol = _normalize_symbol(pos.get("symbol"))
        if not symbol:
            continue
        qty = _to_float(pos.get("qty"), 0.0)
        if qty > 0:
            out.add(symbol)
    return out


def apply_risk_guard(
    trade_plan: List[Dict[str, Any]],
    risk_config: Dict[str, Any],
    account_snapshot: Dict[str, Any] | None = None,
    positions_snapshot: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    account = account_snapshot or {}
    positions = positions_snapshot or []

    cash = max(_to_float(account.get("cash"), 0.0), _to_float(account.get("buying_power"), 0.0))
    max_position_pct = max(min(_to_float(risk_config.get("max_position_pct"), 0.1), 1.0), 0.0)
    max_positions = max(_to_int(risk_config.get("max_positions"), 5), 1)
    max_trade_notional = max(_to_float(risk_config.get("max_trade_notional"), 2000.0), 0.0)

    base_symbols = _existing_position_symbols(positions)
    simulated_symbols = set(base_symbols)

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for order in trade_plan or []:
        symbol = _normalize_symbol(order.get("symbol"))
        action = str(order.get("action", "")).lower().strip()
        qty = _to_int(order.get("qty"), 0)
        notional = _to_float(order.get("notional_estimate"), 0.0)
        reasons: List[str] = []

        if action not in {"buy", "sell"}:
            reasons.append("invalid_action")
        if not symbol:
            reasons.append("invalid_symbol")
        if qty <= 0:
            reasons.append("invalid_qty")

        if action == "buy":
            if max_trade_notional > 0 and notional > max_trade_notional:
                reasons.append("exceed_max_trade_notional")
            if cash > 0 and notional > cash * max_position_pct:
                reasons.append("exceed_max_position_pct")
            projected_symbols = set(simulated_symbols)
            projected_symbols.add(symbol)
            if len(projected_symbols) > max_positions:
                reasons.append("exceed_max_positions")
        elif action == "sell":
            # 允许卖出减少持仓，不额外限制 max_positions
            pass

        if reasons:
            rejected.append(
                {
                    "order": order,
                    "reasons": reasons,
                }
            )
            continue

        accepted.append(order)
        if action == "buy":
            simulated_symbols.add(symbol)
        elif action == "sell":
            # 仅在没有其它 buy 同 symbol 时尝试从集合移除
            has_pending_buy = any(
                str(x.get("action", "")).lower() == "buy" and _normalize_symbol(x.get("symbol")) == symbol
                for x in accepted
            )
            if not has_pending_buy and symbol in simulated_symbols and symbol not in base_symbols:
                simulated_symbols.discard(symbol)

    return {
        "accepted_plan": accepted,
        "rejections": rejected,
        "risk_snapshot": {
            "cash": cash,
            "max_position_pct": max_position_pct,
            "max_positions": max_positions,
            "max_trade_notional": max_trade_notional,
            "existing_positions": sorted(base_symbols),
            "projected_positions": sorted(simulated_symbols),
        },
    }
