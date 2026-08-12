#!/usr/bin/env python3
"""Build the standalone, fully invested V4.6-R1 ten-stock factor basket.

This is a portable, standard-library adaptation of the frozen factor-model
ranking and selection rules. It does not fetch fundamentals or place orders.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import itertools
import json
import math
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from _config import get_factor_portfolio_config, load_config


FACTOR_KEYS = ("size", "value", "profitability", "investment", "momentum")
FUNDAMENTAL_KEYS = FACTOR_KEYS[:-1]
DEFAULT_WEIGHTS = {
    "size": 0.10,
    "value": 0.30,
    "profitability": 0.10,
    "investment": 0.30,
    "momentum": 0.20,
}
REQUIRED_IDENTITY_COLUMNS = (
    "security_id",
    "ticker",
    "ff_industry_12",
    "membership_date",
    "decision_date",
    "constituent_as_of_date",
    "fundamental_available_date",
    "price_as_of_date",
    "industry_as_of_date",
)
SOURCE_SNAPSHOT_KEYS = ("constituents", "fundamentals", "prices", "industries")


class FactorPortfolioError(ValueError):
    """Raised when a factor input cannot safely produce a portfolio."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FactorPortfolioError(f"{label} is invalid") from exc


def validate_signal_manifest(
    manifest_path: Path,
    signal_path: Path,
    *,
    research_id: str,
    membership_date: str,
    decision_date: str,
) -> dict[str, Any]:
    """Verify the immutable point-in-time signal snapshot and its provenance."""
    if not manifest_path.is_file():
        raise FactorPortfolioError(f"signal manifest does not exist: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactorPortfolioError("signal manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise FactorPortfolioError("signal manifest version must be 1")
    expected = {
        "research_id": research_id,
        "membership_date": membership_date,
        "decision_date": decision_date,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise FactorPortfolioError(f"signal manifest {key} does not match the cross-section")
    actual_signal_hash = _file_sha256(signal_path)
    if manifest.get("signal_sha256") != actual_signal_hash:
        raise FactorPortfolioError("signal CSV hash does not match its manifest")
    decision = _iso_date(decision_date, "decision_date")
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(SOURCE_SNAPSHOT_KEYS):
        raise FactorPortfolioError("signal manifest must identify all four source snapshots")
    normalized: dict[str, dict[str, str]] = {}
    for name in SOURCE_SNAPSHOT_KEYS:
        source = snapshots[name]
        if not isinstance(source, dict):
            raise FactorPortfolioError(f"source snapshot {name} is invalid")
        available = _iso_date(source.get("available_through"), f"{name}.available_through")
        source_hash = str(source.get("sha256", "")).lower()
        source_path_value = str(source.get("path", "")).strip()
        source_path = Path(source_path_value)
        if not source_path.is_absolute():
            source_path = manifest_path.parent / source_path
        if available > decision:
            raise FactorPortfolioError(f"source snapshot {name} contains future data")
        if len(source_hash) != 64 or any(character not in "0123456789abcdef" for character in source_hash):
            raise FactorPortfolioError(f"source snapshot {name} has an invalid SHA-256")
        if not source_path_value or not source_path.is_file():
            raise FactorPortfolioError(f"source snapshot {name} artifact does not exist")
        if _file_sha256(source_path) != source_hash:
            raise FactorPortfolioError(f"source snapshot {name} artifact hash does not match")
        normalized[name] = {
            "path": str(source_path),
            "availableThrough": available.isoformat(),
            "sha256": source_hash,
        }
    return {
        "path": str(manifest_path),
        "manifestSha256": _file_sha256(manifest_path),
        "signalSha256": actual_signal_hash,
        "sourceSnapshots": normalized,
    }


def risk_factor_audit(database: Path, *, decision_date: str, lag_months: int) -> dict[str, Any]:
    """Prove which append-only FF6 vintage and observations were usable."""
    if not database.is_file():
        raise FactorPortfolioError(f"factor database does not exist: {database}")
    cutoff = conservative_factor_cutoff(_iso_date(decision_date, "decision_date"), lag_months)
    connection = sqlite3.connect(str(database))
    try:
        vintage = connection.execute(
            "SELECT vintage_id, fetched_at_utc FROM fama_french_vintages "
            "WHERE date(fetched_at_utc) <= ? ORDER BY fetched_at_utc DESC LIMIT 1",
            (decision_date,),
        ).fetchone()
        if vintage is None:
            raise FactorPortfolioError("no Fama-French vintage was available by decision_date")
        row = connection.execute(
            "SELECT MAX(trade_date), COUNT(*) FROM fama_french_daily "
            "WHERE vintage_id=? AND trade_date<=?",
            (vintage[0], cutoff.isoformat()),
        ).fetchone()
    except sqlite3.Error as exc:
        raise FactorPortfolioError("factor database is not the append-only V4.6 schema") from exc
    finally:
        connection.close()
    if not row or not row[0] or row[1] < 1:
        raise FactorPortfolioError("factor database has no causally usable observations")
    return {
        "database": str(database),
        "vintageId": vintage[0],
        "vintageFetchedAtUtc": vintage[1],
        "contractualCutoff": cutoff.isoformat(),
        "actualCutoff": row[0],
        "usableRows": row[1],
    }


def candidate_weight_grid(
    values: Sequence[float], *, fundamental_sum: float, momentum: float
) -> list[dict[str, float]]:
    """Return the 19 preregistered V4.6 fundamental-weight combinations."""
    rows: list[dict[str, float]] = []
    for size, value, profitability, investment in itertools.product(values, repeat=4):
        if not math.isclose(
            size + value + profitability + investment,
            fundamental_sum,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            continue
        rows.append(
            {
                "size": float(size),
                "value": float(value),
                "profitability": float(profitability),
                "investment": float(investment),
                "momentum": float(momentum),
            }
        )
    return sorted(rows, key=lambda row: tuple(row[key] for key in FACTOR_KEYS))


def conservative_factor_cutoff(decision_date: date, lag_months: int = 2) -> date:
    """Return the month-end through which FF6 data is assumed public."""
    if lag_months < 2:
        raise FactorPortfolioError("factor_lag_months must be at least 2")
    month_index = decision_date.year * 12 + decision_date.month - 1 - lag_months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "available"}


def derive_raw_factors(row: Mapping[str, Any]) -> dict[str, float | None]:
    """Use supplied raw signals, or derive them from point-in-time components."""
    existing = {key: _number(row.get(f"{key}_raw")) for key in FACTOR_KEYS}
    market_cap = _number(row.get("market_cap"))
    net_income = _number(row.get("net_income_ttm"))
    operating_income = _number(row.get("operating_income_ttm"))
    assets_current = _number(row.get("assets_current"))
    assets_lag = _number(row.get("assets_lag_4q"))
    momentum = _number(row.get("momentum_12_1"))
    valid_market_cap = market_cap is not None and market_cap > 0
    valid_assets = (
        assets_current is not None
        and assets_current > 0
        and assets_lag is not None
        and assets_lag > 0
    )
    average_assets = (assets_current + assets_lag) / 2.0 if valid_assets else None
    derived = {
        "size": -math.log(market_cap) if valid_market_cap else None,
        "value": net_income / market_cap if valid_market_cap and net_income is not None else None,
        "profitability": (
            operating_income / average_assets
            if average_assets is not None and operating_income is not None
            else None
        ),
        "investment": (-(assets_current / assets_lag - 1.0) if valid_assets else None),
        "momentum": momentum,
    }
    return {
        key: existing[key] if existing[key] is not None else derived[key]
        for key in FACTOR_KEYS
    }


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise FactorPortfolioError("cannot calculate a quantile from no values")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _average_rank_percentiles(items: Sequence[tuple[str, float]]) -> dict[str, float]:
    """Match pandas rank(method='average', pct=True) for ascending fundamentals."""
    ordered = sorted(items, key=lambda item: (item[1], item[0]))
    result: dict[str, float] = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        percentile = average_rank / len(ordered)
        for security_id, _ in ordered[start:end]:
            result[security_id] = percentile
        start = end
    return result


def _momentum_percentiles(items: Sequence[tuple[str, float]]) -> dict[str, float]:
    """Rank momentum descending with security_id as the deterministic tie-breaker."""
    ordered = sorted(items, key=lambda item: (-item[1], item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    return {
        security_id: 1.0 - index / (len(ordered) - 1.0)
        for index, (security_id, _) in enumerate(ordered)
    }


def _validated_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if set(weights) != set(FACTOR_KEYS):
        raise FactorPortfolioError("weights must contain exactly five selection signals")
    parsed = {key: float(weights[key]) for key in FACTOR_KEYS}
    if any(value < 0.0 for value in parsed.values()) or not math.isclose(
        sum(parsed.values()), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise FactorPortfolioError("weights must be non-negative and sum to 1")
    return parsed


def score_cross_section(
    rows: Iterable[Mapping[str, Any]],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    *,
    industry_min_count: int = 10,
    winsor_limits: tuple[float, float] = (0.01, 0.99),
) -> list[dict[str, Any]]:
    """Calculate point-in-time percentiles and the frozen weighted score."""
    parsed_weights = _validated_weights(weights)
    if industry_min_count < 1:
        raise FactorPortfolioError("industry_min_count must be positive")
    if not 0.0 <= winsor_limits[0] < winsor_limits[1] <= 1.0:
        raise FactorPortfolioError("winsor limits are invalid")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_tickers: set[str] = set()
    for source in rows:
        row = dict(source)
        missing = [column for column in REQUIRED_IDENTITY_COLUMNS if not str(row.get(column, "")).strip()]
        if missing:
            raise FactorPortfolioError(f"missing identity columns: {', '.join(missing)}")
        security_id = str(row["security_id"])
        if security_id in seen:
            raise FactorPortfolioError(f"duplicate security_id: {security_id}")
        seen.add(security_id)
        ticker = str(row["ticker"]).upper().strip()
        if ticker in seen_tickers:
            raise FactorPortfolioError(f"duplicate ticker in one cross-section: {ticker}")
        seen_tickers.add(ticker)
        decision = _iso_date(row["decision_date"], "decision_date")
        for column in REQUIRED_IDENTITY_COLUMNS[3:]:
            parsed_date = _iso_date(row[column], column)
            if parsed_date > decision:
                raise FactorPortfolioError(f"{column} contains data unavailable on decision_date")
            row[column] = parsed_date.isoformat()
        raw = derive_raw_factors(row)
        row.update({f"{key}_raw": raw[key] for key in FACTOR_KEYS})
        row["security_id"] = security_id
        row["ticker"] = ticker
        row["ff_industry_12"] = str(row["ff_industry_12"])
        row["risk_eligible"] = _truthy(row.get("risk_eligible"), False)
        row["adv20_usd"] = _number(row.get("adv20_usd")) or 0.0
        result.append(row)

    if not result:
        raise FactorPortfolioError("signal input is empty")
    if len({row["membership_date"] for row in result}) != 1 or len(
        {row["decision_date"] for row in result}
    ) != 1:
        raise FactorPortfolioError("signal input must contain exactly one monthly decision cross-section")

    for factor in FUNDAMENTAL_KEYS:
        valid = [row for row in result if row[f"{factor}_raw"] is not None]
        values = [float(row[f"{factor}_raw"]) for row in valid]
        if not values:
            continue
        lower = _quantile(values, winsor_limits[0])
        upper = _quantile(values, winsor_limits[1])
        winsorized = {
            row["security_id"]: min(max(float(row[f"{factor}_raw"]), lower), upper)
            for row in valid
        }
        global_percentiles = _average_rank_percentiles(list(winsorized.items()))
        by_industry: dict[str, list[tuple[str, float]]] = {}
        for row in valid:
            by_industry.setdefault(row["ff_industry_12"], []).append(
                (row["security_id"], winsorized[row["security_id"]])
            )
        industry_percentiles = {
            industry: _average_rank_percentiles(items)
            for industry, items in by_industry.items()
            if len(items) >= industry_min_count
        }
        for row in result:
            security_id = row["security_id"]
            if security_id not in winsorized:
                row[f"{factor}_percentile"] = None
                row[f"{factor}_rank_scope"] = None
                continue
            industry = row["ff_industry_12"]
            if industry in industry_percentiles:
                row[f"{factor}_percentile"] = industry_percentiles[industry][security_id]
                row[f"{factor}_rank_scope"] = "ff12_industry"
            else:
                row[f"{factor}_percentile"] = global_percentiles[security_id]
                row[f"{factor}_rank_scope"] = "all_market_fallback"
            row[f"{factor}_winsorized"] = winsorized[security_id]

    momentum_by_industry: dict[str, list[tuple[str, float]]] = {}
    for row in result:
        raw = row["momentum_raw"]
        if raw is not None:
            momentum_by_industry.setdefault(row["ff_industry_12"], []).append(
                (row["security_id"], float(raw))
            )
    momentum_ranks = {
        industry: _momentum_percentiles(items)
        for industry, items in momentum_by_industry.items()
    }
    for row in result:
        ranks = momentum_ranks.get(row["ff_industry_12"], {})
        row["momentum_percentile"] = ranks.get(row["security_id"])
        row["momentum_rank_scope"] = "ff12_industry" if row["security_id"] in ranks else None
        percentiles = [row.get(f"{key}_percentile") for key in FACTOR_KEYS]
        row["score"] = (
            sum(float(row[f"{key}_percentile"]) * parsed_weights[key] for key in FACTOR_KEYS)
            if all(value is not None for value in percentiles)
            else None
        )
    return sorted(result, key=lambda row: row["security_id"])


def select_factor_portfolio(
    rows: Iterable[Mapping[str, Any]],
    *,
    holdings: int = 10,
    max_names_per_industry: int = 3,
    minimum_adv20_usd: float = 0.0,
) -> list[dict[str, Any]]:
    """Select the diversified top names and assign equal target weights."""
    if holdings < 1 or max_names_per_industry < 1:
        raise FactorPortfolioError("portfolio limits must be positive")
    eligible = [
        dict(row)
        for row in rows
        if _truthy(row.get("risk_eligible"), False)
        and _number(row.get("score")) is not None
        and (_number(row.get("adv20_usd")) or 0.0) >= minimum_adv20_usd
    ]
    eligible.sort(key=lambda row: (-float(row["score"]), str(row["security_id"])))
    selected: list[dict[str, Any]] = []
    industry_counts: dict[str, int] = {}
    for row in eligible:
        industry = str(row["ff_industry_12"])
        if industry_counts.get(industry, 0) >= max_names_per_industry:
            continue
        selected.append(row)
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if len(selected) == holdings:
            break
    if len(selected) != holdings:
        raise FactorPortfolioError(f"only {len(selected)} diversified eligible names; need {holdings}")
    target_weight = 1.0 / holdings
    for rank, row in enumerate(selected, 1):
        row["selection_rank"] = rank
        row["target_weight"] = target_weight
    return selected


def load_signal_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FactorPortfolioError(f"signal input does not exist: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _parse_weights(value: str) -> dict[str, float]:
    if not value:
        return dict(DEFAULT_WEIGHTS)
    pairs = [item.split("=", 1) for item in value.split(",") if "=" in item]
    return _validated_weights({key.strip(): float(raw) for key, raw in pairs})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a V4.6-R1 ten-stock factor basket")
    parser.add_argument("--input", default="", help="Point-in-time signal CSV")
    parser.add_argument("--output", default="", help="Output JSON path")
    parser.add_argument("--manifest", default="", help="Signal provenance manifest JSON")
    parser.add_argument("--weights", default="", help="size=.1,value=.3,...")
    parser.add_argument("--list-candidates", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.list_candidates:
        print(json.dumps(candidate_weight_grid([0.1, 0.2, 0.3], fundamental_sum=0.8, momentum=0.2), indent=2))
        return
    config = get_factor_portfolio_config(load_config())
    if not config["enabled"] or config["mode"] != "v4_6_r1_top10":
        raise FactorPortfolioError("factor portfolio must be enabled in v4_6_r1_top10 mode")
    input_path = Path(args.input or config["signal_input"])
    output_path = Path(args.output or config["output_path"])
    weights = _parse_weights(args.weights) if args.weights else config["weights"]
    if args.weights and config["parameter_mode"] == "frozen":
        raise FactorPortfolioError("--weights is forbidden in frozen mode")
    scored = score_cross_section(
        load_signal_csv(input_path),
        weights,
        industry_min_count=config["minimum_industry_count"],
        winsor_limits=(config["winsor_lower"], config["winsor_upper"]),
    )
    manifest = validate_signal_manifest(
        Path(args.manifest or config["signal_manifest"]),
        input_path,
        research_id=config["research_id"],
        membership_date=scored[0]["membership_date"],
        decision_date=scored[0]["decision_date"],
    )
    risk_audit = risk_factor_audit(
        Path(config["factor_db"]),
        decision_date=scored[0]["decision_date"],
        lag_months=config["factor_lag_months"],
    )
    selected = select_factor_portfolio(
        scored,
        holdings=config["holdings"],
        max_names_per_industry=config["max_names_per_industry"],
        minimum_adv20_usd=config["minimum_adv20_usd"],
    )
    effective_config = {
        key: config[key]
        for key in (
            "holdings", "max_names_per_industry", "minimum_industry_count",
            "minimum_adv20_usd", "winsor_lower", "winsor_upper",
            "factor_lag_months", "allocation_method", "rebalance_frequency", "weights",
        )
    }
    config_hash = hashlib.sha256(
        json.dumps(effective_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "method": (
            "v4_6_r1_factor_selection"
            if config["parameter_mode"] == "frozen"
            else "factor_selection_research_candidate"
        ),
        "research_id": config["research_id"],
        "parameter_mode": config["parameter_mode"],
        "membership_date": scored[0]["membership_date"],
        "decision_date": scored[0]["decision_date"],
        "allocation_method": "equal_weight",
        "weights": weights,
        "effective_config": effective_config,
        "effective_config_sha256": config_hash,
        "baseline_deviations": config["baseline_deviations"],
        "signal_manifest": manifest,
        "risk_factor_audit": risk_audit,
        "selected": selected,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
