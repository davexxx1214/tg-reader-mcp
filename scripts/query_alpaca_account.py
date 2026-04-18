#!/usr/bin/env python3
"""
查询 Alpaca 交易账户状态

用法:
    python query_alpaca_account.py           # 查询账户余额和持仓
    python query_alpaca_account.py --orders  # 同时显示最近订单
    python query_alpaca_account.py --json    # 以 JSON 格式输出
"""

from __future__ import annotations

import sys
import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

# 将 scripts 目录加入 Python 路径以导入 _config
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import load_config, get_alpaca_credentials

# Alpaca 导入
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    print("WARNING: alpaca-py not installed. Run: pip install alpaca-py")

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def resolve_skill_data_dir() -> Path:
    # skills/alpaca-live-trading/scripts -> skills/alpaca-live-trading/data
    return Path(__file__).resolve().parent.parent / "data"


def get_now_timestamps() -> Dict[str, str]:
    now_utc = datetime.now(timezone.utc)
    if ZoneInfo is not None:
        now_et = now_utc.astimezone(ZoneInfo("US/Eastern"))
    else:
        now_et = now_utc
    return {
        "date": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_et": now_et.isoformat(timespec="seconds"),
        "timestamp_utc": now_utc.isoformat(timespec="seconds"),
    }


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_next_record_id(path: Path) -> int:
    if not path.exists():
        return 1
    current = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            try:
                current = max(current, int(row.get("id", 0)))
            except Exception:
                continue
    return current + 1


def build_position_snapshot(positions: List[Dict[str, Any]], cash: float) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {"CASH": float(cash)}
    for pos in positions:
        symbol = str(pos.get("symbol", "")).upper()
        if not symbol:
            continue
        try:
            qty = float(pos.get("qty", 0))
        except Exception:
            qty = 0.0
        if abs(qty) > 0:
            snapshot[symbol] = qty
    return snapshot


def persist_account_snapshot(
    account: Dict[str, Any],
    positions: List[Dict[str, Any]],
    *,
    source: str = "query_alpaca_account",
    action: str = "refresh_snapshot",
) -> Dict[str, Any]:
    """
    将最新账户/持仓快照追加到本地记录文件，供分析、风控和审计复用。
    """
    base_dir = resolve_skill_data_dir()
    position_file = base_dir / "position" / "position.jsonl"
    balance_file = base_dir / "balance" / "balance.jsonl"
    created: List[str] = []
    if not position_file.exists():
        created.append(str(position_file))
    if not balance_file.exists():
        created.append(str(balance_file))

    ts = get_now_timestamps()
    next_id = get_next_record_id(position_file)
    append_jsonl(
        position_file,
        {
            "date": ts["date"],
            "timestamp_et": ts["timestamp_et"],
            "timestamp_utc": ts["timestamp_utc"],
            "id": next_id,
            "this_action": {
                "action": action,
                "symbol": "N/A",
                "amount": 0,
                "price": None,
                "order_type": "snapshot",
                "order_status": "snapshot",
                "source": source,
                "order_id": "",
            },
            "positions": build_position_snapshot(positions, account.get("cash", 0.0)),
        },
    )
    append_jsonl(
        balance_file,
        {
            "date": ts["date"],
            "timestamp_et": ts["timestamp_et"],
            "timestamp_utc": ts["timestamp_utc"],
            "trade": {
                "action": action,
                "symbol": "N/A",
                "qty": 0,
                "order_type": "snapshot",
                "order_status": "snapshot",
                "filled_price": None,
                "order_id": "",
                "source": source,
            },
            "account": account,
            "positions": positions,
        },
    )
    return {
        "position_file": str(position_file),
        "balance_file": str(balance_file),
        "created_files": created,
        "position_record_id": next_id,
    }


def ensure_local_record_files(account: Dict[str, Any], positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    查询账户时自动初始化本地记录文件：
    - data/position/position.jsonl
    - data/balance/balance.jsonl
    """
    base_dir = resolve_skill_data_dir()
    position_file = base_dir / "position" / "position.jsonl"
    balance_file = base_dir / "balance" / "balance.jsonl"
    created: List[str] = []
    ts = get_now_timestamps()

    if not position_file.exists():
        append_jsonl(
            position_file,
            {
                "date": ts["date"],
                "timestamp_et": ts["timestamp_et"],
                "timestamp_utc": ts["timestamp_utc"],
                "id": 1,
                "this_action": {
                    "action": "init_snapshot",
                    "symbol": "N/A",
                    "amount": 0,
                    "price": None,
                    "order_type": "snapshot",
                    "order_status": "snapshot",
                    "source": "query_alpaca_account",
                    "order_id": "",
                },
                "positions": build_position_snapshot(positions, account.get("cash", 0.0)),
            },
        )
        created.append(str(position_file))

    if not balance_file.exists():
        append_jsonl(
            balance_file,
            {
                "date": ts["date"],
                "timestamp_et": ts["timestamp_et"],
                "timestamp_utc": ts["timestamp_utc"],
                "trade": {
                    "action": "init_snapshot",
                    "symbol": "N/A",
                    "qty": 0,
                    "order_type": "snapshot",
                    "order_status": "snapshot",
                    "filled_price": None,
                    "order_id": "",
                },
                "account": account,
                "positions": positions,
            },
        )
        created.append(str(balance_file))

    return {
        "position_file": str(position_file),
        "balance_file": str(balance_file),
        "created_files": created,
    }


def get_alpaca_client() -> Optional[TradingClient]:
    """
    获取 Alpaca 客户端（从 config.yaml 读取凭证）

    Returns:
        TradingClient 实例或 None
    """
    if not ALPACA_AVAILABLE:
        return None

    config = load_config()
    api_key, secret_key, paper = get_alpaca_credentials(config)

    return TradingClient(api_key, secret_key, paper=paper)


def get_account_info(client: TradingClient) -> Dict[str, Any]:
    """
    获取账户信息

    Args:
        client: Alpaca 客户端

    Returns:
        账户信息字典
    """
    account = client.get_account()

    return {
        "account_number": account.account_number,
        "status": account.status.value if hasattr(account.status, 'value') else str(account.status),
        "currency": account.currency,
        "cash": float(account.cash),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
        "equity": float(account.equity),
        "last_equity": float(account.last_equity),
        "long_market_value": float(account.long_market_value),
        "short_market_value": float(account.short_market_value),
        "initial_margin": float(account.initial_margin),
        "maintenance_margin": float(account.maintenance_margin),
        "daytrade_count": account.daytrade_count,
        "pattern_day_trader": account.pattern_day_trader,
    }


def get_positions(client: TradingClient) -> List[Dict[str, Any]]:
    """
    获取当前持仓

    Args:
        client: Alpaca 客户端

    Returns:
        持仓列表
    """
    positions = client.get_all_positions()

    result = []
    for pos in positions:
        result.append({
            "symbol": pos.symbol,
            "qty": float(pos.qty),
            "avg_entry_price": float(pos.avg_entry_price),
            "market_value": float(pos.market_value),
            "current_price": float(pos.current_price),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": float(pos.unrealized_plpc) * 100,  # 转为百分比
            "side": pos.side.value if hasattr(pos.side, 'value') else str(pos.side),
        })

    return result


def get_recent_orders(client: TradingClient, days: int = 7) -> List[Dict[str, Any]]:
    """
    获取最近订单

    Args:
        client: Alpaca 客户端
        days: 查询天数

    Returns:
        订单列表
    """
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        after=datetime.now() - timedelta(days=days)
    )
    orders = client.get_orders(filter=request)

    result = []
    for order in orders[:20]:  # 最多20条
        result.append({
            "id": str(order.id),
            "symbol": order.symbol,
            "side": order.side.value if hasattr(order.side, 'value') else str(order.side),
            "qty": float(order.qty) if order.qty else 0,
            "filled_qty": float(order.filled_qty) if order.filled_qty else 0,
            "type": order.type.value if hasattr(order.type, 'value') else str(order.type),
            "status": order.status.value if hasattr(order.status, 'value') else str(order.status),
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else 0,
            "created_at": order.created_at.strftime("%Y-%m-%d %H:%M:%S") if order.created_at else "",
            "filled_at": order.filled_at.strftime("%Y-%m-%d %H:%M:%S") if order.filled_at else "",
        })

    return result


def format_currency(value: float) -> str:
    """格式化货币"""
    return f"${value:,.2f}"


def format_percent(value: float) -> str:
    """格式化百分比"""
    return f"{value:+.2f}%"


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="查询 Alpaca 交易账户")
    parser.add_argument("--orders", action="store_true",
                        help="显示最近订单")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 格式输出")
    args = parser.parse_args()

    if not ALPACA_AVAILABLE:
        sys.exit(1)

    # 读取配置判断 paper 模式
    config = load_config()
    paper = config.get("alpaca", {}).get("paper", True)
    mode_str = "Paper Trading (模拟交易)" if paper else "Live Trading (真实交易)"

    print("=" * 60)
    print(f"💰 Alpaca {mode_str} 账户状态")
    print("=" * 60)

    # 获取客户端
    client = get_alpaca_client()
    if not client:
        sys.exit(1)

    print(f"查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    try:
        # 获取账户信息
        account = get_account_info(client)
        # 获取持仓（用于输出 + 初始化本地文件）
        positions = get_positions(client)
        init_result = persist_account_snapshot(
            account,
            positions,
            source="query_alpaca_account",
            action="account_snapshot",
        )

        if args.json:
            output = {"account": account}
            output["records"] = init_result
        else:
            print("📊 账户概览")
            print("-" * 40)
            print(f"  账户号码: {account['account_number']}")
            print(f"  账户状态: {account['status']}")
            print(f"  现金余额: {format_currency(account['cash'])}")
            print(f"  买入能力: {format_currency(account['buying_power'])}")
            print(f"  账户净值: {format_currency(account['equity'])}")
            print(f"  投资组合价值: {format_currency(account['portfolio_value'])}")
            print(f"  多头市值: {format_currency(account['long_market_value'])}")

            # 计算日收益
            daily_change = account['equity'] - account['last_equity']
            daily_change_pct = (daily_change / account['last_equity'] * 100) if account['last_equity'] > 0 else 0
            print(f"  日收益: {format_currency(daily_change)} ({format_percent(daily_change_pct)})")
            print()

        if args.json:
            output["positions"] = positions
        else:
            print("📦 当前持仓")
            print("-" * 40)

            if not positions:
                print("  (无持仓)")
            else:
                total_unrealized = 0
                for pos in positions:
                    print(f"  {pos['symbol']}: {pos['qty']:.0f} 股")
                    print(f"    成本价: {format_currency(pos['avg_entry_price'])}")
                    print(f"    现价: {format_currency(pos['current_price'])}")
                    print(f"    市值: {format_currency(pos['market_value'])}")
                    print(f"    盈亏: {format_currency(pos['unrealized_pl'])} ({format_percent(pos['unrealized_plpc'])})")
                    print()
                    total_unrealized += pos['unrealized_pl']

                print(f"  总未实现盈亏: {format_currency(total_unrealized)}")
            print()

        # 获取订单
        if args.orders:
            orders = get_recent_orders(client)

            if args.json:
                output["orders"] = orders
            else:
                print("📝 最近订单 (7天内)")
                print("-" * 40)

                if not orders:
                    print("  (无订单)")
                else:
                    for order in orders[:10]:
                        status_emoji = "✅" if order['status'] == 'filled' else "⏳" if order['status'] == 'new' else "❌"
                        print(f"  {status_emoji} {order['symbol']} {order['side'].upper()} {order['qty']:.0f}")
                        print(f"    状态: {order['status']} | 成交价: {format_currency(order['filled_avg_price'])}")
                        print(f"    创建: {order['created_at']}")
                        print()

        if args.json:
            print(json.dumps(output, indent=2, ensure_ascii=False))
        elif init_result["created_files"]:
            print("🧩 已自动初始化本地记录文件:")
            for p in init_result["created_files"]:
                print(f"  - {p}")

    except Exception as e:
        print(f"\n❌ 查询失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("=" * 60)
    if paper:
        print("💡 提示: 这是 Paper Trading (模拟交易) 账户，不涉及真实资金")
    else:
        print("⚠️ 注意: 这是 Live Trading (真实交易) 账户")


if __name__ == "__main__":
    main()
