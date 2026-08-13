#!/usr/bin/env python3
"""Reconcile an interrupted frozen factor execution archive against Alpaca."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from query_alpaca_account import get_alpaca_client, get_open_orders
from run_analysis_trade_pipeline import _state_hmac, _windows_dpapi


TERMINAL_BROKER_STATUSES = {
    "filled",
    "canceled",
    "expired",
    "rejected",
    "done_for_day",
}


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _enum_value(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value).lower()


def _find_one(names: Iterable[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"Archive must contain exactly one {suffix}; found {len(matches)}")
    return matches[0]


def _find_json_by_sha256(
    source: zipfile.ZipFile,
    names: Iterable[str],
    expected_sha256: str,
    *,
    preferred_basename: str = "",
) -> str:
    """Locate an immutable artifact by content, independent of late audit output."""
    candidates: list[str] = []
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        if preferred_basename and PurePosixPath(name).name != preferred_basename:
            continue
        if hashlib.sha256(source.read(name)).hexdigest() == expected_sha256:
            candidates.append(name)
    if not candidates and preferred_basename:
        return _find_json_by_sha256(source, names, expected_sha256)
    if len(candidates) != 1:
        raise RuntimeError(
            f"Archive must contain exactly one JSON artifact with SHA-256 {expected_sha256}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _selected_identities(portfolio: Mapping[str, Any]) -> list[tuple[int, str, str]]:
    selected = portfolio.get("selected")
    if not isinstance(selected, list) or len(selected) != 10:
        raise RuntimeError("Archived portfolio membership is invalid")
    ordered = sorted(selected, key=lambda row: int(row.get("selection_rank", 0)))
    identities = [
        (
            int(row.get("selection_rank", 0)),
            str(row.get("security_id", "")).strip(),
            str(row.get("ticker", "")).upper().strip(),
        )
        for row in ordered
    ]
    if [rank for rank, _, _ in identities] != list(range(1, 11)):
        raise RuntimeError("Archived portfolio ranks are invalid")
    return identities


def _decode_state_key(stored: bytes) -> str:
    raw = stored.decode("ascii").strip()
    if raw.startswith("dpapi-v1:"):
        encrypted = base64.b64decode(raw.split(":", 1)[1], validate=True)
        key = _windows_dpapi(encrypted, decrypt=True).decode("ascii").lower()
    else:
        key = raw.lower()
    if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
        raise RuntimeError("Archive factor execution state key is invalid")
    return key


def _validate_archive_entries(infos: Iterable[zipfile.ZipInfo]) -> None:
    entries = list(infos)
    names = [info.filename for info in entries]
    if len(names) != len(set(names)):
        raise RuntimeError("Archive contains duplicate entry names")
    if sum(info.file_size for info in entries) > 2 * 1024 * 1024 * 1024:
        raise RuntimeError("Archive uncompressed size exceeds the reconciliation limit")
    for info in entries:
        path = PurePosixPath(info.filename)
        mode = (info.external_attr >> 16) & 0xF000
        if (
            not info.filename
            or "\\" in info.filename
            or path.is_absolute()
            or ".." in path.parts
            or any(":" in part for part in path.parts)
            or mode == stat.S_IFLNK
        ):
            raise RuntimeError(f"Archive contains an unsafe entry: {info.filename!r}")


def _archived_account_number(raw_jsonl: bytes) -> str:
    try:
        lines = raw_jsonl.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeError("Archived balance log is not valid UTF-8") from exc
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        account = row.get("account") if isinstance(row, dict) else None
        number = str(account.get("account_number", "")).strip() if isinstance(account, dict) else ""
        if number:
            return number
    raise RuntimeError("Archived balance log contains no Alpaca account number")


def _broker_order(client: Any, client_order_id: str) -> Any | None:
    try:
        return client.get_order_by_client_id(client_order_id)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404:
            return None
        raise


def _expected_client_order_id(
    intent: Mapping[str, Any], *, target_sha256: str, execution_date: str, index: int,
    strategy: str = "v4_6_r1_top10",
) -> str:
    canonical = json.dumps(
        {
            "target": target_sha256,
            "date": execution_date,
            "index": index,
            "action": intent["action"],
            "symbol": intent["symbol"],
            "qty": intent["qty"],
            "target_weight": intent.get("target_weight"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    intent_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    prefix = "fv47" if strategy == "v4_7_top10_score_tilt" else "fv46"
    order_id = f"{prefix}-{intent_key}-{index:02d}-{str(intent['action'])[0]}-{intent['symbol']}"
    return order_id[:48]


def reconcile_archive(input_zip: Path, output_zip: Path, *, client: Any) -> Dict[str, Any]:
    input_zip = Path(input_zip).resolve()
    output_zip = Path(output_zip).resolve()
    if input_zip == output_zip:
        raise RuntimeError("Reconciliation output must not overwrite the source archive")
    if output_zip.exists():
        raise RuntimeError(f"Reconciliation output already exists: {output_zip}")

    with zipfile.ZipFile(input_zip, "r") as source:
        _validate_archive_entries(source.infolist())
        names = source.namelist()
        journal_name = _find_one(names, "/factor_execution_journal.json")
        key_name = _find_one(names, "/factor_execution_state.key")
        balance_name = _find_one(names, "/balance/balance.jsonl")
        state_names = [name for name in names if name.endswith("/factor_execution_state.json")]
        if len(state_names) > 1:
            raise RuntimeError("Archive contains multiple factor_execution_state.json files")

        journal = json.loads(source.read(journal_name))
        expected_target_hash = str(journal.get("target_sha256", "")).lower()
        target_basename = str(journal.get("target_artifact_filename", ""))
        if (
            len(expected_target_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_target_hash)
            or (target_basename and PurePosixPath(target_basename).name != target_basename)
        ):
            raise RuntimeError("Journal target artifact identity is invalid")
        portfolio_name = _find_json_by_sha256(
            source, names, expected_target_hash, preferred_basename=target_basename
        )
        portfolio_bytes = source.read(portfolio_name)
        portfolio = json.loads(portfolio_bytes)
        state_secret = _decode_state_key(source.read(key_name))

        archived_account = _archived_account_number(source.read(balance_name))
        broker_account = str(_value(client.get_account(), "account_number", "")).strip()
        if not broker_account or archived_account != broker_account:
            raise RuntimeError("Archived data belongs to a different Alpaca account")

        actual_target_hash = hashlib.sha256(portfolio_bytes).hexdigest()
        if actual_target_hash != expected_target_hash:
            raise RuntimeError("Journal target hash does not match the archived portfolio")
        journal_strategy = str(journal.get("strategy", ""))
        strategy_methods = {
            "v4_6_r1_top10": "v4_6_r1_factor_selection",
            "v4_7_top10_score_tilt": "v4_7_factor_selection_score_tilt",
        }
        if journal_strategy not in strategy_methods or journal.get("status") != "prepared":
            raise RuntimeError("Journal is not an active supported prepared factor journal")
        if portfolio.get("method") != strategy_methods[journal_strategy]:
            raise RuntimeError("Archived target does not match the journal strategy")
        journal_method = journal.get("target_method")
        if journal_method is None and journal.get("schema_version") == 1:
            journal_method = strategy_methods[journal_strategy]
        if journal_method != strategy_methods[journal_strategy]:
            raise RuntimeError("Journal target method does not match the journal strategy")
        if str(portfolio.get("decision_date")) != str(journal.get("execution_date")):
            raise RuntimeError("Portfolio and journal dates are not aligned")
        orders = journal.get("orders")
        if not isinstance(orders, list) or not orders:
            raise RuntimeError("Journal contains no order intents")

        initial_owned: Dict[str, float] = {}
        initial_state: Mapping[str, Any] | None = None
        if state_names:
            initial_state = json.loads(source.read(state_names[0]))
            if (
                not isinstance(initial_state, Mapping)
                or initial_state.get("schema_version") != 1
                or initial_state.get("strategy") not in strategy_methods
                or not isinstance(initial_state.get("owned_quantities"), Mapping)
                or str(initial_state.get("state_hmac_sha256", ""))
                != _state_hmac(initial_state, state_secret)
            ):
                raise RuntimeError("Archived factor execution state is invalid or unauthenticated")
            try:
                initial_owned = {
                    str(symbol).upper().strip(): float(quantity)
                    for symbol, quantity in initial_state["owned_quantities"].items()
                }
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Archived ownership quantities are invalid") from exc
            if any(not symbol or quantity <= 0.0 or not math.isfinite(quantity) for symbol, quantity in initial_owned.items()):
                raise RuntimeError("Archived ownership quantities are invalid")
            prior_date = str(initial_state.get("target_decision_date", ""))
            target_date = str(portfolio.get("decision_date", ""))
            if prior_date > target_date:
                raise RuntimeError("Archived target predates the signed execution state")
            if prior_date == target_date and str(initial_state.get("target_artifact_sha256")) != actual_target_hash:
                predecessor = portfolio.get("predecessor_target")
                valid_upgrade = (
                    initial_state.get("strategy") == "v4_6_r1_top10"
                    and journal_strategy == "v4_7_top10_score_tilt"
                    and isinstance(predecessor, Mapping)
                    and predecessor.get("research_id") == "v4_6_r1_0001"
                    and predecessor.get("sha256") == initial_state.get("target_artifact_sha256")
                )
                if valid_upgrade:
                    predecessor_name = _find_json_by_sha256(
                        source,
                        names,
                        str(predecessor["sha256"]),
                        preferred_basename=str(predecessor.get("artifact_filename", "")),
                    )
                    predecessor_payload = json.loads(source.read(predecessor_name))
                    valid_upgrade = (
                        predecessor_payload.get("method") == "v4_6_r1_factor_selection"
                        and predecessor_payload.get("research_id") == "v4_6_r1_0001"
                        and _selected_identities(portfolio)
                        == _selected_identities(predecessor_payload)
                    )
                if not valid_upgrade:
                    raise RuntimeError("Archived same-month target is not an authenticated strategy upgrade")

        selected = portfolio.get("selected")
        if not isinstance(selected, list):
            raise RuntimeError("Archived portfolio has no selected securities")
        selected_symbols = {
            str(row.get("ticker", "")).upper().strip()
            for row in selected
            if isinstance(row, Mapping)
        }
        client_ids = [str(order.get("client_order_id", "")).strip() for order in orders]
        order_symbols = [str(order.get("symbol", "")).upper().strip() for order in orders]
        if len(client_ids) != len(set(client_ids)) or len(order_symbols) != len(set(order_symbols)):
            raise RuntimeError("Journal contains duplicate client_order_id or symbol")
        if any(symbol not in selected_symbols | set(initial_owned) for symbol in order_symbols):
            raise RuntimeError("Journal contains a symbol outside the approved portfolio")
        for index, intent in enumerate(orders, 1):
            if client_ids[index - 1] != _expected_client_order_id(
                intent,
                target_sha256=actual_target_hash,
                execution_date=str(journal["execution_date"]),
                index=index,
                strategy=journal_strategy,
            ):
                raise RuntimeError("Journal client_order_id does not authenticate its order intent")

        journal_symbols = {str(order.get("symbol", "")).upper() for order in orders}
        if hasattr(client, "get_orders"):
            conflicts = [
                order for order in get_open_orders(client)
                if str(order.get("symbol", "")).upper() in journal_symbols
            ]
            if conflicts:
                raise RuntimeError("A journal symbol still has an open Alpaca order")

        owned: Dict[str, float] = dict(initial_owned)
        broker_audit = []
        for intent in orders:
            symbol = str(intent.get("symbol", "")).upper().strip()
            action = str(intent.get("action", "")).lower().strip()
            client_order_id = str(intent.get("client_order_id", "")).strip()
            intended_qty = float(intent.get("qty") or 0.0)
            if not symbol or action not in {"buy", "sell"} or not client_order_id or intended_qty <= 0.0:
                raise RuntimeError("Journal contains an invalid order intent")
            order = _broker_order(client, client_order_id)
            if order is None:
                broker_audit.append(
                    {"client_order_id": client_order_id, "symbol": symbol, "status": "not_submitted"}
                )
                continue
            broker_symbol = str(_value(order, "symbol", "")).upper().strip()
            broker_side = _enum_value(_value(order, "side", ""))
            status = _enum_value(_value(order, "status", ""))
            filled_qty = float(_value(order, "filled_qty", 0.0) or 0.0)
            broker_qty = float(_value(order, "qty", 0.0) or 0.0)
            if broker_symbol != symbol or broker_side != action:
                raise RuntimeError(f"Broker order identity does not match journal for {client_order_id}")
            if status not in TERMINAL_BROKER_STATUSES:
                raise RuntimeError(f"Broker order is not terminal: {client_order_id} status={status}")
            if (
                filled_qty < 0.0
                or filled_qty > intended_qty + 1e-6
                or broker_qty > intended_qty + 1e-6
                or not math.isfinite(filled_qty)
            ):
                raise RuntimeError(f"Broker fill quantity is invalid for {client_order_id}")
            if filled_qty > 0.0:
                if action == "buy":
                    owned[symbol] = owned.get(symbol, 0.0) + filled_qty
                else:
                    remaining = owned.get(symbol, 0.0) - filled_qty
                    if remaining < -1e-6:
                        raise RuntimeError(f"Sell fill exceeds reconciled ownership for {symbol}")
                    if remaining > 1e-9:
                        owned[symbol] = remaining
                    else:
                        owned.pop(symbol, None)
            broker_audit.append(
                {
                    "order_id": str(_value(order, "id", "")),
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": action,
                    "status": status,
                    "ordered_qty": broker_qty,
                    "filled_qty": filled_qty,
                    "filled_avg_price": float(_value(order, "filled_avg_price", 0.0) or 0.0),
                    "limit_price": float(_value(order, "limit_price", 0.0) or 0.0),
                }
            )

        broker_positions = {
            str(position.symbol).upper(): abs(float(position.qty))
            for position in client.get_all_positions()
        }
        for symbol in journal_symbols | set(initial_owned):
            expected = owned.get(symbol, 0.0)
            actual = broker_positions.get(symbol, 0.0)
            if abs(expected - actual) > 1e-6:
                raise RuntimeError(
                    f"Broker position does not exactly match reconciled ownership for {symbol}: "
                    f"expected={expected} actual={actual}"
                )

        reconciled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        normalized_owned = {
            symbol: round(quantity, 6)
            for symbol, quantity in sorted(owned.items())
            if quantity > 1e-9
        }
        state = {
            "schema_version": 1,
            "strategy": journal_strategy,
            "owned_quantities": normalized_owned,
            "target_artifact_sha256": actual_target_hash,
            "target_decision_date": str(portfolio["decision_date"]),
            "updated_at": reconciled_at,
        }
        state["state_hmac_sha256"] = _state_hmac(state, state_secret)

        archived_journal = {
            **journal,
            "status": "reconciled",
            "reconciled_at": reconciled_at,
            "broker_orders": broker_audit,
            "owned_quantities_after": normalized_owned,
        }
        prefix = journal_name[: -len("factor_execution_journal.json")]
        pipeline_matches = [
            name for name in names if name.endswith("/factor_alpaca_pipeline_latest.json")
        ]
        if len(pipeline_matches) > 1:
            raise RuntimeError("Archive contains multiple factor pipeline audit files")
        pipeline_name = (
            pipeline_matches[0]
            if pipeline_matches
            else prefix + "factor_alpaca_pipeline_latest.json"
        )
        pipeline = json.loads(source.read(pipeline_name)) if pipeline_matches else {}
        pipeline["broker_reconciliation"] = {
            "status": "reconciled",
            "reconciled_at": reconciled_at,
            "source_observation_preserved": True,
            "broker_orders": broker_audit,
            "owned_quantities_after": normalized_owned,
        }

        state_name = prefix + "factor_execution_state.json"
        archived_name = (
            prefix
            + "factor_execution_journal.reconciled."
            + str(journal["execution_date"]).replace("-", "")
            + ".json"
        )
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_zip.name}.", suffix=".tmp", dir=str(output_zip.parent)
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as destination:
                for info in source.infolist():
                    if info.filename in {journal_name, pipeline_name, *state_names}:
                        continue
                    destination.writestr(info, source.read(info.filename))
                destination.writestr(
                    pipeline_name, json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n"
                )
                destination.writestr(
                    state_name, json.dumps(state, ensure_ascii=False, indent=2) + "\n"
                )
                destination.writestr(
                    archived_name,
                    json.dumps(archived_journal, ensure_ascii=False, indent=2) + "\n",
                )
            os.replace(temporary_path, output_zip)
        finally:
            temporary_path.unlink(missing_ok=True)

    return {
        "output_zip": str(output_zip),
        "target_sha256": actual_target_hash,
        "target_decision_date": str(portfolio["decision_date"]),
        "owned_quantities": normalized_owned,
        "broker_orders": broker_audit,
        "active_journal_removed": True,
        "archived_journal": archived_name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-zip", required=True)
    parser.add_argument("--output-zip", required=True)
    args = parser.parse_args()
    client = get_alpaca_client()
    if client is None:
        raise RuntimeError("Alpaca client is unavailable")
    summary = reconcile_archive(Path(args.input_zip), Path(args.output_zip), client=client)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
