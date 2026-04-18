#!/usr/bin/env python3
"""
Telegram 新闻采集与标准化 — 将 TG 频道消息转化为与 AlphaVantage 相同 schema 的新闻数据

直接使用 Telethon 读取消息（复用 tg-reader-mcp 的 session），
无需经过 MCP 协议，可在 pipeline 或 cron job 中独立运行。

用法:
    # 作为模块导入（pipeline 集成）
    from query_tg_news import fetch_tg_news, merge_news_sources

    # 独立运行测试
    python query_tg_news.py --channels jinshishuju_bot --limit 30
    python query_tg_news.py --channels jinshishuju_bot --tickers USO,GLD --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

try:
    from telethon import TelegramClient
    from telethon.tl.types import Message
except ImportError:
    print("Install telethon first: pip install telethon", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# ── Telegram credentials (same as server.py) ──

TG_API_ID = os.getenv("TG_API_ID", "94575")
TG_API_HASH = os.getenv("TG_API_HASH", "a3406de8d171bb422bb6ddf3bbd800e2")

DEFAULT_TICKER_MAP_PATH = SCRIPT_DIR / "tg_ticker_map.yaml"

# ── Sentiment keyword dictionaries ──

_BULLISH_KEYWORDS: List[Tuple[str, float]] = [
    # strong bullish (0.3 – 0.4)
    ("大幅上涨", 0.40), ("暴涨", 0.40), ("强劲增长", 0.35), ("创新高", 0.35),
    ("大涨", 0.35), ("飙升", 0.35), ("井喷", 0.35),
    # moderate bullish (0.15 – 0.29)
    ("上涨", 0.20), ("走高", 0.20), ("反弹", 0.20), ("回升", 0.20),
    ("恢复", 0.18), ("增长", 0.18), ("扩张", 0.18), ("利好", 0.25),
    ("看涨", 0.25), ("看多", 0.25), ("买入", 0.20), ("突破", 0.22),
    ("复苏", 0.20), ("改善", 0.18), ("超预期", 0.25), ("乐观", 0.20),
    ("提振", 0.18), ("刺激", 0.15), ("加速", 0.18), ("回暖", 0.18),
    ("好于预期", 0.22), ("恢复出口", 0.20), ("开放", 0.15),
]

_BEARISH_KEYWORDS: List[Tuple[str, float]] = [
    # strong bearish (-0.3 – -0.4)
    ("暴跌", -0.40), ("大幅下跌", -0.40), ("崩盘", -0.40), ("暴雷", -0.35),
    ("大跌", -0.35), ("重挫", -0.35), ("跳水", -0.35),
    # moderate bearish (-0.15 – -0.29)
    ("下跌", -0.20), ("走低", -0.20), ("下滑", -0.20), ("回落", -0.18),
    ("衰退", -0.25), ("萎缩", -0.22), ("利空", -0.25), ("看跌", -0.25),
    ("看空", -0.25), ("卖出", -0.20), ("减持", -0.18), ("抛售", -0.25),
    ("下行", -0.18), ("恶化", -0.22), ("低于预期", -0.22), ("悲观", -0.20),
    ("封锁", -0.18), ("制裁", -0.20), ("禁令", -0.18), ("战争", -0.22),
    ("冲突", -0.18), ("紧张", -0.15), ("威胁", -0.15), ("打击", -0.18),
    ("枪击", -0.15), ("袭击", -0.20), ("轰炸", -0.22), ("中断", -0.18),
]

_SENTIMENT_LABEL_MAP = [
    (-0.35, "Bearish"),
    (-0.15, "Somewhat-Bearish"),
    (0.15, "Neutral"),
    (0.35, "Somewhat-Bullish"),
    (float("inf"), "Bullish"),
]

# 金十 bot 星级 → 重要性权重
_STAR_WEIGHT = {0: 0.6, 1: 0.8, 2: 1.0}


# ═══════════════════════════════════════════════════════════
# Ticker map loading
# ═══════════════════════════════════════════════════════════

def load_ticker_map(path: Path = DEFAULT_TICKER_MAP_PATH) -> Dict[str, Dict]:
    if yaml is None:
        raise ImportError("pyyaml is required: pip install pyyaml")
    if not path.exists():
        raise FileNotFoundError(f"Ticker map not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _build_keyword_index(
    ticker_map: Dict[str, Dict],
) -> List[Tuple[str, List[str]]]:
    """Pre-sort keywords longest-first for greedy matching."""
    pairs: List[Tuple[str, List[str]]] = []
    for sector in ticker_map.values():
        if not isinstance(sector, dict):
            continue
        tickers = sector.get("tickers", [])
        keywords = sector.get("keywords", [])
        if not tickers or not keywords:
            continue
        for kw in keywords:
            pairs.append((str(kw), [str(t) for t in tickers]))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


def match_tickers(
    text: str,
    keyword_index: List[Tuple[str, List[str]]],
    filter_tickers: Optional[Set[str]] = None,
) -> List[str]:
    """Return de-duplicated list of tickers matched from *text*."""
    matched: dict[str, None] = {}
    text_lower = text.lower()
    for kw, tickers in keyword_index:
        if kw.lower() in text_lower:
            for t in tickers:
                if filter_tickers is None or t.upper() in filter_tickers:
                    matched[t.upper()] = None
    return list(matched.keys())


# ═══════════════════════════════════════════════════════════
# Sentiment estimation (keyword-based)
# ═══════════════════════════════════════════════════════════

def _count_stars(text: str) -> int:
    m = re.search(r"【(★+)】", text)
    return len(m.group(1)) if m else 0


def estimate_sentiment(text: str) -> float:
    """Keyword-based Chinese financial news sentiment, range roughly [-0.4, 0.4]."""
    scores: List[float] = []
    for kw, score in _BULLISH_KEYWORDS:
        if kw in text:
            scores.append(score)
    for kw, score in _BEARISH_KEYWORDS:
        if kw in text:
            scores.append(score)
    if not scores:
        return 0.0
    raw = sum(scores) / len(scores)
    star_count = _count_stars(text)
    weight = _STAR_WEIGHT.get(star_count, 1.0)
    return max(-1.0, min(1.0, raw * weight))


def _sentiment_label(score: float) -> str:
    for threshold, label in _SENTIMENT_LABEL_MAP:
        if score <= threshold:
            return label
    return "Neutral"


# ═══════════════════════════════════════════════════════════
# Telethon session management
# ═══════════════════════════════════════════════════════════

def _resolve_session_path(session_path: Optional[str] = None) -> str:
    """Resolve session path from argument, config, or env."""
    if session_path:
        p = Path(session_path).expanduser().resolve()
    else:
        env = os.getenv("TG_SESSION_PATH", "")
        if env:
            p = Path(env).expanduser().resolve()
        else:
            p = Path(__file__).resolve().parent.parent / "tg_session.session"
    if p.suffix == ".session":
        p = p.with_suffix("")
    return str(p)


async def _read_channel_messages(
    session_path: str,
    channel: str,
    limit: int = 50,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Read messages from a TG channel/DM via Telethon, return raw dicts."""
    client = TelegramClient(session_path, int(TG_API_ID), TG_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session not authorized. Run login.py first.")

        entity = await client.get_entity(channel)
        iter_kwargs: Dict[str, Any] = {"limit": limit}
        if since:
            iter_kwargs["limit"] = min(limit * 3, 500)

        messages: List[Dict[str, Any]] = []
        async for msg in client.iter_messages(entity, **iter_kwargs):
            if not (isinstance(msg, Message) and msg.text):
                continue
            if since and msg.date.replace(tzinfo=timezone.utc) < since.replace(tzinfo=timezone.utc):
                break
            messages.append({
                "id": msg.id,
                "date": msg.date,
                "text": msg.text,
            })
            if since and len(messages) >= limit:
                break

        return messages
    finally:
        await client.disconnect()


# ═══════════════════════════════════════════════════════════
# Normalize TG messages to AV-compatible article schema
# ═══════════════════════════════════════════════════════════

def _to_av_time(dt: datetime) -> str:
    """Convert datetime to AlphaVantage format: YYYYMMDDTHHMMSS"""
    utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt
    return utc.strftime("%Y%m%dT%H%M%S")


def _to_readable_time(dt: datetime) -> str:
    utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt
    return utc.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_tg_article(
    msg: Dict[str, Any],
    ticker: str,
    channel_name: str,
    tg_weight: float = 0.8,
) -> Dict[str, Any]:
    """Convert one TG message into an AV-compatible article dict."""
    text = msg["text"]
    sentiment = estimate_sentiment(text)
    weighted_sentiment = sentiment * tg_weight
    title = text[:80].replace("\n", " ")
    summary = text[:500]

    return {
        "title": title,
        "url": "",
        "source": f"telegram:{channel_name}",
        "time_published": _to_av_time(msg["date"]),
        "time_published_readable": _to_readable_time(msg["date"]),
        "overall_sentiment_label": _sentiment_label(weighted_sentiment),
        "overall_sentiment_score": round(weighted_sentiment, 6),
        "target_ticker_sentiment_score": round(weighted_sentiment, 6),
        "target_ticker_sentiment_label": _sentiment_label(weighted_sentiment),
        "summary": summary,
    }


# ═══════════════════════════════════════════════════════════
# Main fetch function
# ═══════════════════════════════════════════════════════════

def fetch_tg_news(
    tickers: List[str],
    channels: Optional[List[Dict[str, Any]]] = None,
    ticker_map_path: Optional[Path] = None,
    session_path: Optional[str] = None,
    tg_weight: float = 0.8,
    default_limit: int = 50,
    max_articles_per_ticker: int = 50,
) -> List[Dict[str, Any]]:
    """
    Fetch TG news and return grouped results matching fetch_news_per_ticker schema.

    Args:
        tickers: list of tickers to match against (e.g. ["USO", "GLD", "SPY"])
        channels: list of channel configs [{"name": "...", "limit": N}, ...]
        ticker_map_path: path to tg_ticker_map.yaml
        session_path: path to .session file
        tg_weight: weight multiplier for TG sentiment scores (0-1)
        default_limit: default message limit per channel
        max_articles_per_ticker: max articles kept per ticker (default 50)

    Returns:
        List of dicts matching fetch_news_per_ticker output schema
    """
    if channels is None:
        channels = [{"name": "jinshishuju_bot", "limit": default_limit}]

    resolved_session = _resolve_session_path(session_path)
    map_path = ticker_map_path or DEFAULT_TICKER_MAP_PATH
    ticker_map = load_ticker_map(map_path)
    keyword_index = _build_keyword_index(ticker_map)
    filter_set = {t.upper() for t in tickers}

    all_messages: List[Tuple[str, Dict[str, Any]]] = []
    for ch_cfg in channels:
        ch_name = ch_cfg.get("name", "")
        ch_limit = ch_cfg.get("limit", default_limit)
        if not ch_name:
            continue
        try:
            msgs = asyncio.run(_read_channel_messages(
                session_path=resolved_session,
                channel=ch_name,
                limit=ch_limit,
            ))
            for m in msgs:
                all_messages.append((ch_name, m))
        except Exception as e:
            print(f"[tg-news] Warning: failed to read {ch_name}: {e}", file=sys.stderr)

    # Group messages by matched ticker
    ticker_articles: Dict[str, List[Dict[str, Any]]] = {t.upper(): [] for t in tickers}

    for ch_name, msg in all_messages:
        matched = match_tickers(msg["text"], keyword_index, filter_set)
        if not matched:
            continue
        for ticker in matched:
            article = _normalize_tg_article(msg, ticker, ch_name, tg_weight)
            ticker_articles[ticker].append(article)

    grouped: List[Dict[str, Any]] = []
    for ticker in tickers:
        t = ticker.upper()
        articles = ticker_articles.get(t, [])[:max_articles_per_ticker]
        overall_scores = [a["overall_sentiment_score"] for a in articles if a["overall_sentiment_score"] is not None]
        ticker_scores = [a["target_ticker_sentiment_score"] for a in articles if a["target_ticker_sentiment_score"] is not None]

        grouped.append({
            "ticker": t,
            "news_count": len(articles),
            "avg_overall_sentiment_score": (
                round(sum(overall_scores) / len(overall_scores), 6) if overall_scores else None
            ),
            "avg_ticker_sentiment_score": (
                round(sum(ticker_scores) / len(ticker_scores), 6) if ticker_scores else None
            ),
            "articles": articles,
        })

    return grouped


# ═══════════════════════════════════════════════════════════
# Merge AV + TG news sources
# ═══════════════════════════════════════════════════════════

def merge_news_sources(
    av_news: List[Dict[str, Any]],
    tg_news: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Merge AlphaVantage and Telegram news by ticker.
    TG articles are appended to AV articles for the same ticker.
    Averages are recalculated over the combined article set.
    """
    av_map: Dict[str, Dict[str, Any]] = {}
    for item in av_news:
        av_map[str(item.get("ticker", "")).upper()] = item

    tg_map: Dict[str, Dict[str, Any]] = {}
    for item in tg_news:
        t = str(item.get("ticker", "")).upper()
        if item.get("articles"):
            tg_map[t] = item

    all_tickers_ordered: List[str] = []
    seen: set = set()
    for item in av_news:
        t = str(item.get("ticker", "")).upper()
        if t not in seen:
            all_tickers_ordered.append(t)
            seen.add(t)
    for item in tg_news:
        t = str(item.get("ticker", "")).upper()
        if t not in seen:
            all_tickers_ordered.append(t)
            seen.add(t)

    merged: List[Dict[str, Any]] = []
    for ticker in all_tickers_ordered:
        av_item = av_map.get(ticker, {})
        tg_item = tg_map.get(ticker, {})

        av_articles = av_item.get("articles", []) if av_item else []
        tg_articles = tg_item.get("articles", []) if tg_item else []
        combined = av_articles + tg_articles

        if not combined:
            merged.append(av_item if av_item else {"ticker": ticker, "news_count": 0, "avg_overall_sentiment_score": None, "avg_ticker_sentiment_score": None, "articles": []})
            continue

        overall_scores = [
            a["overall_sentiment_score"]
            for a in combined
            if a.get("overall_sentiment_score") is not None
        ]
        ticker_scores = [
            a["target_ticker_sentiment_score"]
            for a in combined
            if a.get("target_ticker_sentiment_score") is not None
        ]

        merged.append({
            "ticker": ticker,
            "news_count": len(combined),
            "avg_overall_sentiment_score": (
                round(sum(overall_scores) / len(overall_scores), 6) if overall_scores else None
            ),
            "avg_ticker_sentiment_score": (
                round(sum(ticker_scores) / len(ticker_scores), 6) if ticker_scores else None
            ),
            "articles": combined,
        })

    return merged


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="从 Telegram 频道采集财经新闻并标准化为 AlphaVantage schema",
    )
    parser.add_argument("--channels", type=str, default="jinshishuju_bot",
                        help="TG 频道/用户名，逗号分隔 (默认: jinshishuju_bot)")
    parser.add_argument("--limit", type=int, default=50,
                        help="每个频道拉取条数 (默认: 50)")
    parser.add_argument("--tickers", type=str, default="",
                        help="仅匹配指定 ticker，逗号分隔 (默认: 匹配所有)")
    parser.add_argument("--session-path", type=str, default="",
                        help="Telethon session 文件路径")
    parser.add_argument("--ticker-map", type=str, default="",
                        help="ticker_map.yaml 路径")
    parser.add_argument("--tg-weight", type=float, default=0.8,
                        help="TG 情绪分权重系数 (默认: 0.8)")
    parser.add_argument("--json", action="store_true",
                        help="JSON 格式输出")
    parser.add_argument("--output-file", type=str, default="",
                        help="输出到文件")
    args = parser.parse_args()

    channel_names = [c.strip() for c in args.channels.split(",") if c.strip()]
    channels = [{"name": n, "limit": args.limit} for n in channel_names]

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        map_path = Path(args.ticker_map) if args.ticker_map else DEFAULT_TICKER_MAP_PATH
        tmap = load_ticker_map(map_path)
        all_t: set = set()
        for sector in tmap.values():
            if isinstance(sector, dict):
                for t in sector.get("tickers", []):
                    all_t.add(str(t).upper())
        tickers = sorted(all_t)

    session = args.session_path or None
    map_path = Path(args.ticker_map) if args.ticker_map else None

    print(f"📡 Telegram 新闻采集")
    print(f"  频道: {channel_names}")
    print(f"  Ticker 数: {len(tickers)}")
    print(f"  权重: {args.tg_weight}")
    print("=" * 50)

    results = fetch_tg_news(
        tickers=tickers,
        channels=channels,
        ticker_map_path=map_path,
        session_path=session,
        tg_weight=args.tg_weight,
        default_limit=args.limit,
    )

    with_articles = [r for r in results if r.get("articles")]
    print(f"\n匹配到 {len(with_articles)} 个 ticker 有 TG 新闻:")
    for item in with_articles:
        avg = item.get("avg_overall_sentiment_score")
        avg_str = f"{avg:+.3f}" if avg is not None else "N/A"
        print(f"  {item['ticker']:10s} | {item['news_count']:3d} 条 | 平均情绪: {avg_str}")

    payload = {
        "source": "telegram",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channels": channel_names,
        "tg_weight": args.tg_weight,
        "results": results,
    }

    if args.output_file:
        out = Path(args.output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"\n💾 已写入: {out}")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
