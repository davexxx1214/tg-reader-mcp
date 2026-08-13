#!/usr/bin/env python3
"""Explicitly rebuild signed V4.7 ownership state from the current Paper account."""

from __future__ import annotations

import argparse
import hashlib
import json

from _config import (
    get_factor_execution_config,
    get_factor_portfolio_config,
    load_config,
)
from factor_portfolio import effective_factor_config
from query_alpaca_account import get_alpaca_client, get_open_orders
from run_analysis_trade_pipeline import (
    _account_fingerprint,
    _assert_no_conflicting_open_orders,
    _exclusive_run_lock,
    _load_account,
    _load_factor_basket,
    _load_or_create_state_key,
    _resolve_root_path,
    _write_factor_execution_state,
)


def recover(config: dict, confirm_target_sha256: str) -> dict:
    execution = get_factor_execution_config(config)
    selection = get_factor_portfolio_config(config)
    approved = str(execution["approved_target_sha256"])
    if confirm_target_sha256.lower() != approved:
        raise RuntimeError("Recovery confirmation does not match the approved target SHA-256")

    state_path = _resolve_root_path(execution["state_path"])
    with _exclusive_run_lock(state_path):
        journal_path = _resolve_root_path(execution["journal_path"])
        if journal_path.exists():
            raise RuntimeError("Cannot recover ownership while an execution journal is active")
        target_path = _resolve_root_path(execution["target_path"])
        target_payload = json.loads(target_path.read_text(encoding="utf-8"))
        basket = _load_factor_basket(
            target_path,
            execution_date=str(target_payload["decision_date"]),
            expected_research_id="v4_7_0001",
            expected_holdings=10,
            maximum_age_days=None,
            approved_sha256=approved,
            expected_effective_config=effective_factor_config(selection),
        )
        client = get_alpaca_client()
        if client is None:
            raise RuntimeError("Alpaca client unavailable for ownership recovery")
        _assert_no_conflicting_open_orders(get_open_orders(client), set())
        account, positions, _ = _load_account(skip_refresh=False, execute_trades=True)
        target_symbols = set(basket["target_weights"])
        unexpected = sorted(
            str(position.get("symbol", "")).upper()
            for position in positions
            if str(position.get("symbol", "")).upper() not in target_symbols
        )
        shorts = sorted(
            str(position.get("symbol", "")).upper()
            for position in positions
            if float(position.get("qty") or 0.0) < 0.0
            or str(position.get("side", "long")).lower() == "short"
        )
        if unexpected or shorts:
            raise RuntimeError(
                "Recovery requires only long positions from the approved basket; "
                f"unexpected={unexpected}, shorts={shorts}"
            )
        owned = {
            str(position["symbol"]).upper(): float(position["qty"])
            for position in positions
            if float(position.get("qty") or 0.0) > 0.0
        }
        secret = _load_or_create_state_key(
            _resolve_root_path(execution["state_key_path"])
        )
        _write_factor_execution_state(
            state_path,
            owned_quantities=owned,
            target_artifact_sha256=approved,
            target_decision_date=str(basket["decision_date"]),
            secret=secret,
            account_fingerprint=_account_fingerprint(
                str(account["account_number"]), secret
            ),
        )
    return {
        "status": "recovered",
        "target_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
        "owned_quantities": owned,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-target-sha256",
        required=True,
        help="Exact approved V4.7 artifact SHA-256; required to prevent accidental recovery",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            recover(load_config(), args.confirm_target_sha256),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
