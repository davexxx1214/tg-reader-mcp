"""Minimal configuration for Telegram plus the single V4.7 Alpaca strategy."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Mapping

import yaml


SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = SKILL_ROOT / "config.yaml"
CONFIG_EXAMPLE_FILE = SKILL_ROOT / "config.example.yaml"
MAX_FACTOR_CAPITAL_USD = 100_000.0

FROZEN_WEIGHTS = {
    "size": 0.10,
    "value": 0.30,
    "profitability": 0.10,
    "investment": 0.30,
    "momentum": 0.20,
}

DEFAULT_FACTOR_PORTFOLIO_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "mode": "v4_7_top10_score_tilt",
    "parameter_mode": "frozen",
    "research_id": "v4_7_0001",
    "factor_db": "data/fama_french_daily.sqlite",
    "signal_input": "data/factor_signal_input.csv",
    "signal_manifest": "data/factor_signal_input.manifest.json",
    "output_path": "data/factor_portfolio_v4_7_latest.json",
    "holdings": 10,
    "max_names_per_industry": 3,
    "minimum_industry_count": 10,
    "minimum_adv20_usd": 10_000_000.0,
    "minimum_signal_rows": 450,
    "winsor_lower": 0.01,
    "winsor_upper": 0.99,
    "factor_lag_months": 2,
    "allocation_method": "score_tilt",
    "score_power": 6.0,
    "minimum_weight": 0.05,
    "maximum_weight": 0.20,
    "maximum_industry_weight": 0.35,
    "rebalance_frequency": "monthly",
    "weights": FROZEN_WEIGHTS,
}

DEFAULT_FACTOR_EXECUTION_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "target_path": "data/factor_portfolio_v4_7_latest.json",
    "approved_target_sha256": "",
    "state_path": "data/factor_execution_state.json",
    "state_key_path": "data/factor_execution_state.key",
    "journal_path": "data/factor_execution_journal.json",
    "maximum_target_age_days": 40,
    "paper_only": True,
    "dedicated_account": True,
    "capital_allocation_usd": 100_000.0,
}

DEFAULT_FACTOR_DATA_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "universe_mode": "latest_only",
    "sp500_repository": "fja05680/sp500",
    "sp500_branch": "master",
    "sp500_file": "sp500.csv",
    "approved_constituent_sha256": "",
    "github_api_base_url": "https://api.github.com",
    "github_raw_base_url": "https://raw.githubusercontent.com",
    "sec_data_base_url": "https://data.sec.gov",
    "sec_archives_base_url": "https://www.sec.gov/Archives",
    "sec_user_agent": "",
    "sec_requests_per_second": 5.0,
    "alpaca_data_base_url": "https://data.alpaca.markets",
    "alpaca_feed": "sip",
    "alpaca_snapshot_feed": "iex",
    "alpaca_adjustment": "all",
    "minimum_constituents": 490,
    "maximum_constituents": 510,
    "minimum_cik_coverage": 0.99,
    "minimum_fundamental_coverage": 0.80,
    "minimum_price_coverage": 0.98,
    "minimum_industry_coverage": 0.95,
    "minimum_complete_signal_coverage": 0.60,
    "price_lookback_sessions": 756,
    "minimum_risk_observations": 504,
    "minimum_adv20_observations": 15,
    "cache_dir": "data/factor_sources",
    "signal_output": "data/factor_signal_input.csv",
    "manifest_output": "data/factor_signal_input.manifest.json",
}


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(config_path: Path | None = None) -> Dict[str, Any]:
    path = config_path or CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(f"Config does not exist: {path}; copy {CONFIG_EXAMPLE_FILE}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Config must be a YAML mapping: {path}")
    return payload


def get_alpaca_credentials(config: Dict[str, Any] | None = None) -> tuple[str, str, bool]:
    payload = config if config is not None else load_config()
    raw = payload.get("alpaca", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(raw, Mapping):
        raise ValueError("alpaca must be a mapping")
    api_key = str(raw.get("api_key", "")).strip()
    secret_key = str(raw.get("secret_key", "")).strip()
    paper = _to_bool(raw.get("paper", True), True)
    if not api_key or api_key.startswith("your_") or not secret_key or secret_key.startswith("your_"):
        raise ValueError("Alpaca API credentials are missing")
    if not paper:
        raise ValueError("V4.7 supports Alpaca Paper only")
    return api_key, secret_key, paper


def get_factor_portfolio_config(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = config if config is not None else load_config()
    raw = payload.get("factor_portfolio", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(raw, Mapping):
        raise ValueError("factor_portfolio must be a mapping")
    effective = {**DEFAULT_FACTOR_PORTFOLIO_CONFIG, **dict(raw)}
    candidate_weights = effective.get("weights")
    if not isinstance(candidate_weights, Mapping) or set(candidate_weights) != set(FROZEN_WEIGHTS):
        raise ValueError("factor_portfolio.weights must contain the five frozen signals")
    weights = {key: float(candidate_weights[key]) for key in FROZEN_WEIGHTS}
    effective["weights"] = weights
    numeric = {
        "holdings": int(effective["holdings"]),
        "max_names_per_industry": int(effective["max_names_per_industry"]),
        "minimum_industry_count": int(effective["minimum_industry_count"]),
        "minimum_adv20_usd": float(effective["minimum_adv20_usd"]),
        "minimum_signal_rows": int(effective["minimum_signal_rows"]),
        "winsor_lower": float(effective["winsor_lower"]),
        "winsor_upper": float(effective["winsor_upper"]),
        "factor_lag_months": int(effective["factor_lag_months"]),
        "score_power": float(effective["score_power"]),
        "minimum_weight": float(effective["minimum_weight"]),
        "maximum_weight": float(effective["maximum_weight"]),
        "maximum_industry_weight": float(effective["maximum_industry_weight"]),
    }
    effective.update(numeric)
    frozen = DEFAULT_FACTOR_PORTFOLIO_CONFIG
    frozen_keys = (
        "mode", "parameter_mode", "research_id", "holdings", "max_names_per_industry",
        "minimum_industry_count", "minimum_adv20_usd", "minimum_signal_rows",
        "winsor_lower", "winsor_upper",
        "factor_lag_months", "allocation_method", "score_power", "minimum_weight",
        "maximum_weight", "maximum_industry_weight", "rebalance_frequency", "weights",
    )
    changed = [key for key in frozen_keys if effective[key] != frozen[key]]
    if changed:
        raise ValueError(f"Frozen V4.7 parameters changed: {', '.join(changed)}")
    effective.update(
        {
            "enabled": _to_bool(effective.get("enabled", True), True),
            "target_method": "v4_7_factor_selection_score_tilt",
            "execution_strategy": "factor-v4.7",
            "state_strategy": "v4_7_top10_score_tilt",
            "baseline_deviations": {},
        }
    )
    return effective


def get_factor_execution_config(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = config if config is not None else load_config()
    raw = payload.get("factor_execution", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(raw, Mapping):
        raise ValueError("factor_execution must be a mapping")
    effective = {**DEFAULT_FACTOR_EXECUTION_CONFIG, **dict(raw)}
    approved = str(effective["approved_target_sha256"]).lower().strip()
    if approved and (
        len(approved) != 64 or any(character not in "0123456789abcdef" for character in approved)
    ):
        raise ValueError("factor_execution.approved_target_sha256 must be a SHA-256")
    result = {
        "enabled": _to_bool(effective["enabled"], True),
        "target_path": str(effective["target_path"]),
        "approved_target_sha256": approved,
        "state_path": str(effective["state_path"]),
        "state_key_path": str(effective["state_key_path"]),
        "journal_path": str(effective["journal_path"]),
        "maximum_target_age_days": int(effective["maximum_target_age_days"]),
        "paper_only": _to_bool(effective["paper_only"], True),
        "dedicated_account": _to_bool(effective["dedicated_account"], True),
        "capital_allocation_usd": float(effective["capital_allocation_usd"]),
    }
    if not result["paper_only"] or not result["dedicated_account"]:
        raise ValueError("V4.7 requires one dedicated Alpaca Paper account")
    if result["maximum_target_age_days"] < 1 or not 0 < result["capital_allocation_usd"] <= MAX_FACTOR_CAPITAL_USD:
        raise ValueError("factor_execution limits must be positive and capital cannot exceed 100000")
    return result


def get_factor_data_config(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return the live-only data contract used to build the current V4.7 cross-section."""

    payload = config if config is not None else load_config()
    raw = payload.get("factor_data", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(raw, Mapping):
        raise ValueError("factor_data must be a mapping")
    effective = {**DEFAULT_FACTOR_DATA_CONFIG, **dict(raw)}
    if not str(effective.get("sec_user_agent", "")).strip():
        effective["sec_user_agent"] = os.environ.get("SEC_USER_AGENT", "").strip()
    user_agent = str(effective["sec_user_agent"]).strip()
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent):
        raise ValueError("factor_data.sec_user_agent must include a contact email")
    approved_constituents = str(effective.get("approved_constituent_sha256", "")).lower().strip()
    if approved_constituents and not re.fullmatch(r"[0-9a-f]{64}", approved_constituents):
        raise ValueError("factor_data.approved_constituent_sha256 must be a SHA-256")

    frozen = DEFAULT_FACTOR_DATA_CONFIG
    frozen_keys = (
        "universe_mode", "sp500_repository", "sp500_branch", "sp500_file",
        "github_api_base_url", "github_raw_base_url", "sec_data_base_url",
        "sec_archives_base_url", "alpaca_data_base_url",
        "alpaca_feed", "alpaca_snapshot_feed", "alpaca_adjustment", "minimum_constituents",
        "maximum_constituents", "price_lookback_sessions",
        "minimum_risk_observations", "minimum_adv20_observations",
        "minimum_cik_coverage", "minimum_fundamental_coverage",
        "minimum_price_coverage", "minimum_industry_coverage",
        "minimum_complete_signal_coverage",
    )
    changed = [key for key in frozen_keys if effective[key] != frozen[key]]
    if changed:
        raise ValueError(f"Frozen live factor-data parameters changed: {', '.join(changed)}")

    numeric = {
        "sec_requests_per_second": float(effective["sec_requests_per_second"]),
        "minimum_constituents": int(effective["minimum_constituents"]),
        "maximum_constituents": int(effective["maximum_constituents"]),
        "minimum_cik_coverage": float(effective["minimum_cik_coverage"]),
        "minimum_fundamental_coverage": float(effective["minimum_fundamental_coverage"]),
        "minimum_price_coverage": float(effective["minimum_price_coverage"]),
        "minimum_industry_coverage": float(effective["minimum_industry_coverage"]),
        "minimum_complete_signal_coverage": float(effective["minimum_complete_signal_coverage"]),
        "price_lookback_sessions": int(effective["price_lookback_sessions"]),
        "minimum_risk_observations": int(effective["minimum_risk_observations"]),
        "minimum_adv20_observations": int(effective["minimum_adv20_observations"]),
    }
    effective.update(numeric)
    if not 0 < effective["sec_requests_per_second"] <= 10:
        raise ValueError("SEC request rate must be between 0 and 10 per second")
    if effective["minimum_constituents"] > effective["maximum_constituents"]:
        raise ValueError("factor-data constituent bounds are invalid")
    for key in (
        "minimum_cik_coverage", "minimum_fundamental_coverage",
        "minimum_price_coverage", "minimum_industry_coverage",
        "minimum_complete_signal_coverage",
    ):
        if not 0 < effective[key] <= 1:
            raise ValueError(f"factor_data.{key} must be in (0, 1]")
    effective["enabled"] = _to_bool(effective.get("enabled", True), True)
    effective["sec_user_agent"] = user_agent
    effective["approved_constituent_sha256"] = approved_constituents
    return effective
