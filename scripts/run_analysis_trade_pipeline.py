#!/usr/bin/env python3
"""Run the single frozen V4.7 ten-stock strategy on Alpaca Paper."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import math
import os
import secrets
import subprocess
import sys
from ctypes import wintypes
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
CASH_RESERVE_USD = 25.0
sys.path.insert(0, str(SCRIPT_DIR))

from _config import (
    get_factor_execution_config,
    get_factor_portfolio_config,
    load_config,
)
from _file_lock import exclusive_file_lock
from query_alpaca_account import (
    get_account_info,
    get_alpaca_client,
    get_open_orders,
    get_positions,
    persist_account_snapshot,
)
from query_stock_prices import _fetch_alpaca_snapshots
from factor_portfolio import (
    FactorPortfolioError,
    allocate_score_tilt,
    effective_config_sha256,
    effective_factor_config,
)


def _load_factor_basket(
    path: Path,
    *,
    execution_date: str,
    expected_research_id: str,
    expected_holdings: int,
    maximum_age_days: Optional[int],
    approved_sha256: str = "",
    expected_method: str = "v4_7_factor_selection_score_tilt",
    expected_allocation_method: str = "score_tilt",
    expected_effective_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Load one immutable frozen monthly ten-stock basket, failing closed."""
    if not path.is_file():
        raise RuntimeError(f"Factor target does not exist: {path}")
    try:
        raw_payload = path.read_bytes()
        payload = json.loads(raw_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Factor target is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Factor target must be a JSON object")
    artifact_hash = hashlib.sha256(raw_payload).hexdigest()
    if not approved_sha256 or artifact_hash != approved_sha256:
        raise RuntimeError("Factor target SHA-256 is not independently approved")
    if payload.get("method") != expected_method:
        raise RuntimeError("Factor target method is not the configured frozen selector")
    if payload.get("parameter_mode") != "frozen":
        raise RuntimeError("Factor execution requires a frozen target")
    if payload.get("research_id") != expected_research_id:
        raise RuntimeError("Factor target research_id does not match config")
    if payload.get("allocation_method") != expected_allocation_method:
        raise RuntimeError("Factor target allocation method does not match config")
    if expected_allocation_method == "score_tilt":
        if expected_effective_config is None:
            raise RuntimeError("Factor target expected effective config is missing")
        expected_config = dict(expected_effective_config)
        actual_config = payload.get("effective_config")
        expected_config_hash = effective_config_sha256(expected_config)
        if (
            payload.get("score_power") != 6.0
            or actual_config != expected_config
            or payload.get("effective_config_sha256") != expected_config_hash
        ):
            raise RuntimeError("Factor target effective config does not match frozen V4.7")
    try:
        target_day = date.fromisoformat(str(payload["decision_date"]))
        execution_day = date.fromisoformat(str(execution_date))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("Factor target has an invalid decision_date") from exc
    age_days = (execution_day - target_day).days
    if age_days < 0:
        raise RuntimeError("Factor target decision_date is in the future")
    if maximum_age_days is not None and age_days > maximum_age_days:
        raise RuntimeError(
            f"Factor target is stale: decision_date={target_day.isoformat()} age_days={age_days}"
        )
    selected = payload.get("selected")
    if not isinstance(selected, list) or len(selected) != expected_holdings:
        raise RuntimeError(f"Factor target must contain exactly {expected_holdings} stocks")
    ordered = sorted(selected, key=lambda row: int(row.get("selection_rank", 0)))
    tickers: List[str] = []
    security_ids: List[str] = []
    for expected_rank, row in enumerate(ordered, 1):
        if not isinstance(row, dict) or int(row.get("selection_rank", 0)) != expected_rank:
            raise RuntimeError("Factor target selection ranks must be consecutive")
        ticker = str(row.get("ticker", "")).upper().strip()
        security_id = str(row.get("security_id", "")).strip()
        if (
            not ticker
            or len(ticker) > 15
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for character in ticker)
            or not security_id
        ):
            raise RuntimeError("Factor target contains an invalid ticker or security_id")
        tickers.append(ticker)
        security_ids.append(security_id)
    if len(set(tickers)) != expected_holdings or len(set(security_ids)) != expected_holdings:
        raise RuntimeError("Factor target contains duplicate tickers or security_ids")
    if expected_allocation_method != "score_tilt":
        raise RuntimeError("V4.7 only supports the frozen score-tilt allocation")
    else:
        try:
            target_weights = {
                ticker: float(row["target_weight"])
                for ticker, row in zip(tickers, ordered)
            }
            scores = [float(row["score"]) for row in ordered]
            industries = [str(row["ff_industry_12"]).strip() for row in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Factor target score-tilt weights are invalid") from exc
        if any(not math.isfinite(score) for score in scores) or any(
            not industry for industry in industries
        ):
            raise RuntimeError("Factor target score or industry is invalid")
        if (
            abs(sum(target_weights.values()) - 1.0) > 1e-7
            or min(target_weights.values()) < 0.05 - 1e-7
            or max(target_weights.values()) > 0.20 + 1e-7
            or any(not math.isfinite(value) for value in target_weights.values())
        ):
            raise RuntimeError("Factor target score-tilt weights violate frozen bounds")
        score_order = sorted(range(expected_holdings), key=lambda index: (-scores[index], index))
        ordered_weights = [target_weights[tickers[index]] for index in score_order]
        if any(left < right - 1e-7 for left, right in zip(ordered_weights, ordered_weights[1:])):
            raise RuntimeError("Factor target score-weight monotonicity is violated")
        for industry in set(industries):
            industry_weight = sum(
                target_weights[ticker]
                for ticker, row_industry in zip(tickers, industries)
                if row_industry == industry
            )
            if industry_weight > 0.35 + 1e-7:
                raise RuntimeError("Factor target industry weight exceeds 35%")
        try:
            recomputed = allocate_score_tilt(
                ordered,
                power=float(expected_effective_config["score_power"]),
                minimum_weight=float(expected_effective_config["minimum_weight"]),
                maximum_weight=float(expected_effective_config["maximum_weight"]),
                maximum_industry_weight=float(expected_effective_config["maximum_industry_weight"]),
            )
        except (FactorPortfolioError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Factor target frozen score projection cannot be reproduced") from exc
        if any(
            abs(float(row["target_weight"]) - float(expected_row["target_weight"])) > 1e-9
            for row, expected_row in zip(ordered, recomputed)
        ):
            raise RuntimeError("Factor target weights do not match the frozen score projection")
        members_match_predecessor = False
    predecessor = payload.get("predecessor_target")
    if predecessor is not None and (
        not isinstance(predecessor, dict)
        or predecessor.get("research_id") != "v4_6_r1_0001"
        or len(str(predecessor.get("sha256", ""))) != 64
        or Path(str(predecessor.get("artifact_filename", ""))).name
        != str(predecessor.get("artifact_filename", ""))
    ):
        raise RuntimeError("Factor target predecessor link is invalid")
    if expected_allocation_method == "score_tilt":
        if not isinstance(predecessor, dict) or not predecessor.get("artifact_filename"):
            raise RuntimeError("Factor target predecessor artifact is missing")
        predecessor_path = path.parent / str(predecessor["artifact_filename"])
        try:
            predecessor_bytes = predecessor_path.read_bytes()
            predecessor_payload = json.loads(predecessor_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Factor target predecessor artifact cannot be loaded") from exc
        if (
            hashlib.sha256(predecessor_bytes).hexdigest() != predecessor.get("sha256")
            or not isinstance(predecessor_payload, dict)
            or predecessor_payload.get("method") != "v4_6_r1_factor_selection"
            or predecessor_payload.get("research_id") != "v4_6_r1_0001"
        ):
            raise RuntimeError("Factor target predecessor artifact is not authentic")
        predecessor_selected = predecessor_payload.get("selected")
        if not isinstance(predecessor_selected, list) or len(predecessor_selected) != expected_holdings:
            raise RuntimeError("Factor target predecessor membership is invalid")
        predecessor_ordered = sorted(
            predecessor_selected, key=lambda row: int(row.get("selection_rank", 0))
        )
        identity = lambda row: (
            int(row.get("selection_rank", 0)),
            str(row.get("security_id", "")).strip(),
            str(row.get("ticker", "")).upper().strip(),
        )
        members_match_predecessor = [identity(row) for row in ordered] == [
            identity(row) for row in predecessor_ordered
        ]
        if not members_match_predecessor:
            raise RuntimeError("Factor target members or ranks differ from the V4.6 predecessor")
    return {
        "path": str(path),
        "artifact_sha256": artifact_hash,
        "decision_date": target_day.isoformat(),
        "age_days": age_days,
        "research_id": expected_research_id,
        "target_weights": target_weights,
        "base_weights": target_weights,
        "allocation_method": expected_allocation_method,
        "predecessor_target": predecessor,
        "members_match_predecessor": members_match_predecessor,
    }


def _build_factor_rebalance_plan(
    *,
    account: Mapping[str, Any],
    positions: Iterable[Mapping[str, Any]],
    prices: Mapping[str, Any],
    target_weights: Mapping[str, Any],
    reason: str = "v4_7_top10_score_tilt_rebalance",
) -> List[Dict[str, Any]]:
    """Create fractional-share orders for the isolated factor sleeve."""
    equity = float(account.get("equity") or account.get("portfolio_value") or 0.0)
    if equity <= 0.0 or not math.isfinite(equity):
        raise ValueError("Account equity must be positive")
    position_map: Dict[str, Dict[str, float]] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper().strip()
        if not symbol:
            continue
        quantity = abs(float(position.get("qty") or 0.0))
        if str(position.get("side", "long")).lower() == "short":
            quantity = -quantity
        position_map[symbol] = {
            "qty": quantity,
            "current_price": float(position.get("current_price") or 0.0),
        }
    normalized_prices = {
        str(symbol).upper().strip(): float(price) for symbol, price in prices.items()
    }
    normalized_targets = {
        str(symbol).upper().strip(): float(weight)
        for symbol, weight in target_weights.items()
    }
    if (
        len(normalized_targets) != 10
        or any(
            not symbol or not math.isfinite(weight) or weight <= 0.0 or weight > 1.0
            for symbol, weight in normalized_targets.items()
        )
        or abs(sum(normalized_targets.values()) - 1.0) > 1e-9
    ):
        raise ValueError("Factor portfolio requires ten positive target weights summing to 100%")
    plan: List[Dict[str, Any]] = []
    for symbol in sorted(set(position_map) | set(normalized_targets)):
        current_qty = position_map.get(symbol, {}).get("qty", 0.0)
        price = normalized_prices.get(symbol) or position_map.get(symbol, {}).get("current_price", 0.0)
        if price <= 0.0 or not math.isfinite(price):
            raise ValueError(f"Missing valid price for {symbol}")
        target_weight = normalized_targets.get(symbol, 0.0)
        target_qty = round((equity * target_weight) / price, 6)
        delta_qty = target_qty - current_qty
        order_qty = round(abs(delta_qty), 6)
        if order_qty <= 0.0 or order_qty * price < 5.0:
            continue
        plan.append(
            {
                "action": "buy" if delta_qty > 0.0 else "sell",
                "symbol": symbol,
                "qty": order_qty,
                "price": price,
                "estimated_notional": round(order_qty * price, 2),
                "current_qty": current_qty,
                "target_qty": target_qty,
                "target_weight": target_weight,
                "reason": reason,
            }
        )
    return sorted(plan, key=lambda item: (item["action"] != "sell", item["symbol"]))


def _strategy_sleeve(
    account: Mapping[str, Any],
    positions: Iterable[Mapping[str, Any]],
    owned_quantities: Mapping[str, Any],
    *,
    target_symbols: Iterable[str] = (),
    capital_allocation_usd: float,
) -> Dict[str, Any]:
    """Build the dedicated, cash-only V4.7 Paper account sleeve."""
    owned = {
        str(symbol).upper().strip(): float(quantity)
        for symbol, quantity in owned_quantities.items()
        if str(symbol).strip() and float(quantity) > 0.0
    }
    targets = set(_normalized_symbols(target_symbols))
    managed_scope = set(owned) | targets
    filtered: List[Dict[str, Any]] = []
    managed_value = 0.0
    for source in positions:
        symbol = str(source.get("symbol", "")).upper().strip()
        try:
            signed_broker_qty = float(source.get("qty") or 0.0)
            broker_qty = abs(signed_broker_qty)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Alpaca position quantity is invalid for {symbol}") from exc
        if (
            symbol in managed_scope
            and (
                signed_broker_qty < -1e-9
                or str(source.get("side", "long")).strip().lower() == "short"
            )
        ):
            raise RuntimeError(f"V4.7 does not permit a managed short position: {symbol}")
        if symbol not in managed_scope and broker_qty > 0.0:
            raise RuntimeError(f"Dedicated V4.7 account contains an unexpected position: {symbol}")
        if symbol in targets and symbol not in owned:
            owned[symbol] = broker_qty
        if symbol not in owned:
            continue
        if abs(owned[symbol] - broker_qty) > 1e-9:
            raise RuntimeError(f"Owned quantity does not match Alpaca for {symbol}")
        row = dict(source)
        filtered.append(row)
        try:
            value = float(row.get("market_value") or 0.0)
            if value == 0.0:
                value = float(row.get("qty") or 0.0) * float(row.get("current_price") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        managed_value += max(value, 0.0)
    missing_owned = sorted(set(owned) - {str(row.get("symbol", "")).upper() for row in filtered})
    if missing_owned:
        raise RuntimeError(
            f"Owned position is missing at Alpaca: {', '.join(missing_owned)}; recover ownership state"
        )
    try:
        cash = max(float(account.get("cash") or 0.0), 0.0)
        total_equity = max(float(account.get("equity") or account.get("portfolio_value") or 0.0), 0.0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Alpaca account has invalid cash or equity") from exc
    available = min(cash + managed_value, total_equity)
    notional = min(float(capital_allocation_usd), available) - CASH_RESERVE_USD
    if notional <= 0.0 or not math.isfinite(notional):
        raise RuntimeError("Factor strategy sleeve has no investable cash or managed assets")
    return {
        "positions": filtered,
        "owned_quantities": owned,
        "managed_symbols": sorted(set(owned) | targets),
        "cash": cash,
        "managed_market_value": managed_value,
        "notional": notional,
        "current_exposure": min(max(managed_value / notional, 0.0), 1.0),
    }


def validate_factor_execution(
    *, execute_trades: bool, paper_only: bool, alpaca_paper: bool
) -> None:
    if not paper_only:
        raise ValueError("V4.7 is an immutable Paper-only execution strategy")
    if execute_trades and not alpaca_paper:
        raise ValueError("Factor execution is restricted to Alpaca Paper trading")


def _assert_no_conflicting_open_orders(
    open_orders: Iterable[Mapping[str, Any]],
    managed_symbols: Iterable[str],
    *,
    allowed_client_order_ids: Iterable[str] = (),
) -> None:
    """Reject every non-journal order because V4.7 owns the dedicated account."""
    _normalized_symbols(managed_symbols)
    allowed = {str(value) for value in allowed_client_order_ids}
    conflicts = sorted(
        {
            str(order.get("symbol", "")).upper().strip()
            for order in open_orders
            if str(order.get("client_order_id", "")) not in allowed
        }
    )
    if conflicts:
        raise RuntimeError(
            f"Conflicting open Alpaca order exists for: {', '.join(conflicts)}"
        )


def _confirmed_owned_quantities(
    positions: Iterable[Mapping[str, Any]], owned_symbols: Iterable[str]
) -> Dict[str, float]:
    allowed = set(_normalized_symbols(owned_symbols))
    confirmed: Dict[str, float] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper().strip()
        if symbol not in allowed:
            continue
        try:
            quantity = abs(float(position.get("qty") or 0.0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Post-trade Alpaca quantity is invalid for {symbol}") from exc
        if quantity > 1e-9:
            confirmed[symbol] = quantity
    return confirmed


def _state_hmac(payload: Mapping[str, Any], secret: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "state_hmac_sha256"}
    message = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(f"factor-execution-state:{secret}".encode("utf-8")).digest()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _journal_hmac(payload: Mapping[str, Any], secret: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "journal_hmac_sha256"}
    message = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(f"factor-execution-journal:{secret}".encode("utf-8")).digest()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _account_fingerprint(account_number: str, secret: str) -> str:
    """Bind local execution state to one Alpaca account without storing its ID."""
    number = str(account_number).strip()
    if not number or not secret:
        raise RuntimeError("Alpaca account fingerprint inputs are missing")
    key = hashlib.sha256(f"factor-account:{secret}".encode("utf-8")).digest()
    return hmac.new(key, number.encode("utf-8"), hashlib.sha256).hexdigest()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _windows_dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Protect state-key bytes for the current Windows user via DPAPI."""
    if sys.platform != "win32":
        raise RuntimeError("Windows DPAPI is unavailable on this platform")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(
        len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    destination = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    if decrypt:
        success = function(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(destination)
        )
    else:
        success = function(
            ctypes.byref(source), "V4.6-R1 factor state key", None, None, None, 0x1,
            ctypes.byref(destination),
        )
    if not success:
        raise RuntimeError(f"Windows DPAPI failed with error {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def _load_or_create_state_key(path: Path) -> str:
    """Return a stable signing key protected by DPAPI or POSIX file permissions."""
    if path.exists():
        stored = path.read_text(encoding="ascii").strip()
        if sys.platform == "win32":
            if not stored.startswith("dpapi-v1:"):
                raise RuntimeError("Factor execution state key is not protected by Windows DPAPI")
            try:
                encrypted = base64.b64decode(stored.split(":", 1)[1], validate=True)
                key = _windows_dpapi(encrypted, decrypt=True).decode("ascii").lower()
            except (ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("Factor execution state signing key is invalid") from exc
        else:
            key = stored.lower()
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise RuntimeError("Factor execution state signing key is invalid")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    stored = (
        "dpapi-v1:" + base64.b64encode(_windows_dpapi(key.encode("ascii"))).decode("ascii")
        if sys.platform == "win32"
        else key
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.write(descriptor, (stored + "\n").encode("ascii"))
    finally:
        os.close(descriptor)
    return key


def _load_factor_execution_state(
    path: Path,
    *,
    secret: str = "",
    account_fingerprint: str = "",
) -> Dict[str, Any]:
    if not path.exists():
        return {
            "owned_quantities": {},
            "target_decision_date": None,
            "target_artifact_sha256": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Factor execution state is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 2
        or payload.get("strategy") != "v4_7_top10_score_tilt"
        or not isinstance(payload.get("owned_quantities"), dict)
    ):
        raise RuntimeError("Factor execution state contract is invalid")
    if not secret or not hmac.compare_digest(
        str(payload.get("state_hmac_sha256", "")), _state_hmac(payload, secret)
    ):
        raise RuntimeError("Factor execution state authentication failed")
    stored_fingerprint = str(payload.get("account_fingerprint", ""))
    if len(stored_fingerprint) != 64:
        raise RuntimeError("Factor execution state account binding is invalid")
    if account_fingerprint and not hmac.compare_digest(
        stored_fingerprint, str(account_fingerprint)
    ):
        raise RuntimeError("Factor execution state belongs to a different Alpaca account")
    try:
        owned = {
            str(symbol).upper().strip(): float(quantity)
            for symbol, quantity in payload["owned_quantities"].items()
        }
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Factor execution state owned quantities are invalid") from exc
    if any(not symbol or quantity <= 0.0 for symbol, quantity in owned.items()):
        raise RuntimeError("Factor execution state owned quantities are invalid")
    try:
        target_decision_date = date.fromisoformat(str(payload["target_decision_date"])).isoformat()
    except (KeyError, ValueError) as exc:
        raise RuntimeError("Factor execution state target_decision_date is invalid") from exc
    target_hash = str(payload.get("target_artifact_sha256", "")).lower()
    if len(target_hash) != 64 or any(character not in "0123456789abcdef" for character in target_hash):
        raise RuntimeError("Factor execution state target_artifact_sha256 is invalid")
    return {
        **payload,
        "owned_quantities": owned,
        "target_decision_date": target_decision_date,
        "target_artifact_sha256": target_hash,
    }


def _validate_factor_target_transition(
    basket: Mapping[str, Any], state: Mapping[str, Any]
) -> None:
    prior_date_value = state.get("target_decision_date")
    if not prior_date_value:
        return
    prior_date = date.fromisoformat(str(prior_date_value))
    basket_date = date.fromisoformat(str(basket.get("decision_date")))
    if basket_date < prior_date:
        raise RuntimeError("Factor target is older than the last successfully executed basket")
    if (
        basket_date == prior_date
        and str(basket.get("artifact_sha256")) != str(state.get("target_artifact_sha256"))
    ):
        raise RuntimeError("Factor target changed after this monthly basket was executed")


def _resolve_root_path(value: str) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else ROOT_DIR / path


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _latest_local_account() -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    balance_path = ROOT_DIR / "data" / "balance" / "balance.jsonl"
    for row in reversed(_read_jsonl(balance_path)):
        account = row.get("account")
        positions = row.get("positions")
        if isinstance(account, dict) and isinstance(positions, list):
            return dict(account), list(positions)
    return {}, []


def validate_run_options(
    *, execute_trades: bool, skip_account_refresh: bool, execution_date: str = ""
) -> None:
    if execute_trades and skip_account_refresh:
        raise ValueError("Live execution requires a fresh Alpaca account snapshot")
    if execute_trades and execution_date:
        raise ValueError("Live execution cannot use a manually supplied execution date")


def _execute_trade_plan(
    trade_plan: List[Dict[str, Any]],
    *,
    require_paper: bool = False,
    available_cash: Optional[float] = None,
) -> List[Dict[str, Any]]:
    exec_script = SCRIPT_DIR / "execute_alpaca_trade.py"
    results: List[Dict[str, Any]] = []
    remaining_cash = float(available_cash) if available_cash is not None else math.inf
    if require_paper and (not math.isfinite(remaining_cash) or remaining_cash < 0.0):
        raise ValueError("Paper execution requires a finite non-negative cash reservation")
    for item in trade_plan:
        action = str(item.get("action", "")).lower().strip()
        symbol = str(item.get("symbol", "")).upper().strip()
        qty = float(item.get("qty", 0))
        if action not in {"buy", "sell"} or not symbol or qty <= 0:
            results.append({"status": "skipped", "input": item, "reason": "invalid action/symbol/qty"})
            continue

        command = [
                sys.executable,
                str(exec_script),
                "--action",
                action,
                "--symbol",
                symbol,
                "--qty",
                str(qty),
                "--wait-seconds",
                "0",
                "--json",
            ]
        client_order_id = str(item.get("client_order_id", "")).strip()
        if client_order_id:
            command.extend(["--client-order-id", client_order_id])
        if require_paper:
            command.append("--require-paper")
            command.append("--require-long-only")
            if action == "buy":
                price = float(item.get("price") or 0.0)
                if price <= 0.0 or not math.isfinite(price):
                    results.append(
                        {"status": "skipped", "input": item, "reason": "invalid limit price"}
                    )
                    break
                maximum_cost = qty * price
                if maximum_cost > remaining_cash + 1e-6:
                    results.append(
                        {
                            "status": "deferred_cash_reservation",
                            "input": item,
                            "remaining_cash": remaining_cash,
                        }
                    )
                    continue
                remaining_cash -= maximum_cost
                command.extend(
                    [
                        "--order-type", "limit",
                        "--limit-price", str(price),
                        "--require-cash-only",
                    ]
                )
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            results.append(
                {
                    "status": "failed",
                    "input": item,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            results.append(
                {
                    "status": "failed_non_json",
                    "input": item,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            continue
        results.append({"status": "ok", "trade": payload})
    return results


def _prepare_execution_journal(
    path: Path,
    trade_plan: List[Dict[str, Any]],
    *,
    target_sha256: str,
    execution_date: str,
    strategy: str = "v4_7_top10_score_tilt",
    target_artifact_path: Optional[Path] = None,
    target_method: str = "v4_7_factor_selection_score_tilt",
    account_fingerprint: str = "",
    secret: str = "",
    owned_quantities_before: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Persist deterministic order intents before any broker-side mutation."""
    if path.exists():
        raise RuntimeError(
            f"Unresolved factor execution journal exists: {path}; reconcile it before retrying"
        )
    journaled: List[Dict[str, Any]] = []
    for index, source in enumerate(trade_plan, 1):
        item = dict(source)
        intent = json.dumps(
            {
                "target": target_sha256,
                "date": execution_date,
                "index": index,
                "action": item["action"],
                "symbol": item["symbol"],
                "qty": item["qty"],
                "target_weight": item.get("target_weight"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        intent_key = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:16]
        order_id = f"fv47-{intent_key}-{index:02d}-{item['action'][0]}-{item['symbol']}"
        if len(order_id) > 48:
            order_id = order_id[:48]
        item["client_order_id"] = order_id
        journaled.append(item)
    if len(account_fingerprint) != 64 or not secret:
        raise RuntimeError("Execution journal requires an authenticated Alpaca account binding")
    if target_artifact_path is None or not target_artifact_path.is_file():
        raise RuntimeError("Execution journal requires the approved target artifact")
    target_bytes = target_artifact_path.read_bytes()
    if hashlib.sha256(target_bytes).hexdigest() != target_sha256:
        raise RuntimeError("Execution journal target artifact hash does not match")
    try:
        target_payload = json.loads(target_bytes.decode("utf-8"))
        predecessor_name = str(target_payload["predecessor_target"]["artifact_filename"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Execution journal target predecessor is invalid") from exc
    if Path(predecessor_name).name != predecessor_name:
        raise RuntimeError("Execution journal target predecessor path is unsafe")
    predecessor_path = target_artifact_path.parent / predecessor_name
    predecessor_bytes = predecessor_path.read_bytes()
    archive_dir = path.parent / "factor_execution_targets" / target_sha256
    archive_dir.mkdir(parents=True, exist_ok=True)
    for archive_path, content in (
        (archive_dir / "target.json", target_bytes),
        (archive_dir / predecessor_name, predecessor_bytes),
    ):
        if archive_path.exists() and archive_path.read_bytes() != content:
            raise RuntimeError("Immutable execution target archive has conflicting content")
        temporary = archive_path.with_suffix(archive_path.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(archive_path)
    payload = {
        "schema_version": 2,
        "strategy": strategy,
        "status": "prepared",
        "account_fingerprint": account_fingerprint,
        "target_sha256": target_sha256,
        "target_archive": f"factor_execution_targets/{target_sha256}/target.json",
        "target_method": target_method,
        "execution_date": execution_date,
        "owned_quantities_before": {
            str(symbol).upper(): float(quantity)
            for symbol, quantity in sorted((owned_quantities_before or {}).items())
            if float(quantity) > 0.0
        },
        "orders": journaled,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload["journal_hmac_sha256"] = _journal_hmac(payload, secret)
    _atomic_write_json(path, payload)
    return journaled


def _load_execution_journal(
    path: Path,
    *,
    secret: str,
    account_fingerprint: str,
    target_sha256: str = "",
) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Factor execution journal is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 2
        or payload.get("strategy") != "v4_7_top10_score_tilt"
        or payload.get("status") != "prepared"
        or not isinstance(payload.get("orders"), list)
        or not payload["orders"]
    ):
        raise RuntimeError("Factor execution journal contract is invalid")
    if not hmac.compare_digest(
        str(payload.get("journal_hmac_sha256", "")), _journal_hmac(payload, secret)
    ):
        raise RuntimeError("Factor execution journal authentication failed")
    if not hmac.compare_digest(
        str(payload.get("account_fingerprint", "")), str(account_fingerprint)
    ):
        raise RuntimeError("Factor execution journal belongs to a different Alpaca account")
    if target_sha256 and not hmac.compare_digest(
        str(payload.get("target_sha256", "")), str(target_sha256)
    ):
        raise RuntimeError("Factor execution journal targets a different portfolio")
    journal_hash = str(payload.get("target_sha256", ""))
    expected_archive = f"factor_execution_targets/{journal_hash}/target.json"
    if payload.get("target_archive") != expected_archive:
        raise RuntimeError("Factor execution journal target archive is invalid")
    client_ids = [str(order.get("client_order_id", "")) for order in payload["orders"]]
    if any(not value.startswith("fv47-") for value in client_ids) or len(client_ids) != len(set(client_ids)):
        raise RuntimeError("Factor execution journal order identity is invalid")
    return payload


def _journal_target_path(journal_path: Path, journal: Mapping[str, Any]) -> Path:
    target_hash = str(journal.get("target_sha256", ""))
    if len(target_hash) != 64 or any(character not in "0123456789abcdef" for character in target_hash):
        raise RuntimeError("Factor execution journal target hash is invalid")
    path = journal_path.parent / "factor_execution_targets" / target_hash / "target.json"
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != target_hash:
        raise RuntimeError("Factor execution journal target archive is missing or corrupt")
    return path


def _broker_value(source: Any, name: str, default: Any = None) -> Any:
    value = source.get(name, default) if isinstance(source, Mapping) else getattr(source, name, default)
    return value.value if hasattr(value, "value") else value


def _inspect_execution_journal_orders(
    client: Any, orders: Iterable[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Classify deterministic journal legs without ever resubmitting an existing order."""
    terminal_statuses = {"filled", "canceled", "expired", "rejected", "done_for_day"}
    broker_orders: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    inflight: List[Dict[str, Any]] = []
    terminal: List[Dict[str, Any]] = []
    reserved_buy_notional = 0.0
    for intent in orders:
        client_order_id = str(intent.get("client_order_id", ""))
        try:
            broker = client.get_order_by_client_id(client_order_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                missing.append(dict(intent))
                continue
            raise
        status = str(_broker_value(broker, "status", "")).lower()
        row = {
            "client_order_id": client_order_id,
            "symbol": str(_broker_value(broker, "symbol", "")).upper(),
            "action": str(_broker_value(broker, "side", "")).lower(),
            "status": status,
            "qty": float(_broker_value(broker, "qty", 0.0) or 0.0),
            "filled_qty": float(_broker_value(broker, "filled_qty", 0.0) or 0.0),
            "filled_avg_price": float(_broker_value(broker, "filled_avg_price", 0.0) or 0.0),
        }
        if row["symbol"] != str(intent.get("symbol", "")).upper() or row["action"] != str(
            intent.get("action", "")
        ).lower():
            raise RuntimeError(f"Broker order identity mismatch for {client_order_id}")
        broker_orders.append(row)
        if status in terminal_statuses:
            terminal.append(row)
        else:
            inflight.append(row)
            if row["action"] == "buy":
                remaining_qty = max(row["qty"] - row["filled_qty"], 0.0)
                reserved_buy_notional += remaining_qty * float(intent.get("price") or 0.0)
    return {
        "broker_orders": broker_orders,
        "missing": missing,
        "inflight": inflight,
        "completed": terminal,
        "terminal": not missing and not inflight,
        "reserved_buy_notional": reserved_buy_notional,
    }


def _execution_date(value: str) -> str:
    if value:
        return date.fromisoformat(value).isoformat()
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    return datetime.now().date().isoformat()


def _normalized_symbols(symbols: Iterable[str] | str) -> List[str]:
    source = [symbols] if isinstance(symbols, str) else list(symbols)
    return list(dict.fromkeys(str(symbol).upper().strip() for symbol in source if str(symbol).strip()))


def _load_prices(
    symbols: Iterable[str] | str,
    positions: Iterable[Mapping[str, Any]],
    *,
    require_live: bool = False,
) -> Dict[str, float]:
    normalized = _normalized_symbols(symbols)
    prices: Dict[str, float] = {}
    try:
        snapshot = _fetch_alpaca_snapshots(normalized, timeout_seconds=30.0)
        for symbol in normalized:
            if symbol in snapshot and float(snapshot[symbol].get("price") or 0.0) > 0:
                prices[symbol] = float(snapshot[symbol]["price"])
    except Exception:
        pass
    missing_live = [symbol for symbol in normalized if symbol not in prices]
    if missing_live:
        raise RuntimeError(
            f"Alpaca quotes are required for: {', '.join(missing_live)}"
        )
    for position in positions:
        position_symbol = str(position.get("symbol", "")).upper().strip()
        current_price = float(position.get("current_price") or 0.0)
        if position_symbol and current_price > 0:
            prices.setdefault(position_symbol, current_price)
    return prices


def _load_account(*, skip_refresh: bool, execute_trades: bool) -> tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    if skip_refresh:
        account, positions = _latest_local_account()
        return account, positions, {"status": "local_snapshot"}
    try:
        client = get_alpaca_client()
        if client is None:
            raise RuntimeError("Alpaca client unavailable")
        account = get_account_info(client)
        positions = get_positions(client)
        records = persist_account_snapshot(
            account,
            positions,
            source="run_analysis_trade_pipeline",
            action="pre_v4_7_top10_snapshot",
        )
        return account, positions, {"status": "fresh", "records": records}
    except Exception:
        if execute_trades:
            raise
        account, positions = _latest_local_account()
        return account, positions, {"status": "fallback_local"}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _write_factor_execution_state(
    path: Path,
    *,
    owned_quantities: Mapping[str, Any],
    target_artifact_sha256: str,
    target_decision_date: str,
    secret: str,
    account_fingerprint: str,
    strategy: str = "v4_7_top10_score_tilt",
) -> None:
    payload = {
            "schema_version": 2,
            "strategy": strategy,
            "account_fingerprint": account_fingerprint,
            "owned_quantities": {
                str(symbol).upper(): float(quantity)
                for symbol, quantity in sorted(owned_quantities.items())
                if float(quantity) > 0.0
            },
            "target_artifact_sha256": str(target_artifact_sha256),
            "target_decision_date": str(target_decision_date),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    payload["state_hmac_sha256"] = _state_hmac(payload, secret)
    _atomic_write_json(path, payload)


def _exclusive_run_lock(state_path: Path):
    """Compatibility wrapper around the repository-wide OS file lock."""

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    return exclusive_file_lock(lock_path, label="Factor pipeline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a frozen fully invested top-10 factor portfolio")
    parser.add_argument(
        "--strategy",
        required=True,
        choices=["factor-v4.7"],
        help="Run the single frozen V4.7 execution contract",
    )
    parser.add_argument("--execute-trades", action="store_true", help="Submit Alpaca Paper orders; default is dry-run")
    parser.add_argument("--skip-account-refresh", action="store_true")
    parser.add_argument("--execution-date", default="", help="Date for deterministic dry-run only")
    parser.add_argument(
        "--output-file",
        default="data/factor_alpaca_pipeline_latest.json",
        help="JSON audit output path",
    )
    return parser


def _run_pipeline(
    args: argparse.Namespace,
    *,
    config: Mapping[str, Any],
    selection_config: Mapping[str, Any],
    execution_config: Mapping[str, Any],
    state_path: Path,
) -> tuple[Dict[str, Any], Path]:
    validate_run_options(
        execute_trades=bool(args.execute_trades),
        skip_account_refresh=bool(args.skip_account_refresh),
        execution_date=str(args.execution_date or ""),
    )
    if not selection_config["enabled"] or not execution_config["enabled"]:
        raise RuntimeError("factor selection and execution must be enabled")
    configured_strategy = str(selection_config.get("execution_strategy", "factor-v4.7"))
    requested_strategy = str(getattr(args, "strategy", configured_strategy))
    if requested_strategy != configured_strategy:
        raise RuntimeError("Requested strategy does not match the frozen factor config")
    alpaca_paper_value = config.get("alpaca", {}).get("paper", True)
    alpaca_paper = (
        alpaca_paper_value
        if isinstance(alpaca_paper_value, bool)
        else str(alpaca_paper_value).strip().lower() in {"1", "true", "yes", "on"}
    )
    validate_factor_execution(
        execute_trades=bool(args.execute_trades),
        paper_only=bool(execution_config["paper_only"]),
        alpaca_paper=alpaca_paper,
    )

    execution_date = _execution_date(args.execution_date)
    output_path = _resolve_root_path(args.output_file)
    configured_target_path = _resolve_root_path(execution_config["target_path"])

    def load_basket(
        path: Path, approved_sha256: str, maximum_age_days: Optional[int]
    ) -> Dict[str, Any]:
        return _load_factor_basket(
            path,
            execution_date=execution_date,
            expected_research_id=str(selection_config["research_id"]),
            expected_holdings=int(selection_config["holdings"]),
            maximum_age_days=maximum_age_days,
            approved_sha256=approved_sha256,
            expected_method=str(
                selection_config.get("target_method", "v4_7_factor_selection_score_tilt")
            ),
            expected_allocation_method=str(
                selection_config.get("allocation_method", "equal_weight")
            ),
            expected_effective_config=(
                effective_factor_config(selection_config)
                if selection_config.get("allocation_method") == "score_tilt"
                else None
            ),
        )

    state_key_path = _resolve_root_path(execution_config["state_key_path"])
    account, positions, account_refresh = _load_account(
        skip_refresh=bool(args.skip_account_refresh),
        execute_trades=bool(args.execute_trades),
    )
    if float(account.get("equity") or account.get("portfolio_value") or 0.0) <= 0:
        raise RuntimeError("No valid Alpaca account snapshot is available")
    state_secret = _load_or_create_state_key(state_key_path)
    account_number = str(account.get("account_number", "")).strip()
    if not account_number:
        raise RuntimeError("Alpaca account snapshot has no account identity")
    account_fingerprint = _account_fingerprint(account_number, state_secret)
    execution_state = _load_factor_execution_state(
        state_path,
        secret=state_secret,
        account_fingerprint=account_fingerprint,
    )
    journal_path = _resolve_root_path(execution_config["journal_path"])
    journal_status: Dict[str, Any] = {"status": "none"}
    if journal_path.exists():
        journal = _load_execution_journal(
            journal_path,
            secret=state_secret,
            account_fingerprint=account_fingerprint,
        )
        basket = load_basket(
            _journal_target_path(journal_path, journal),
            str(journal["target_sha256"]),
            None,
        )
        basket_symbols = set(basket["target_weights"])
        _validate_factor_target_transition(basket, execution_state)
        order_client = get_alpaca_client()
        if order_client is None:
            raise RuntimeError("Alpaca client unavailable for journal reconciliation")
        allowed_ids = [str(order["client_order_id"]) for order in journal["orders"]]
        _assert_no_conflicting_open_orders(
            get_open_orders(order_client),
            basket_symbols | set(execution_state["owned_quantities"]),
            allowed_client_order_ids=allowed_ids,
        )
        journal_status = _inspect_execution_journal_orders(order_client, journal["orders"])
        journal_status["status"] = "active"
        resumed_results: List[Dict[str, Any]] = []
        if args.execute_trades and journal_status["missing"]:
            resumable_cash = max(
                float(account.get("cash") or 0.0)
                - CASH_RESERVE_USD
                - float(journal_status["reserved_buy_notional"]),
                0.0,
            )
            resumed_results = _execute_trade_plan(
                list(journal_status["missing"]),
                require_paper=True,
                available_cash=resumable_cash,
            )
            journal_status = _inspect_execution_journal_orders(order_client, journal["orders"])
            journal_status["status"] = "active"
            journal_status["resumed_results"] = resumed_results
        if not journal_status["terminal"]:
            output = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "strategy": configured_strategy,
                "mode": "live" if args.execute_trades else "dry_run",
                "market_data_provider": "alpaca",
                "account_refresh": account_refresh,
                "factor_selection_config": selection_config,
                "factor_execution_config": execution_config,
                "factor_basket": basket,
                "journal_status": journal_status,
                "positions": positions,
                "target_weights": dict(basket["target_weights"]),
                "trade_plan": list(journal_status["missing"]),
                "trade_results": resumed_results,
            }
            _atomic_write_json(output_path, output)
            return output, output_path
        account, positions, account_refresh = _load_account(
            skip_refresh=False,
            execute_trades=True,
        )
        refreshed_account_number = str(account.get("account_number", "")).strip()
        if not refreshed_account_number or not hmac.compare_digest(
            _account_fingerprint(refreshed_account_number, state_secret),
            account_fingerprint,
        ):
            raise RuntimeError(
                "Alpaca account changed while settling the execution journal"
            )
        confirmed = _confirmed_owned_quantities(
            positions, basket_symbols | set(execution_state["owned_quantities"])
        )
        _write_factor_execution_state(
            state_path,
            owned_quantities=confirmed,
            target_artifact_sha256=str(basket["artifact_sha256"]),
            target_decision_date=str(basket["decision_date"]),
            secret=state_secret,
            account_fingerprint=account_fingerprint,
        )
        journal_path.unlink()
        execution_state = _load_factor_execution_state(
            state_path,
            secret=state_secret,
            account_fingerprint=account_fingerprint,
        )
        journal_status["status"] = "settled"
    basket = load_basket(
        configured_target_path,
        str(execution_config["approved_target_sha256"]),
        int(execution_config["maximum_target_age_days"]),
    )
    basket_symbols = set(basket["target_weights"])
    _validate_factor_target_transition(basket, execution_state)
    sleeve = _strategy_sleeve(
        account,
        positions,
        execution_state["owned_quantities"],
        target_symbols=basket_symbols,
        capital_allocation_usd=float(execution_config["capital_allocation_usd"]),
    )
    managed_symbols = set(sleeve["managed_symbols"])
    target_weights = dict(basket["target_weights"])
    price_symbols = basket_symbols | {
        str(position.get("symbol", "")).upper().strip()
        for position in sleeve["positions"]
        if str(position.get("symbol", "")).strip()
    }
    prices = _load_prices(
        price_symbols,
        sleeve["positions"],
        require_live=bool(args.execute_trades),
    )
    sleeve_account = dict(account)
    sleeve_account["equity"] = sleeve["notional"]
    sleeve_account["portfolio_value"] = sleeve["notional"]
    trade_plan = _build_factor_rebalance_plan(
        account=sleeve_account,
        positions=sleeve["positions"],
        prices=prices,
        target_weights=target_weights,
        reason="v4_7_top10_score_tilt_rebalance",
    )
    if args.execute_trades:
        order_client = get_alpaca_client()
        if order_client is None:
            raise RuntimeError("Alpaca client unavailable for open-order reconciliation")
        _assert_no_conflicting_open_orders(
            get_open_orders(order_client),
            managed_symbols,
        )
    if args.execute_trades and trade_plan:
        trade_plan = _prepare_execution_journal(
            journal_path,
            trade_plan,
            target_sha256=str(basket["artifact_sha256"]),
            execution_date=execution_date,
            strategy="v4_7_top10_score_tilt",
            target_artifact_path=_resolve_root_path(execution_config["target_path"]),
            target_method="v4_7_factor_selection_score_tilt",
            account_fingerprint=account_fingerprint,
            secret=state_secret,
            owned_quantities_before=sleeve["owned_quantities"],
        )
    trade_results = (
        _execute_trade_plan(
            trade_plan,
            require_paper=True,
            available_cash=max(float(account.get("cash") or 0.0) - CASH_RESERVE_USD, 0.0),
        )
        if args.execute_trades
        else []
    )
    if args.execute_trades and trade_plan:
        order_client = get_alpaca_client()
        if order_client is None:
            raise RuntimeError("Alpaca client unavailable after order submission")
        journal_status = _inspect_execution_journal_orders(order_client, trade_plan)
        journal_status["status"] = "active"
    if args.execute_trades and trade_plan and journal_status.get("terminal"):
        _, confirmed_positions, _ = _load_account(skip_refresh=False, execute_trades=True)
        owned_after = _confirmed_owned_quantities(
            confirmed_positions,
            basket_symbols | set(sleeve["owned_quantities"]),
        )
        _write_factor_execution_state(
            state_path,
            owned_quantities=owned_after,
            target_artifact_sha256=str(basket["artifact_sha256"]),
            target_decision_date=str(basket["decision_date"]),
            secret=state_secret,
            account_fingerprint=account_fingerprint,
        )
        journal_path.unlink(missing_ok=True)
        journal_status["status"] = "settled"

    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy": configured_strategy,
        "mode": "live" if args.execute_trades else "dry_run",
        "market_data_provider": "alpaca",
        "account_refresh": account_refresh,
        "factor_selection_config": selection_config,
        "factor_execution_config": execution_config,
        "factor_basket": basket,
        "strategy_sleeve": sleeve,
        "target_weights": target_weights,
        "prices": prices,
        "trade_plan": trade_plan,
        "trade_results": trade_results,
        "journal_status": journal_status,
    }
    _atomic_write_json(output_path, output)
    return output, output_path


def main() -> None:
    args = build_parser().parse_args()
    config = load_config()
    selection_config = get_factor_portfolio_config(config)
    execution_config = get_factor_execution_config(config)
    state_path = _resolve_root_path(execution_config["state_path"])
    with _exclusive_run_lock(state_path):
        output, output_path = _run_pipeline(
            args,
            config=config,
            selection_config=selection_config,
            execution_config=execution_config,
            state_path=state_path,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Result written to: {output_path}")


if __name__ == "__main__":
    main()
