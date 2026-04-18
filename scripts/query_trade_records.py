#!/usr/bin/env python3
"""
查询并统一展示最近 N 条交易记录（默认 50 条）。

数据来源：
- position.jsonl: 动作与交易后持仓快照
- balance.jsonl: 交易后账户总览与持仓明细

用法:
    python skills/alpaca-live-trading/scripts/query_trade_records.py
    python skills/alpaca-live-trading/scripts/query_trade_records.py --limit 20
    python skills/alpaca-live-trading/scripts/query_trade_records.py --json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

def resolve_skill_data_dir() -> Path:
    # skills/alpaca-live-trading/scripts -> skills/alpaca-live-trading/data
    return Path(__file__).resolve().parent.parent / "data"


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def build_balance_map(balance_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    # 以 order_id 为主键，便于与 position 记录对齐
    mapping: Dict[str, Dict[str, Any]] = {}
    for row in balance_rows:
        trade = row.get("trade", {})
        order_id = str(trade.get("order_id", "")).strip()
        if order_id:
            mapping[order_id] = row
    return mapping


def summarize_positions(position_snapshot: Dict[str, Any], top_n: int = 8) -> str:
    holdings = []
    for symbol, qty in position_snapshot.items():
        if symbol == "CASH":
            continue
        try:
            q = float(qty)
        except Exception:
            continue
        if abs(q) > 0:
            holdings.append((symbol, q))

    holdings.sort(key=lambda x: (-abs(x[1]), x[0]))
    if not holdings:
        return "无持仓"

    head = ", ".join([f"{s}:{int(q) if float(q).is_integer() else q}" for s, q in holdings[:top_n]])
    if len(holdings) > top_n:
        head += f" ... 共{len(holdings)}只"
    return head


def format_currency(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "N/A"


def _fallback_key(date: Any, action: Any, symbol: Any, amount: Any) -> str:
    return f"{date}|{str(action).lower()}|{str(symbol).upper()}|{amount}"


def _record_time_key(row: Dict[str, Any]) -> str:
    """
    排序优先级：UTC -> ET -> date（兼容旧数据）
    """
    return str(row.get("timestamp_utc") or row.get("timestamp_et") or row.get("date") or "")


def unified_records(
    position_rows: List[Dict[str, Any]],
    balance_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    balance_map = build_balance_map(balance_rows)
    fallback_map: Dict[str, Dict[str, Any]] = {}
    for b in balance_rows:
        trade = b.get("trade", {})
        k = _fallback_key(
            b.get("date"),
            trade.get("action"),
            trade.get("symbol"),
            trade.get("qty"),
        )
        fallback_map[k] = b

    merged: List[Dict[str, Any]] = []

    for p in position_rows:
        action = p.get("this_action", {})
        order_id = str(action.get("order_id", "")).strip()
        b = balance_map.get(order_id, {})
        if not b:
            k = _fallback_key(
                p.get("date"),
                action.get("action"),
                action.get("symbol"),
                action.get("amount"),
            )
            b = fallback_map.get(k, {})

        account = b.get("account", {})
        merged.append(
            {
                "date": p.get("date"),
                "timestamp_et": p.get("timestamp_et"),
                "timestamp_utc": p.get("timestamp_utc"),
                "id": p.get("id"),
                "action": action.get("action"),
                "symbol": action.get("symbol"),
                "amount": action.get("amount"),
                "price": action.get("price"),
                "order_id": order_id,
                "cash": account.get("cash"),
                "equity": account.get("equity"),
                "portfolio_value": account.get("portfolio_value"),
                "positions_summary": summarize_positions(p.get("positions", {})),
                "has_balance_snapshot": bool(b),
            }
        )

    # 若存在仅 balance 没有 position 的历史记录，也补进去
    existing_order_ids = {str(x.get("order_id", "")).strip() for x in merged if x.get("order_id")}
    existing_fallback_keys = {
        _fallback_key(x.get("date"), x.get("action"), x.get("symbol"), x.get("amount")) for x in merged
    }
    for b in balance_rows:
        trade = b.get("trade", {})
        order_id = str(trade.get("order_id", "")).strip()
        fallback_k = _fallback_key(
            b.get("date"),
            trade.get("action"),
            trade.get("symbol"),
            trade.get("qty"),
        )
        if (order_id and order_id in existing_order_ids) or fallback_k in existing_fallback_keys:
            continue
        account = b.get("account", {})
        merged.append(
            {
                "date": b.get("date"),
                "timestamp_et": b.get("timestamp_et"),
                "timestamp_utc": b.get("timestamp_utc"),
                "id": None,
                "action": trade.get("action"),
                "symbol": trade.get("symbol"),
                "amount": trade.get("qty"),
                "price": trade.get("filled_price"),
                "order_id": order_id,
                "cash": account.get("cash"),
                "equity": account.get("equity"),
                "portfolio_value": account.get("portfolio_value"),
                "positions_summary": "来自 balance 快照",
                "has_balance_snapshot": True,
            }
        )

    # 按日期 + id 排序
    def sort_key(x: Dict[str, Any]):
        return (_record_time_key(x), int(x.get("id") or 0))

    merged.sort(key=sort_key)
    return merged


def print_human_readable(rows: List[Dict[str, Any]], limit: int) -> None:
    print("📚 Alpaca 交易记录（统一视图）")
    print("=" * 80)
    print(f"显示最近: {limit} 条")
    print("=" * 80)

    if not rows:
        print("暂无记录。")
        return

    for idx, row in enumerate(rows, 1):
        print(f"[{idx}] {row.get('date')} | id={row.get('id')}")
        if row.get("timestamp_et") or row.get("timestamp_utc"):
            print(f"  时间: ET={row.get('timestamp_et') or 'N/A'} | UTC={row.get('timestamp_utc') or 'N/A'}")
        print(
            f"  交易: {str(row.get('action', '')).upper()} "
            f"{row.get('symbol')} x {row.get('amount')} @ {format_currency(row.get('price'))}"
        )
        print(f"  订单: {row.get('order_id') or 'N/A'}")
        print(
            f"  账户: cash={format_currency(row.get('cash'))}, "
            f"equity={format_currency(row.get('equity'))}, "
            f"portfolio={format_currency(row.get('portfolio_value'))}"
        )
        print(f"  持仓: {row.get('positions_summary')}")
        print(f"  balance快照: {'是' if row.get('has_balance_snapshot') else '否'}")
        print("-" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="查询最近 N 条交易记录（统一视图）")
    parser.add_argument("--limit", type=int, default=50, help="最近记录条数，默认 50")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    if args.limit <= 0:
        print("❌ --limit 必须为正整数")
        raise SystemExit(1)

    base = resolve_skill_data_dir()
    position_file = base / "position" / "position.jsonl"
    balance_file = base / "balance" / "balance.jsonl"

    position_rows = read_jsonl(position_file)
    balance_rows = read_jsonl(balance_file)
    merged = unified_records(position_rows, balance_rows)
    merged = merged[-args.limit:]

    if args.json:
        print(
            json.dumps(
                {
                    "records_dir": str(base),
                    "limit": args.limit,
                    "position_file": str(position_file),
                    "balance_file": str(balance_file),
                    "count": len(merged),
                    "records": merged,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print_human_readable(merged, args.limit)


if __name__ == "__main__":
    main()
