#!/usr/bin/env python3
"""
查询股票实时价格 - 通过 Alpaca 行情 + SQLite 技术指标

用法:
    python query_stock_prices.py                    # 查询 NASDAQ 100 热门股票
    python query_stock_prices.py AAPL MSFT NVDA    # 查询指定股票
"""

import sys
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
# 将 scripts 目录加入 Python 路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import get_alpaca_credentials, load_config

# 默认查询: NASDAQ 100 + QQQ (共 101)
DEFAULT_SYMBOLS = [
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
    "QQQ"
]

DEFAULT_DATA_BASE_URL = "https://data.alpaca.markets"


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        token = _normalize_symbol(item)
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _chunk_symbols(symbols: List[str], chunk_size: int) -> List[List[str]]:
    size = max(int(chunk_size), 1)
    return [symbols[i:i + size] for i in range(0, len(symbols), size)]


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / float(period)


def _ema_series(values: List[float], period: int) -> List[float]:
    if len(values) < period or period <= 0:
        return []
    seed = sum(values[:period]) / float(period)
    alpha = 2.0 / (period + 1.0)
    out = [seed]
    for value in values[period:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _rsi_wilder(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for idx in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(values: List[float]) -> tuple[Optional[float], Optional[float]]:
    ema12 = _ema_series(values, 12)
    ema26 = _ema_series(values, 26)
    if not ema12 or not ema26:
        return None, None
    macd_line: List[float] = []
    # ema12 从 index=11 开始，ema26 从 index=25 开始
    for abs_idx in range(25, len(values)):
        ema12_idx = abs_idx - 11
        ema26_idx = abs_idx - 25
        if 0 <= ema12_idx < len(ema12) and 0 <= ema26_idx < len(ema26):
            macd_line.append(ema12[ema12_idx] - ema26[ema26_idx])
    if not macd_line:
        return None, None
    signal_series = _ema_series(macd_line, 9)
    macd_latest = macd_line[-1]
    signal_latest = signal_series[-1] if signal_series else None
    return macd_latest, signal_latest


def _technical_from_closes(closes: List[float], live_price: Optional[float]) -> Dict[str, Any]:
    if not closes:
        return {}
    series = closes.copy()
    if live_price is not None and live_price > 0:
        # 用实时价替换最后一个收盘价，提升当日指标时效性
        series[-1] = float(live_price)

    rsi_14 = _rsi_wilder(series, period=14)
    sma20 = _sma(series, 20)
    sma50 = _sma(series, 50)
    ema20_series = _ema_series(series, 20)
    ema50_series = _ema_series(series, 50)
    ema20 = ema20_series[-1] if ema20_series else None
    ema50 = ema50_series[-1] if ema50_series else None
    macd, macd_signal = _macd(series)

    price = live_price if live_price is not None and live_price > 0 else series[-1]
    ma_votes: List[float] = []
    if sma20 is not None:
        ma_votes.append(1.0 if price >= sma20 else -1.0)
    if sma50 is not None:
        ma_votes.append(1.0 if price >= sma50 else -1.0)
    if ema20 is not None:
        ma_votes.append(1.0 if price >= ema20 else -1.0)
    if ema50 is not None:
        ma_votes.append(1.0 if price >= ema50 else -1.0)
    if sma20 is not None and sma50 is not None:
        ma_votes.append(1.0 if sma20 >= sma50 else -1.0)
    if ema20 is not None and ema50 is not None:
        ma_votes.append(1.0 if ema20 >= ema50 else -1.0)
    recommend_ma = (sum(ma_votes) / len(ma_votes)) if ma_votes else 0.0

    other_votes: List[float] = []
    if rsi_14 is not None:
        other_votes.append(_clamp((rsi_14 - 50.0) / 20.0, -1.0, 1.0))
    if macd is not None and macd_signal is not None:
        denom = max(abs(macd) + abs(macd_signal), 0.05)
        other_votes.append(_clamp((macd - macd_signal) / denom, -1.0, 1.0))
    recommend_other = (sum(other_votes) / len(other_votes)) if other_votes else 0.0
    recommend_all = (recommend_ma + recommend_other) / 2.0

    return {
        "rsi_14": round(rsi_14, 6) if rsi_14 is not None else None,
        "macd": round(macd, 6) if macd is not None else None,
        "macd_signal": round(macd_signal, 6) if macd_signal is not None else None,
        "sma20": round(sma20, 6) if sma20 is not None else None,
        "sma50": round(sma50, 6) if sma50 is not None else None,
        "ema20": round(ema20, 6) if ema20 is not None else None,
        "ema50": round(ema50, 6) if ema50 is not None else None,
        "recommend_ma": round(recommend_ma, 6),
        "recommend_other": round(recommend_other, 6),
        "recommend_all": round(recommend_all, 6),
    }


def _load_close_history(symbols: List[str], limit_per_symbol: int = 300) -> Dict[str, List[float]]:
    db_path = Path(__file__).resolve().parent.parent / "data" / "stock_daily.sqlite"
    if not db_path.exists():
        return {}
    result: Dict[str, List[float]] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        for symbol in symbols:
            rows = conn.execute(
                """
                SELECT close
                FROM stock_daily
                WHERE symbol = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (symbol, max(int(limit_per_symbol), 60)),
            ).fetchall()
            closes_desc = [_to_float(row[0]) for row in rows]
            closes = [float(x) for x in reversed(closes_desc) if x is not None and x > 0]
            if closes:
                result[symbol] = closes
    finally:
        conn.close()
    return result


def _alpaca_headers() -> Dict[str, str]:
    config = load_config()
    api_key, secret_key, _ = get_alpaca_credentials(config)
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }


def _fetch_alpaca_snapshots(symbols: List[str], timeout_seconds: float) -> Dict[str, Dict[str, Any]]:
    headers = _alpaca_headers()
    feed = os.getenv("ALPACA_DATA_FEED", "iex").strip() or "iex"
    chunk_size = int(os.getenv("ALPACA_SNAPSHOT_CHUNK_SIZE", "200") or "200")
    base_url = os.getenv("ALPACA_DATA_BASE_URL", DEFAULT_DATA_BASE_URL).strip() or DEFAULT_DATA_BASE_URL
    endpoint = f"{base_url.rstrip('/')}/v2/stocks/snapshots"

    quotes: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunk_symbols(symbols, chunk_size):
        params = {
            "symbols": ",".join(chunk),
            "feed": feed,
        }
        resp = requests.get(endpoint, headers=headers, params=params, timeout=max(timeout_seconds, 1.0))
        resp.raise_for_status()
        payload = resp.json()
        snapshots = payload.get("snapshots", payload) if isinstance(payload, dict) else {}
        if not isinstance(snapshots, dict):
            continue
        for symbol in chunk:
            snap = snapshots.get(symbol) or snapshots.get(symbol.upper())
            if not isinstance(snap, dict):
                continue
            latest_trade = snap.get("latestTrade", {}) if isinstance(snap.get("latestTrade"), dict) else {}
            latest_quote = snap.get("latestQuote", {}) if isinstance(snap.get("latestQuote"), dict) else {}
            daily_bar = snap.get("dailyBar", {}) if isinstance(snap.get("dailyBar"), dict) else {}
            prev_daily_bar = snap.get("prevDailyBar", {}) if isinstance(snap.get("prevDailyBar"), dict) else {}

            trade_price = _to_float(latest_trade.get("p"))
            ask_price = _to_float(latest_quote.get("ap"))
            bid_price = _to_float(latest_quote.get("bp"))
            daily_close = _to_float(daily_bar.get("c"))
            prev_close = _to_float(prev_daily_bar.get("c"))

            price: Optional[float] = trade_price
            if price is None and ask_price is not None and bid_price is not None:
                price = (ask_price + bid_price) / 2.0
            if price is None:
                price = daily_close or prev_close
            if price is None:
                continue

            change = 0.0
            change_pct = 0.0
            if prev_close is not None and prev_close > 0:
                change = float(price - prev_close)
                change_pct = float(change / prev_close * 100.0)
            elif daily_close is not None and daily_close > 0 and prev_close is not None and prev_close > 0:
                change = float(daily_close - prev_close)
                change_pct = float(change / prev_close * 100.0)

            quotes[symbol] = {
                "symbol": symbol,
                "price": float(price),
                "change": change,
                "change_pct": change_pct,
                "volume": float(_to_float(daily_bar.get("v")) or 0.0),
                "technical": {},
            }
    return quotes


def _get_snapshot_timeout_seconds() -> float:
    raw = (
        os.getenv("ALPACA_SNAPSHOT_TIMEOUT_SECONDS", "").strip()
        or os.getenv("TVSCREENER_SNAPSHOT_TIMEOUT_SECONDS", "").strip()
    )
    if not raw:
        return 50.0
    try:
        return max(float(raw), 1.0)
    except ValueError:
        return 50.0


def _is_snapshot_fetch_skipped() -> bool:
    raw = (
        os.getenv("ALPACA_SKIP_SNAPSHOT_FETCH", "").strip()
        or os.getenv("TVSCREENER_SKIP_SNAPSHOT_FETCH", "").strip()
    ).lower()
    return raw in {"1", "true", "yes", "on"}


def _load_cached_quotes(max_age_seconds: float = 3600.0) -> Optional[List[Dict[str, Any]]]:
    """
    从本地 stock_prices_latest.json 回退读取最近一次可用报价。
    """
    cache_path = Path(__file__).resolve().parent.parent / "data" / "stock_prices_latest.json"
    if not cache_path.exists():
        return None

    try:
        age = (datetime.now().timestamp() - cache_path.stat().st_mtime)
        if age > max_age_seconds:
            print(
                f"⚠️ 本地快照已过期（{int(age)}s > {int(max_age_seconds)}s），跳过回退: {cache_path}"
            )
            return None
    except Exception:
        # 读取文件年龄失败时，继续尝试解析内容
        pass

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if isinstance(results, list) and results:
            print(f"ℹ️ 使用本地缓存报价快照（{len(results)} 条）: {cache_path}")
            return results
    except Exception as e:
        print(f"⚠️ 读取本地快照失败: {e}")
    return None


def _load_market_snapshot(symbols: Optional[List[str]] = None, timeout_seconds: Optional[float] = None) -> Any:
    """
    拉取 Alpaca 行情快照，并结合 SQLite 历史日线计算技术面。
    """
    if _is_snapshot_fetch_skipped():
        print("ℹ️ 已配置跳过 Alpaca 实时快照拉取（ALPACA_SKIP_SNAPSHOT_FETCH=1）。")
        return _load_cached_quotes()

    target_symbols = _dedupe_keep_order(symbols or DEFAULT_SYMBOLS)
    if not target_symbols:
        return None

    timeout = _get_snapshot_timeout_seconds() if timeout_seconds is None else max(timeout_seconds, 1.0)
    result_holder: Dict[str, Any] = {"snapshot": None, "error": None}

    def _fetch_snapshot() -> None:
        try:
            quotes = _fetch_alpaca_snapshots(target_symbols, timeout_seconds=timeout)
            history_map = _load_close_history(target_symbols)
            for symbol in target_symbols:
                if symbol not in quotes:
                    continue
                closes = history_map.get(symbol, [])
                quotes[symbol]["technical"] = _technical_from_closes(
                    closes=closes,
                    live_price=_to_float(quotes[symbol].get("price")),
                )
            result_holder["snapshot"] = {
                "source": "alpaca+sqlite",
                "quotes": quotes,
            }
        except BaseException as e:
            result_holder["error"] = e

    thread = threading.Thread(target=_fetch_snapshot, name="alpaca-snapshot-loader", daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        print(f"⚠️ Alpaca 快照拉取超时（>{timeout:.1f}s），回退本地缓存。")
        return _load_cached_quotes()

    if result_holder["error"] is not None:
        print(f"⚠️ Alpaca 快照拉取失败: {result_holder['error']}，回退本地缓存。")
        return _load_cached_quotes()

    return result_holder["snapshot"]


def get_quote(symbol: str, snapshot) -> Dict[str, Any]:
    """
    获取股票的实时报价（Alpaca）+ 技术面（SQLite）

    Args:
        symbol: 股票代码（例如 NVDA 或 NASDAQ:NVDA）
        snapshot: Alpaca 快照对象 / 缓存列表

    Returns:
        包含报价信息的字典
    """
    if snapshot is None:
        return {"error": "行情快照未就绪"}

    token = symbol.split(":")[-1].upper()

    # 首选模式：snapshot 为 {"source": "...", "quotes": {...}} 结构
    if isinstance(snapshot, dict) and isinstance(snapshot.get("quotes"), dict):
        item = snapshot["quotes"].get(token) or snapshot["quotes"].get(symbol)
        if isinstance(item, dict):
            return item
        return {"error": "无数据"}

    # 回退模式：snapshot 为本地缓存报价列表（write_latest_snapshot 的结果）
    if isinstance(snapshot, list):
        for item in snapshot:
            if not isinstance(item, dict):
                continue
            row_symbol = str(item.get("symbol", "")).upper().split(":")[-1]
            if row_symbol == token:
                return item
        return {"error": "无数据"}

    # 兼容历史遗留：若仍传入 DataFrame（旧 tvscreener 逻辑），继续支持解析
    if not hasattr(snapshot, "iloc"):
        return {"error": "无数据"}
    symbol_col = "Symbol" if hasattr(snapshot, "columns") and "Symbol" in snapshot.columns else None
    row = snapshot[snapshot[symbol_col] == symbol] if symbol_col else snapshot.iloc[0:0]
    if row.empty and symbol_col:
        row = snapshot[snapshot[symbol_col].astype(str).str.upper() == token]
    if row.empty and symbol_col:
        row = snapshot[snapshot[symbol_col].astype(str).str.upper().str.endswith(f":{token}")]
    if row.empty and "Name" in snapshot.columns:
        row = snapshot[snapshot["Name"].astype(str).str.upper() == token]

    if row.empty:
        return {"error": "无数据"}

    payload = row.iloc[0].to_dict()

    def _lookup(col_name: str):
        if col_name in payload:
            return payload.get(col_name)
        # 兼容大小写差异
        for k, v in payload.items():
            if str(k).lower() == str(col_name).lower():
                return v
        return None

    price = float(_lookup("Price") or 0)
    change_pct = float(_lookup("Change %") or 0)
    change = price * change_pct / 100 if price else 0.0

    return {
        "symbol": payload.get("Symbol") or symbol,
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "volume": float(_lookup("Volume") or 0),
        "technical": {
            "rsi_14": None,
            "macd": None,
            "macd_signal": None,
            "sma20": None,
            "sma50": None,
            "ema20": None,
            "ema50": None,
            "recommend_all": 0.0,
            "recommend_ma": 0.0,
            "recommend_other": 0.0,
        },
    }


def write_latest_snapshot(results: List[Dict[str, Any]], symbols: List[str]) -> None:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "stock_prices_latest.json"
    payload = {
        "source": "alpaca+sqlite",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbols": symbols,
        "results": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    """主函数"""
    # 解析命令行参数
    symbols = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_SYMBOLS
    
    print("📈 股票实时价格查询")
    print("=" * 50)
    print("数据来源: Alpaca Market Data + SQLite 技术指标")
    print(f"查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"查询股票: {', '.join(symbols)}")
    print("=" * 50)

    print("\n获取报价数据...\n")
    
    results = []
    snapshot = _load_market_snapshot(symbols=symbols)
    for symbol in symbols:
        print(f"  获取 {symbol}...", end=" ")
        result = get_quote(symbol, snapshot)
        if "error" in result:
            print(f"❌ {result['error']}")
        else:
            print("✓")
            results.append(result)
    
    if results:
        print("\n" + "=" * 50)
        print("📊 股票价格汇总")
        print("=" * 50)
        print(f"{'股票':<8} {'当前价格':>12} {'涨跌':>10} {'涨跌幅':>10}")
        print("-" * 50)
        
        for r in results:
            change_str = f"{r['change']:+.2f}" if r.get("change") is not None else "N/A"
            pct_str = f"{float(r['change_pct']):+.2f}%" if r.get("change_pct") is not None else "N/A"
            print(f"{r['symbol']:<8} ${r['price']:>10.2f} {change_str:>10} {pct_str:>10}")

    write_latest_snapshot(results, symbols)
    print("\n💾 已更新最新股价文件: skills/alpaca-live-trading/data/stock_prices_latest.json")


if __name__ == "__main__":
    main()
