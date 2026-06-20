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
DEFAULT_TACO_STRATEGY_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "symbol": "QQQ",
    "dashboard_url": "https://ocmacro.com/dashboard/trump",
    "taco_db": "data/taco_daily.sqlite",
    "jin10_db": "data/jin10_messages.sqlite",
    "jin10_channel": "jinshishuju_bot",
    "smoothing_days": 3,
    "news_half_life_days": 2,
    "risk_beta": -3.0,
    "relief_beta": 5.0,
    "buy_threshold": -4.0,
    "max_data_age_days": 7,
    "require_fresh_news": True,
    "transaction_cost_bps": 10.0,
}


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


def get_taco_strategy_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Return validated configuration for the TACO + Jin10 QQQ timing strategy."""
    if config is None:
        config = load_config()
    raw = config.get("taco_strategy", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    def as_float(key: str) -> float:
        try:
            return float(raw.get(key, DEFAULT_TACO_STRATEGY_CONFIG[key]))
        except (TypeError, ValueError):
            return float(DEFAULT_TACO_STRATEGY_CONFIG[key])

    def as_int(key: str) -> int:
        try:
            return int(raw.get(key, DEFAULT_TACO_STRATEGY_CONFIG[key]))
        except (TypeError, ValueError):
            return int(DEFAULT_TACO_STRATEGY_CONFIG[key])

    symbol = str(raw.get("symbol", DEFAULT_TACO_STRATEGY_CONFIG["symbol"]) or "QQQ").upper().strip()
    return {
        "enabled": _to_bool(raw.get("enabled", True), True),
        "symbol": symbol or "QQQ",
        "dashboard_url": str(raw.get("dashboard_url", DEFAULT_TACO_STRATEGY_CONFIG["dashboard_url"])),
        "taco_db": str(raw.get("taco_db", DEFAULT_TACO_STRATEGY_CONFIG["taco_db"])),
        "jin10_db": str(raw.get("jin10_db", DEFAULT_TACO_STRATEGY_CONFIG["jin10_db"])),
        "jin10_channel": str(raw.get("jin10_channel", DEFAULT_TACO_STRATEGY_CONFIG["jin10_channel"])),
        "smoothing_days": max(as_int("smoothing_days"), 1),
        "news_half_life_days": max(as_int("news_half_life_days"), 1),
        "risk_beta": as_float("risk_beta"),
        "relief_beta": as_float("relief_beta"),
        "buy_threshold": as_float("buy_threshold"),
        "max_data_age_days": max(as_int("max_data_age_days"), 0),
        "require_fresh_news": _to_bool(
            raw.get("require_fresh_news", DEFAULT_TACO_STRATEGY_CONFIG["require_fresh_news"]),
            True,
        ),
        "transaction_cost_bps": max(as_float("transaction_cost_bps"), 0.0),
    }


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
