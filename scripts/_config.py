"""
共享配置加载模块

从 skills/alpaca-live-trading/config.yaml 读取 API Key 配置。
所有 skill 脚本统一通过此模块获取配置，不依赖项目根目录 .env。

用法:
    from _config import load_config
    config = load_config()
    api_key = config["alphavantage"]["api_key"]
"""

import sys
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:
    print("❌ 缺少 pyyaml 依赖，请安装: pip install pyyaml")
    sys.exit(1)

# Skill 根目录: skills/alpaca-live-trading/
SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = SKILL_ROOT / "config.yaml"
CONFIG_EXAMPLE_FILE = SKILL_ROOT / "config.example.yaml"
DEFAULT_STRATEGY_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "name": "",
    "names": [],
    "min_confidence": 0.6,
    "prefilter_top_k": 10,
}
DEFAULT_RISK_CONFIG: Dict[str, Any] = {
    "max_position_pct": 0.1,
    "max_positions": 5,
    "max_trade_notional": 2000.0,
}
DEFAULT_MARKET_GATE_CONFIG: Dict[str, Any] = {
    "benchmark_tickers": ["QQQ", "SPY"],
    "threshold": -0.05,
}
DEFAULT_NTACO_STRATEGY_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "symbol": "QQQ",
    "dashboard_url": "https://ocmacro.com/dashboard/trump",
    "taco_db": "data/taco_daily.sqlite",
    "state_file": "data/ntaco_strategy_state.json",
    "normalization_lookback": 42,
    "lower_threshold": 0.30,
    "upper_threshold": 0.49,
    "buy_exposure": 1.0,
    "sell_fraction": 0.20,
    "max_data_age_days": 7,
    "transaction_cost_bps": 5.0,
}
DEFAULT_FACTOR_PORTFOLIO_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "mode": "v4_6_r1_top10",
    "parameter_mode": "frozen",
    "research_id": "v4_6_r1_0001",
    "factor_db": "data/fama_french_daily.sqlite",
    "signal_input": "data/factor_signal_input.csv",
    "signal_manifest": "data/factor_signal_input.manifest.json",
    "output_path": "data/factor_portfolio_latest.json",
    "holdings": 10,
    "max_names_per_industry": 3,
    "minimum_industry_count": 10,
    "minimum_adv20_usd": 10_000_000.0,
    "winsor_lower": 0.01,
    "winsor_upper": 0.99,
    "factor_lag_months": 2,
    "allocation_method": "equal_weight",
    "rebalance_frequency": "monthly",
    "weights": {
        "size": 0.10,
        "value": 0.30,
        "profitability": 0.10,
        "investment": 0.30,
        "momentum": 0.20,
    },
}
DEFAULT_FACTOR_EXECUTION_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "target_path": "data/factor_portfolio_latest.json",
    "approved_target_sha256": "",
    "state_path": "data/factor_execution_state.json",
    "state_key_path": "data/factor_execution_state.key",
    "journal_path": "data/factor_execution_journal.json",
    "maximum_target_age_days": 40,
    "legacy_managed_symbols": [],
    "preserve_unmanaged_positions": True,
    "paper_only": True,
    "capital_allocation_usd": 100_000.0,
}
MAX_FACTOR_CAPITAL_USD = 100_000.0


def load_config(config_path: Path = None) -> Dict[str, Any]:
    """
    加载 config.yaml 配置文件

    Args:
        config_path: 配置文件路径，默认为 skills/alpaca-live-trading/config.yaml

    Returns:
        配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置文件格式错误
    """
    if config_path is None:
        config_path = CONFIG_FILE

    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}")
        print(f"   请复制模板并填入真实 API Key:")
        print(f"   cp {CONFIG_EXAMPLE_FILE} {CONFIG_FILE}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        print(f"❌ 配置文件格式错误: {config_path}")
        sys.exit(1)

    return config


def get_alphavantage_key(config: Dict[str, Any] = None) -> str:
    """获取 AlphaVantage API Key"""
    if config is None:
        config = load_config()
    key = config.get("alphavantage", {}).get("api_key", "")
    if not key or key.startswith("your_"):
        print("❌ AlphaVantage API Key 未配置，请在 config.yaml 中填入真实 Key")
        sys.exit(1)
    return key


def get_alpaca_credentials(config: Dict[str, Any] = None) -> tuple:
    """
    获取 Alpaca API 凭证

    Args:
        config: 配置字典，为空则自动加载

    Returns:
        (api_key, secret_key, paper) 元组，paper 为 bool 表示是否模拟交易
    """
    if config is None:
        config = load_config()
    alpaca_config = config.get("alpaca", {})
    api_key = alpaca_config.get("api_key", "")
    secret_key = alpaca_config.get("secret_key", "")
    paper = alpaca_config.get("paper", True)
    if not api_key or api_key.startswith("your_") or not secret_key or secret_key.startswith("your_"):
        print("❌ Alpaca API 凭证未配置，请在 config.yaml 中填入真实 Key")
        sys.exit(1)
    return api_key, secret_key, paper


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def get_strategy_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    获取策略配置，带默认值和基础校验。
    """
    if config is None:
        config = load_config()

    raw = config.get("strategy", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    name = str(raw.get("name", DEFAULT_STRATEGY_CONFIG["name"]) or "").strip()

    names = raw.get("names", DEFAULT_STRATEGY_CONFIG["names"])
    if not isinstance(names, list):
        names = []
    names = [str(name).strip() for name in names if str(name).strip()]
    selected_name = name or (names[0] if names else "")

    try:
        prefilter_top_k = int(raw.get("prefilter_top_k", DEFAULT_STRATEGY_CONFIG["prefilter_top_k"]))
    except (TypeError, ValueError):
        prefilter_top_k = int(DEFAULT_STRATEGY_CONFIG["prefilter_top_k"])

    try:
        min_conf = float(raw.get("min_confidence", DEFAULT_STRATEGY_CONFIG["min_confidence"]))
    except (TypeError, ValueError):
        min_conf = float(DEFAULT_STRATEGY_CONFIG["min_confidence"])

    return {
        "enabled": _to_bool(raw.get("enabled", DEFAULT_STRATEGY_CONFIG["enabled"]), DEFAULT_STRATEGY_CONFIG["enabled"]),
        "name": selected_name,
        "names": names,
        "min_confidence": _clamp(min_conf, 0.0, 1.0),
        "prefilter_top_k": max(prefilter_top_k, 1),
    }


def get_ntaco_strategy_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Return validated configuration for the nTACO 100/20 QQQ strategy."""
    if config is None:
        config = load_config()
    raw = config.get("ntaco_strategy", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict) or not raw:
        legacy = config.get("taco_strategy", {}) if isinstance(config, dict) else {}
        legacy = legacy if isinstance(legacy, dict) else {}
        raw = {
            key: legacy[key]
            for key in ("enabled", "symbol", "dashboard_url", "taco_db", "max_data_age_days")
            if key in legacy
        }

    def as_float(key: str) -> float:
        try:
            return float(raw.get(key, DEFAULT_NTACO_STRATEGY_CONFIG[key]))
        except (TypeError, ValueError):
            return float(DEFAULT_NTACO_STRATEGY_CONFIG[key])

    def as_int(key: str) -> int:
        try:
            return int(raw.get(key, DEFAULT_NTACO_STRATEGY_CONFIG[key]))
        except (TypeError, ValueError):
            return int(DEFAULT_NTACO_STRATEGY_CONFIG[key])

    symbol = str(raw.get("symbol", DEFAULT_NTACO_STRATEGY_CONFIG["symbol"]) or "QQQ").upper().strip()
    lower = _clamp(as_float("lower_threshold"), 0.0, 1.0)
    upper = _clamp(as_float("upper_threshold"), 0.0, 1.0)
    if lower >= upper:
        lower = float(DEFAULT_NTACO_STRATEGY_CONFIG["lower_threshold"])
        upper = float(DEFAULT_NTACO_STRATEGY_CONFIG["upper_threshold"])
    return {
        "enabled": _to_bool(raw.get("enabled", True), True),
        "symbol": symbol or "QQQ",
        "dashboard_url": str(raw.get("dashboard_url", DEFAULT_NTACO_STRATEGY_CONFIG["dashboard_url"])),
        "taco_db": str(raw.get("taco_db", DEFAULT_NTACO_STRATEGY_CONFIG["taco_db"])),
        "state_file": str(raw.get("state_file", DEFAULT_NTACO_STRATEGY_CONFIG["state_file"])),
        "normalization_lookback": max(as_int("normalization_lookback"), 1),
        "lower_threshold": lower,
        "upper_threshold": upper,
        "buy_exposure": _clamp(as_float("buy_exposure"), 0.0, 1.0),
        "sell_fraction": _clamp(as_float("sell_fraction"), 0.0, 1.0),
        "max_data_age_days": max(as_int("max_data_age_days"), 0),
        "transaction_cost_bps": max(as_float("transaction_cost_bps"), 0.0),
    }


def get_factor_portfolio_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Return validated settings for the standalone V4.6-R1 stock selector."""
    if config is None:
        config = load_config()
    raw = config.get("factor_portfolio", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    def as_float(key: str) -> float:
        try:
            return float(raw.get(key, DEFAULT_FACTOR_PORTFOLIO_CONFIG[key]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"factor_portfolio.{key} must be numeric") from exc

    def as_int(key: str) -> int:
        try:
            return int(raw.get(key, DEFAULT_FACTOR_PORTFOLIO_CONFIG[key]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"factor_portfolio.{key} must be an integer") from exc

    expected = set(DEFAULT_FACTOR_PORTFOLIO_CONFIG["weights"])
    candidate = raw.get("weights", DEFAULT_FACTOR_PORTFOLIO_CONFIG["weights"])
    if not isinstance(candidate, dict) or set(candidate) != expected:
        raise ValueError("factor_portfolio.weights must contain exactly the five frozen signals")
    try:
        parsed = {key: float(candidate[key]) for key in expected}
    except (TypeError, ValueError) as exc:
        raise ValueError("factor_portfolio.weights must be numeric") from exc
    if any(value < 0.0 for value in parsed.values()) or abs(sum(parsed.values()) - 1.0) > 1e-12:
        raise ValueError("factor_portfolio.weights must be non-negative and sum to 1")
    weights = {key: parsed[key] for key in DEFAULT_FACTOR_PORTFOLIO_CONFIG["weights"]}

    lower = as_float("winsor_lower")
    upper = as_float("winsor_upper")
    if not 0.0 <= lower < upper <= 1.0:
        raise ValueError("factor_portfolio winsor limits are invalid")
    allocation = str(raw.get("allocation_method", "equal_weight") or "equal_weight")
    if allocation != "equal_weight":
        raise ValueError("factor_portfolio only supports equal_weight allocation")
    mode = str(raw.get("mode", DEFAULT_FACTOR_PORTFOLIO_CONFIG["mode"]))
    if mode != "v4_6_r1_top10":
        raise ValueError("factor_portfolio only supports v4_6_r1_top10 mode")
    rebalance = str(raw.get("rebalance_frequency", "monthly"))
    if rebalance != "monthly":
        raise ValueError("factor_portfolio only supports monthly rebalancing")
    parameter_mode = str(raw.get("parameter_mode", "frozen")).strip().lower()
    if parameter_mode not in {"frozen", "research"}:
        raise ValueError("factor_portfolio.parameter_mode must be frozen or research")
    research_id = str(raw.get("research_id", DEFAULT_FACTOR_PORTFOLIO_CONFIG["research_id"])).strip()
    effective = {
        "enabled": _to_bool(raw.get("enabled", True), True),
        "mode": mode,
        "parameter_mode": parameter_mode,
        "research_id": research_id,
        "factor_db": str(raw.get("factor_db", DEFAULT_FACTOR_PORTFOLIO_CONFIG["factor_db"])),
        "signal_input": str(raw.get("signal_input", DEFAULT_FACTOR_PORTFOLIO_CONFIG["signal_input"])),
        "signal_manifest": str(raw.get("signal_manifest", DEFAULT_FACTOR_PORTFOLIO_CONFIG["signal_manifest"])),
        "output_path": str(raw.get("output_path", DEFAULT_FACTOR_PORTFOLIO_CONFIG["output_path"])),
        "holdings": as_int("holdings"),
        "max_names_per_industry": as_int("max_names_per_industry"),
        "minimum_industry_count": as_int("minimum_industry_count"),
        "minimum_adv20_usd": as_float("minimum_adv20_usd"),
        "winsor_lower": lower,
        "winsor_upper": upper,
        "factor_lag_months": as_int("factor_lag_months"),
        "allocation_method": allocation,
        "rebalance_frequency": rebalance,
        "weights": weights,
    }
    if (
        effective["holdings"] < 1
        or effective["max_names_per_industry"] < 1
        or effective["minimum_industry_count"] < 1
        or effective["minimum_adv20_usd"] < 0.0
        or effective["factor_lag_months"] < 2
    ):
        raise ValueError("factor_portfolio numeric limits are outside their safe range")
    tunable = (
        "mode", "holdings", "max_names_per_industry", "minimum_industry_count",
        "minimum_adv20_usd", "winsor_lower", "winsor_upper", "factor_lag_months",
        "allocation_method", "rebalance_frequency", "weights",
    )
    deviations = {
        key: {"baseline": DEFAULT_FACTOR_PORTFOLIO_CONFIG[key], "effective": effective[key]}
        for key in tunable
        if effective[key] != DEFAULT_FACTOR_PORTFOLIO_CONFIG[key]
    }
    if parameter_mode == "frozen" and deviations:
        raise ValueError(f"frozen V4.6-R1 parameters were changed: {', '.join(deviations)}")
    if parameter_mode == "research" and (
        not research_id or research_id == DEFAULT_FACTOR_PORTFOLIO_CONFIG["research_id"]
    ):
        raise ValueError("research mode requires a new, non-baseline research_id")
    effective["baseline_deviations"] = deviations
    return effective


def get_factor_execution_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Return the isolated broker-execution contract for the frozen factor basket."""
    if config is None:
        config = load_config()
    raw = config.get("factor_execution", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raise ValueError("factor_execution must be a mapping")
    legacy = raw.get(
        "legacy_managed_symbols", DEFAULT_FACTOR_EXECUTION_CONFIG["legacy_managed_symbols"]
    )
    if not isinstance(legacy, list):
        raise ValueError("factor_execution.legacy_managed_symbols must be a list")
    try:
        maximum_age = int(
            raw.get("maximum_target_age_days", DEFAULT_FACTOR_EXECUTION_CONFIG["maximum_target_age_days"])
        )
        capital = float(
            raw.get("capital_allocation_usd", DEFAULT_FACTOR_EXECUTION_CONFIG["capital_allocation_usd"])
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("factor_execution numeric settings are invalid") from exc
    approved_hash = str(raw.get("approved_target_sha256", "")).lower().strip()
    if approved_hash and (
        len(approved_hash) != 64
        or any(character not in "0123456789abcdef" for character in approved_hash)
    ):
        raise ValueError("factor_execution.approved_target_sha256 must be a SHA-256")
    result = {
        "enabled": _to_bool(raw.get("enabled", True), True),
        "target_path": str(raw.get("target_path", DEFAULT_FACTOR_EXECUTION_CONFIG["target_path"])),
        "approved_target_sha256": approved_hash,
        "state_path": str(raw.get("state_path", DEFAULT_FACTOR_EXECUTION_CONFIG["state_path"])),
        "state_key_path": str(raw.get("state_key_path", DEFAULT_FACTOR_EXECUTION_CONFIG["state_key_path"])),
        "journal_path": str(raw.get("journal_path", DEFAULT_FACTOR_EXECUTION_CONFIG["journal_path"])),
        "maximum_target_age_days": maximum_age,
        "legacy_managed_symbols": list(
            dict.fromkeys(str(symbol).upper().strip() for symbol in legacy if str(symbol).strip())
        ),
        "preserve_unmanaged_positions": _to_bool(
            raw.get("preserve_unmanaged_positions", True), True
        ),
        "paper_only": _to_bool(raw.get("paper_only", True), True),
        "capital_allocation_usd": capital,
    }
    if maximum_age < 1 or capital <= 0.0:
        raise ValueError("factor_execution limits must be positive")
    if capital > MAX_FACTOR_CAPITAL_USD:
        raise ValueError("factor_execution.capital_allocation_usd cannot exceed 100000")
    if not result["preserve_unmanaged_positions"]:
        raise ValueError("factor execution must preserve unmanaged account positions")
    if not result["paper_only"]:
        raise ValueError("V4.6-R1 factor execution is always Paper-only")
    return result


def get_risk_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    获取风控配置，带默认值和基础校验。
    """
    if config is None:
        config = load_config()

    raw = config.get("risk", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    try:
        max_position_pct = float(raw.get("max_position_pct", DEFAULT_RISK_CONFIG["max_position_pct"]))
    except (TypeError, ValueError):
        max_position_pct = float(DEFAULT_RISK_CONFIG["max_position_pct"])

    try:
        max_positions = int(raw.get("max_positions", DEFAULT_RISK_CONFIG["max_positions"]))
    except (TypeError, ValueError):
        max_positions = int(DEFAULT_RISK_CONFIG["max_positions"])

    try:
        max_trade_notional = float(raw.get("max_trade_notional", DEFAULT_RISK_CONFIG["max_trade_notional"]))
    except (TypeError, ValueError):
        max_trade_notional = float(DEFAULT_RISK_CONFIG["max_trade_notional"])

    return {
        "max_position_pct": _clamp(max_position_pct, 0.0, 1.0),
        "max_positions": max(max_positions, 1),
        "max_trade_notional": max(max_trade_notional, 0.0),
    }


DEFAULT_TELEGRAM_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "session_path": "",
    "channels": [{"name": "jinshishuju_bot", "type": "dm", "limit": 50}],
    "ticker_map": "scripts/tg_ticker_map.yaml",
    "sentiment_mode": "keyword",
    "tg_weight": 0.8,
    "max_articles_per_ticker": 50,
}


def get_telegram_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """获取 Telegram 新闻源配置，带默认值。"""
    if config is None:
        config = load_config()

    raw = config.get("telegram", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    channels = raw.get("channels", DEFAULT_TELEGRAM_CONFIG["channels"])
    if not isinstance(channels, list):
        channels = list(DEFAULT_TELEGRAM_CONFIG["channels"])

    try:
        tg_weight = float(raw.get("tg_weight", DEFAULT_TELEGRAM_CONFIG["tg_weight"]))
    except (TypeError, ValueError):
        tg_weight = float(DEFAULT_TELEGRAM_CONFIG["tg_weight"])

    try:
        max_articles = int(raw.get("max_articles_per_ticker", DEFAULT_TELEGRAM_CONFIG["max_articles_per_ticker"]))
    except (TypeError, ValueError):
        max_articles = int(DEFAULT_TELEGRAM_CONFIG["max_articles_per_ticker"])

    return {
        "enabled": _to_bool(raw.get("enabled", DEFAULT_TELEGRAM_CONFIG["enabled"]), DEFAULT_TELEGRAM_CONFIG["enabled"]),
        "session_path": str(raw.get("session_path", DEFAULT_TELEGRAM_CONFIG["session_path"]) or ""),
        "channels": channels,
        "ticker_map": str(raw.get("ticker_map", DEFAULT_TELEGRAM_CONFIG["ticker_map"]) or ""),
        "sentiment_mode": str(raw.get("sentiment_mode", DEFAULT_TELEGRAM_CONFIG["sentiment_mode"]) or "keyword"),
        "tg_weight": _clamp(tg_weight, 0.0, 1.0),
        "max_articles_per_ticker": max(max_articles, 1),
    }


def get_market_gate_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    获取市场门控配置，带默认值和基础校验。
    """
    if config is None:
        config = load_config()

    raw = config.get("market_gate", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    benchmark_tickers = raw.get("benchmark_tickers", DEFAULT_MARKET_GATE_CONFIG["benchmark_tickers"])
    if isinstance(benchmark_tickers, str):
        benchmark_tickers = [x.strip().upper() for x in benchmark_tickers.split(",") if x.strip()]
    elif isinstance(benchmark_tickers, list):
        benchmark_tickers = [str(x).strip().upper() for x in benchmark_tickers if str(x).strip()]
    else:
        benchmark_tickers = list(DEFAULT_MARKET_GATE_CONFIG["benchmark_tickers"])

    if not benchmark_tickers:
        benchmark_tickers = list(DEFAULT_MARKET_GATE_CONFIG["benchmark_tickers"])

    try:
        threshold = float(raw.get("threshold", DEFAULT_MARKET_GATE_CONFIG["threshold"]))
    except (TypeError, ValueError):
        threshold = float(DEFAULT_MARKET_GATE_CONFIG["threshold"])

    return {
        "benchmark_tickers": benchmark_tickers,
        "threshold": _clamp(threshold, -1.0, 1.0),
    }
