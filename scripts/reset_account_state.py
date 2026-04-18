#!/usr/bin/env python3
"""
重置本地账户记录状态。

作用：
- 清理 position.jsonl
- 清理 balance.jsonl

注意：
- 仅清理本地记录文件，不会修改 Alpaca 真实账户持仓/余额。
- 下次执行交易脚本时会自动重新创建记录文件。

用法:
    python skills/alpaca-live-trading/scripts/reset_account_state.py
    python skills/alpaca-live-trading/scripts/reset_account_state.py --yes
"""

from __future__ import annotations

import argparse
from pathlib import Path

def resolve_skill_data_dir() -> Path:
    # skills/alpaca-live-trading/scripts -> skills/alpaca-live-trading/data
    return Path(__file__).resolve().parent.parent / "data"


def remove_if_exists(path: Path) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="重置本地账户记录状态（删除 position/balance jsonl）")
    parser.add_argument("--yes", action="store_true", help="跳过确认直接执行")
    args = parser.parse_args()

    base = resolve_skill_data_dir()
    position_file = base / "position" / "position.jsonl"
    balance_file = base / "balance" / "balance.jsonl"

    if not args.yes:
        print("⚠️ 即将删除以下本地记录文件：")
        print(f"  - {position_file}")
        print(f"  - {balance_file}")
        confirm = input("确认执行？输入 yes 继续: ").strip().lower()
        if confirm != "yes":
            print("已取消。")
            return

    deleted_position = remove_if_exists(position_file)
    deleted_balance = remove_if_exists(balance_file)

    print("✅ 重置完成（本地记录）")
    print(f"  position.jsonl: {'已删除' if deleted_position else '不存在'}")
    print(f"  balance.jsonl : {'已删除' if deleted_balance else '不存在'}")
    print("  下次交易会自动重新创建文件。")


if __name__ == "__main__":
    main()
