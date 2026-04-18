#!/usr/bin/env python3
"""
执行 Alpaca 交易并同步本地记录。

功能：
1. 提交 buy/sell 市价单
2. 成交后重新查询 Alpaca 账户与持仓（真实状态）
3. 更新 position.jsonl
4. 更新 balance.jsonl（含账户总览 + 持仓明细）

用法:
    python skills/alpaca-live-trading/scripts/execute_alpaca_trade.py --action buy --symbol AAPL --qty 1
    python skills/alpaca-live-trading/scripts/execute_alpaca_trade.py --action sell --symbol AAPL --qty 1
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from _config import get_alpaca_credentials, load_config

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
except ImportError:
    print("❌ 缺少 alpaca-py，请安装: pip install alpaca-py")
    raise SystemExit(1)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


DEFAULT_POSITION_SYMBOLS = [
    "NVDA", "MSFT", "AAPL", "GOOG", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "NFLX",
    "PLTR", "COST", "ASML", "AMD", "CSCO", "AZN", "TMUS", "MU", "LIN", "PEP",
    "SHOP", "APP", "INTU", "AMAT", "LRCX", "PDD", "QCOM", "ARM", "INTC", "BKNG",
    "AMGN", "TXN", "ISRG", "GILD", "KLAC", "PANW", "ADBE", "HON", "CRWD", "CEG",
    "ADI", "ADP", "DASH", "CMCSA", "VRTX", "MELI", "SBUX", "CDNS", "ORLY", "SNPS",
    "MSTR", "MDLZ", "ABNB", "MRVL", "CTAS", "TRI", "MAR", "MNST", "CSX", "ADSK",
    "PYPL", "FTNT", "AEP", "WDAY", "REGN", "ROP", "NXPI", "DDOG", "AXON", "ROST",
    "IDXX", "EA", "PCAR", "FAST", "EXC", "TTWO", "XEL", "ZS", "PAYX", "WBD",
    "BKR", "CPRT", "CCEP", "FANG", "TEAM", "CHTR", "KDP", "MCHP", "GEHC", "VRSK",
    "CTSH", "CSGP", "KHC", "ODFL", "DXCM", "TTD", "ON", "BIIB", "LULU", "CDW", "GFS",
    "QQQ",
]

TERMINAL_ORDER_STATUSES = {
    "filled",
    "partially_filled",
    "canceled",
    "expired",
    "rejected",
    "done_for_day",
}


def resolve_skill_data_dir() -> Path:
    # skills/alpaca-live-trading/scripts -> skills/alpaca-live-trading/data
    return Path(__file__).resolve().parent.parent / "data"


def get_dual_timestamps(order) -> Dict[str, str]:
    """
    生成双时区时间戳，避免跨机器时区漂移。
    优先使用 Alpaca 订单成交时间。
    """
    dt = getattr(order, "filled_at", None)
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    if ZoneInfo is not None:
        et_dt = dt.astimezone(ZoneInfo("US/Eastern"))
    else:
        # fallback: 若极端情况下不可用 zoneinfo，则保留 UTC，避免脚本失败
        et_dt = dt.astimezone(timezone.utc)

    return {
        "date": et_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_et": et_dt.isoformat(timespec="seconds"),
        "timestamp_utc": dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def normalize_positions(raw_positions: Dict[str, Any]) -> Dict[str, Any]:
    out = {symbol: 0 for symbol in DEFAULT_POSITION_SYMBOLS}
    for symbol, qty in raw_positions.items():
        if symbol == "CASH":
            continue
        out[symbol] = int(float(qty))
    out["CASH"] = float(raw_positions.get("CASH", 0.0))
    return out


def get_next_id(jsonl_path: Path) -> int:
    if not jsonl_path.exists():
        return 0
    current = 0
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                current = max(current, int(row.get("id", 0)))
            except Exception:
                continue
    return current


def append_jsonl(jsonl_path: Path, row: Dict[str, Any]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_positions_from_alpaca(account, positions) -> Dict[str, Any]:
    raw = {"CASH": float(account.cash)}
    for pos in positions:
        raw[pos.symbol] = int(float(pos.qty))
    return normalize_positions(raw)


def build_positions_details(positions) -> List[Dict[str, Any]]:
    details = []
    for pos in positions:
        details.append(
            {
                "symbol": pos.symbol,
                "qty": float(pos.qty),
                "filled_price": float(pos.avg_entry_price),  # 当前持仓口径下的成交均价（成本）
                "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
                "side": pos.side.value if hasattr(pos.side, "value") else str(pos.side),
            }
        )
    return details


def order_status_value(order: Any) -> str:
    status = getattr(order, "status", "")
    return status.value if hasattr(status, "value") else str(status)


def to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="执行/管理 Alpaca 交易并更新 position/balance")
    parser.add_argument("--action", choices=["buy", "sell"], help="交易动作")
    parser.add_argument("--symbol", help="股票代码，如 AAPL")
    parser.add_argument("--qty", type=int, help="交易股数，正整数")
    parser.add_argument("--order-type", choices=["market", "limit"], default="market", help="订单类型")
    parser.add_argument("--limit-price", type=float, help="限价单价格（order-type=limit 时必填）")
    parser.add_argument("--wait-seconds", type=int, default=15, help="提交后轮询订单状态秒数，默认 15")
    parser.add_argument("--cancel-order-id", help="取消指定订单")
    parser.add_argument("--cancel-all-open", action="store_true", help="取消所有未完成订单")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    config = load_config()
    api_key, secret_key, paper = get_alpaca_credentials(config)
    client = TradingClient(api_key, secret_key, paper=paper)
    # 订单管理操作
    if args.cancel_order_id:
        client.cancel_order_by_id(args.cancel_order_id)
        result = {
            "operation": "cancel_order",
            "order_id": args.cancel_order_id,
            "status": "requested",
            "paper": bool(paper),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            mode = "Paper Trading" if paper else "Live Trading"
            print(f"✅ 已提交撤单请求 ({mode})")
            print(f"  订单号: {args.cancel_order_id}")
        raise SystemExit(0)

    if args.cancel_all_open:
        responses = client.cancel_orders()
        out = []
        for r in responses:
            out.append(
                {
                    "id": str(getattr(r, "id", "")),
                    "status_code": int(getattr(r, "status", 0))
                    if getattr(r, "status", None) is not None
                    else None,
                }
            )
        result = {
            "operation": "cancel_all_open",
            "cancelled_count": len(out),
            "responses": out,
            "paper": bool(paper),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            mode = "Paper Trading" if paper else "Live Trading"
            print(f"✅ 已提交批量撤单 ({mode})")
            print(f"  撤单数量: {len(out)}")
        raise SystemExit(0)

    # 交易提交参数校验
    if not args.action:
        print("❌ 缺少操作，请使用 --action 或撤单参数")
        raise SystemExit(1)
    if not args.symbol:
        print("❌ --symbol 必填")
        raise SystemExit(1)
    if args.qty is None or args.qty <= 0:
        print("❌ --qty 必须为正整数")
        raise SystemExit(1)
    if args.order_type == "limit" and (args.limit_price is None or args.limit_price <= 0):
        print("❌ 限价单必须提供有效的 --limit-price")
        raise SystemExit(1)
    if args.order_type == "market" and args.limit_price is not None:
        print("❌ 市价单不需要 --limit-price")
        raise SystemExit(1)
    if args.wait_seconds < 0:
        print("❌ --wait-seconds 不能小于 0")
        raise SystemExit(1)

    symbol = args.symbol.upper()
    side = OrderSide.BUY if args.action == "buy" else OrderSide.SELL

    if args.order_type == "market":
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=args.qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
    else:
        order_request = LimitOrderRequest(
            symbol=symbol,
            qty=args.qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=args.limit_price,
        )

    order = client.submit_order(order_data=order_request)
    current_status = order_status_value(order)
    for _ in range(args.wait_seconds):
        time.sleep(1)
        order = client.get_order_by_id(order.id)
        current_status = order_status_value(order)
        if current_status in TERMINAL_ORDER_STATUSES:
            break

    # 每笔交易后都记录快照；即便未成交也保留审计轨迹
    account = client.get_account()
    positions = client.get_all_positions()
    normalized_positions = build_positions_from_alpaca(account, positions)
    positions_details = build_positions_details(positions)

    ts = get_dual_timestamps(order)
    filled_price = to_float_or_none(getattr(order, "filled_avg_price", None))
    limit_price = to_float_or_none(getattr(order, "limit_price", None))

    base_dir = resolve_skill_data_dir()
    position_file = base_dir / "position" / "position.jsonl"
    balance_file = base_dir / "balance" / "balance.jsonl"

    next_id = get_next_id(position_file) + 1
    append_jsonl(
        position_file,
        {
            "date": ts["date"],
            "timestamp_et": ts["timestamp_et"],
            "timestamp_utc": ts["timestamp_utc"],
            "id": next_id,
            "this_action": {
                "action": args.action,
                "symbol": symbol,
                "amount": args.qty,
                "price": filled_price,
                "order_type": args.order_type,
                "limit_price": limit_price,
                "order_status": current_status,
                "source": "alpaca-skill",
                "order_id": str(order.id),
            },
            "positions": normalized_positions,
        },
    )

    append_jsonl(
        balance_file,
        {
            "date": ts["date"],
            "timestamp_et": ts["timestamp_et"],
            "timestamp_utc": ts["timestamp_utc"],
            "trade": {
                "action": args.action,
                "symbol": symbol,
                "qty": args.qty,
                "order_type": args.order_type,
                "limit_price": limit_price,
                "order_status": current_status,
                "filled_price": filled_price,
                "order_id": str(order.id),
                "filled_at_et": ts["timestamp_et"],
                "filled_at_utc": ts["timestamp_utc"],
            },
            "account": {
                "account_number": account.account_number,
                "status": account.status.value if hasattr(account.status, "value") else str(account.status),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "equity": float(account.equity),
                "last_equity": float(account.last_equity),
                "long_market_value": float(account.long_market_value),
                "short_market_value": float(account.short_market_value),
                "initial_margin": float(account.initial_margin),
                "maintenance_margin": float(account.maintenance_margin),
                "daytrade_count": account.daytrade_count,
                "pattern_day_trader": account.pattern_day_trader,
            },
            "positions": positions_details,
        },
    )

    result = {
        "operation": "submit_order",
        "status": current_status,
        "action": args.action,
        "symbol": symbol,
        "qty": args.qty,
        "order_type": args.order_type,
        "limit_price": limit_price,
        "filled_price": filled_price,
        "order_id": str(order.id),
        "paper": bool(paper),
        "position_file": str(position_file),
        "balance_file": str(balance_file),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        mode = "Paper Trading" if paper else "Live Trading"
        icon = "✅" if current_status in {"filled", "partially_filled"} else "⏳"
        print(f"{icon} 订单已提交 ({mode})")
        print(f"  动作: {args.action.upper()} {symbol} x {args.qty}")
        print(f"  类型: {args.order_type.upper()}")
        if limit_price is not None:
            print(f"  限价: ${limit_price:.2f}")
        print(f"  状态: {current_status}")
        print(f"  成交均价: ${filled_price:.2f}" if filled_price is not None else "  成交均价: N/A")
        print(f"  订单号: {order.id}")
        print(f"  position: {position_file}")
        print(f"  balance: {balance_file}")

    if current_status in {"canceled", "expired", "rejected"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
