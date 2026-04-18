#!/usr/bin/env python3
"""
查询市场新闻和情绪 - 通过 AlphaVantage NEWS_SENTIMENT API

用法:
    python query_market_news.py                          # 查询最新金融市场新闻
    python query_market_news.py --tickers AAPL,NVDA      # 查询指定股票相关新闻
    python query_market_news.py --topics technology       # 查询指定主题新闻
    python query_market_news.py --tickers AAPL --topics earnings  # 组合过滤
"""

import sys
import json
import argparse
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

# 将 scripts 目录加入 Python 路径以导入 _config
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config import load_config, get_alphavantage_key

# 从 config.yaml 加载 AlphaVantage API Key
_config = load_config()
APIKEY = get_alphavantage_key(_config)
BASE_URL = "https://www.alphavantage.co/query"
DEFAULT_ALPHA_REQUEST_INTERVAL = 0.8  # 付费版 75 次/分钟

# 支持的新闻主题
SUPPORTED_TOPICS = [
    "blockchain", "earnings", "ipo", "mergers_and_acquisitions",
    "financial_markets", "economy_fiscal", "economy_monetary", "economy_macro",
    "energy_transportation", "finance", "life_sciences", "manufacturing",
    "real_estate", "retail_wholesale", "technology"
]


def _split_tickers(tickers: Optional[str]) -> List[str]:
    if not tickers:
        return []
    return [t.strip().upper() for t in tickers.split(",") if t.strip()]


def _extract_ticker_sentiment(article: Dict[str, Any], ticker: str) -> Optional[float]:
    ticker = ticker.upper()
    for item in article.get("ticker_sentiment", []):
        if str(item.get("ticker", "")).upper() == ticker:
            try:
                return float(item.get("ticker_sentiment_score"))
            except (TypeError, ValueError):
                return None
    return None


def fetch_news_per_ticker(
    tickers: List[str],
    per_ticker_limit: int = 5,
    topics: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    sort: str = "LATEST",
    request_interval: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    逐只股票查询新闻，返回按 ticker 分组结果（分析前置场景）。
    """
    grouped_results: List[Dict[str, Any]] = []
    for idx, ticker in enumerate(tickers):
        articles = fetch_news(
            tickers=ticker,
            topics=topics,
            time_from=time_from,
            time_to=time_to,
            sort=sort,
            limit=per_ticker_limit,
        )

        normalized_articles: List[Dict[str, Any]] = []
        overall_scores: List[float] = []
        ticker_scores: List[float] = []

        for a in articles:
            try:
                overall_score = float(a.get("overall_sentiment_score", 0))
                overall_scores.append(overall_score)
            except (TypeError, ValueError):
                overall_score = None

            ticker_score = _extract_ticker_sentiment(a, ticker)
            if ticker_score is not None:
                ticker_scores.append(ticker_score)

            normalized_articles.append(
                {
                    "title": a.get("title", ""),
                    "url": a.get("url", ""),
                    "source": a.get("source", ""),
                    "time_published": a.get("time_published", ""),
                    "time_published_readable": parse_time_published(a.get("time_published", "")),
                    "overall_sentiment_label": a.get("overall_sentiment_label", ""),
                    "overall_sentiment_score": overall_score,
                    "target_ticker_sentiment_score": ticker_score,
                    "target_ticker_sentiment_label": (
                        next(
                            (
                                s.get("ticker_sentiment_label", "")
                                for s in a.get("ticker_sentiment", [])
                                if str(s.get("ticker", "")).upper() == ticker
                            ),
                            "",
                        )
                    ),
                    "summary": a.get("summary", ""),
                }
            )

        grouped_results.append(
            {
                "ticker": ticker,
                "news_count": len(normalized_articles),
                "avg_overall_sentiment_score": (
                    round(sum(overall_scores) / len(overall_scores), 6) if overall_scores else None
                ),
                "avg_ticker_sentiment_score": (
                    round(sum(ticker_scores) / len(ticker_scores), 6) if ticker_scores else None
                ),
                "articles": normalized_articles,
            }
        )

        if request_interval > 0 and idx < len(tickers) - 1:
            time.sleep(request_interval)

    return grouped_results


def fetch_news(
    tickers: Optional[str] = None,
    topics: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    sort: str = "LATEST",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    从 AlphaVantage NEWS_SENTIMENT API 获取新闻

    Args:
        tickers: 股票代码，逗号分隔 (例: "AAPL" 或 "AAPL,NVDA,CRYPTO:BTC")
        topics: 新闻主题，逗号分隔 (例: "technology" 或 "technology,earnings")
        time_from: 起始时间，格式 YYYYMMDDTHHMM (例: "20260101T0000")
        time_to: 结束时间，格式 YYYYMMDDTHHMM
        sort: 排序方式 ("LATEST", "EARLIEST", "RELEVANCE")
        limit: 返回数量上限

    Returns:
        新闻文章列表
    """
    params = {
        "function": "NEWS_SENTIMENT",
        "apikey": APIKEY,
        "sort": sort,
        "limit": limit,
    }

    if tickers:
        params["tickers"] = tickers
    if topics:
        params["topics"] = topics
    if time_from:
        params["time_from"] = time_from
    if time_to:
        params["time_to"] = time_to

    try:
        response = requests.get(BASE_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "Error Message" in data:
            raise Exception(f"API 错误: {data['Error Message']}")
        if "Note" in data:
            raise Exception(f"API 调用限制: {data['Note']}")

        feed = data.get("feed", [])
        return feed[:limit]

    except requests.exceptions.Timeout:
        raise Exception("请求超时")
    except requests.exceptions.RequestException as e:
        raise Exception(f"请求失败: {e}")


def parse_time_published(time_str: str) -> str:
    """
    解析 AlphaVantage 时间格式为可读格式

    Args:
        time_str: AlphaVantage 时间字符串 (例: "20260205T143000")

    Returns:
        格式化的时间字符串
    """
    try:
        if "T" in time_str:
            date_part = time_str.split("T")[0]
            time_part = time_str.split("T")[1]
            if len(date_part) == 8:
                if len(time_part) >= 6:
                    dt = datetime.strptime(time_str[:15], "%Y%m%dT%H%M%S")
                elif len(time_part) >= 4:
                    dt = datetime.strptime(time_str[:13], "%Y%m%dT%H%M")
                else:
                    return time_str
                return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return time_str


def format_sentiment(score: float) -> str:
    """
    将情绪评分格式化为描述性文本

    Args:
        score: 情绪评分 (-1 到 1)

    Returns:
        情绪描述
    """
    if score >= 0.35:
        return f"强烈看涨 ({score:+.3f})"
    elif score >= 0.15:
        return f"看涨 ({score:+.3f})"
    elif score >= -0.15:
        return f"中性 ({score:+.3f})"
    elif score >= -0.35:
        return f"看跌 ({score:+.3f})"
    else:
        return f"强烈看跌 ({score:+.3f})"


def display_articles(articles: List[Dict[str, Any]], verbose: bool = False):
    """
    格式化显示新闻文章

    Args:
        articles: 文章列表
        verbose: 是否显示详细信息
    """
    if not articles:
        print("  (无匹配的新闻)")
        return

    for i, article in enumerate(articles, 1):
        title = article.get("title", "N/A")
        source = article.get("source", "N/A")
        time_published = parse_time_published(article.get("time_published", ""))
        summary = article.get("summary", "")
        overall_sentiment = article.get("overall_sentiment_score", 0)
        sentiment_label = article.get("overall_sentiment_label", "N/A")

        print(f"\n  {i}. {title}")
        print(f"     来源: {source} | 时间: {time_published}")

        # 情绪评分
        try:
            score = float(overall_sentiment)
            print(f"     情绪: {format_sentiment(score)}")
        except (ValueError, TypeError):
            print(f"     情绪: {sentiment_label}")

        # 摘要（截断到 200 字符）
        if summary:
            display_summary = summary[:200] + "..." if len(summary) > 200 else summary
            print(f"     摘要: {display_summary}")

        # 详细模式：显示个股情绪
        if verbose:
            ticker_sentiment = article.get("ticker_sentiment", [])
            if ticker_sentiment:
                print("     个股情绪:")
                for ts in ticker_sentiment[:5]:
                    ticker = ts.get("ticker", "N/A")
                    relevance = ts.get("relevance_score", "N/A")
                    t_score = ts.get("ticker_sentiment_score", "N/A")
                    t_label = ts.get("ticker_sentiment_label", "N/A")
                    print(f"       {ticker}: {t_label} (score={t_score}, relevance={relevance})")

            topics_list = article.get("topics", [])
            if topics_list:
                topics_str = ", ".join([t.get("topic", "") for t in topics_list])
                print(f"     主题: {topics_str}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="查询市场新闻和情绪 (AlphaVantage NEWS_SENTIMENT API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
支持的主题 (--topics):
  {', '.join(SUPPORTED_TOPICS)}

示例:
  python query_market_news.py --tickers AAPL
  python query_market_news.py --tickers NVDA,AMD --topics technology
  python query_market_news.py --topics earnings --limit 5
  python query_market_news.py --tickers CRYPTO:BTC --verbose
"""
    )
    parser.add_argument("--tickers", type=str, default=None,
                        help="股票代码，逗号分隔 (例: AAPL,NVDA,CRYPTO:BTC)")
    parser.add_argument("--topics", type=str, default=None,
                        help="新闻主题，逗号分隔 (例: technology,earnings)")
    parser.add_argument("--days", type=int, default=7,
                        help="查询最近 N 天的新闻 (默认: 7)")
    parser.add_argument("--limit", type=int, default=10,
                        help="返回数量 (默认: 10, 最大: 50)")
    parser.add_argument("--sort", type=str, default="LATEST",
                        choices=["LATEST", "EARLIEST", "RELEVANCE"],
                        help="排序方式 (默认: LATEST)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="显示详细信息（个股情绪、主题等）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 格式输出")
    parser.add_argument(
        "--per-ticker",
        action="store_true",
        help="按股票逐个查询并分组输出（分析前推荐模式）",
    )
    parser.add_argument(
        "--per-ticker-limit",
        type=int,
        default=5,
        help="逐股票模式下，每只股票返回条数（默认: 5）",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=DEFAULT_ALPHA_REQUEST_INTERVAL,
        help="逐股票模式下，每次 API 调用间隔秒数（默认: 0.8，约 75次/分钟）",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default="",
        help="将结果写入指定 JSON 文件（可选）",
    )
    args = parser.parse_args()

    print("📰 市场新闻与情绪查询")
    print("=" * 60)
    print(f"数据来源: AlphaVantage NEWS_SENTIMENT API")
    print(f"查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    filters = []
    if args.tickers:
        filters.append(f"股票: {args.tickers}")
    if args.topics:
        filters.append(f"主题: {args.topics}")
    if filters:
        print(f"过滤条件: {' | '.join(filters)}")
    print(f"排序: {args.sort} | 数量: {args.limit}")
    print("=" * 60)

    # 计算时间范围
    now = datetime.now()
    time_from = (now - timedelta(days=args.days)).strftime("%Y%m%dT0000")
    time_to = now.strftime("%Y%m%dT%H%M")

    try:
        print(f"\n获取最近 {args.days} 天的新闻...\n")
        if args.per_ticker:
            parsed_tickers = _split_tickers(args.tickers)
            if not parsed_tickers:
                raise Exception("逐股票模式必须提供 --tickers，例如 --tickers NVDA,MSFT,AAPL")

            per_ticker_limit = max(1, min(args.per_ticker_limit, 50))
            grouped = fetch_news_per_ticker(
                tickers=parsed_tickers,
                per_ticker_limit=per_ticker_limit,
                topics=args.topics,
                time_from=time_from,
                time_to=time_to,
                sort=args.sort,
                request_interval=max(args.request_interval, 0.0),
            )

            payload = {
                "mode": "per_ticker",
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "days": args.days,
                "sort": args.sort,
                "per_ticker_limit": per_ticker_limit,
                "tickers": parsed_tickers,
                "results": grouped,
            }

            if args.output_file:
                out_path = Path(args.output_file)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"💾 已写入: {out_path}")

            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                for item in grouped:
                    ticker = item["ticker"]
                    print(f"📌 {ticker} | 新闻数: {item['news_count']}")
                    if item["avg_overall_sentiment_score"] is not None:
                        print(f"  平均新闻情绪: {item['avg_overall_sentiment_score']:+.3f}")
                    if item["avg_ticker_sentiment_score"] is not None:
                        print(f"  平均个股情绪: {item['avg_ticker_sentiment_score']:+.3f}")
                    display_articles(item["articles"], verbose=args.verbose)
                    print()
        else:
            articles = fetch_news(
                tickers=args.tickers,
                topics=args.topics,
                time_from=time_from,
                time_to=time_to,
                sort=args.sort,
                limit=min(args.limit, 50),
            )
            if args.output_file:
                out_path = Path(args.output_file)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(articles, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"💾 已写入: {out_path}")

            if args.json:
                print(json.dumps(articles, indent=2, ensure_ascii=False))
            else:
                print(f"找到 {len(articles)} 篇新闻:")
                display_articles(articles, verbose=args.verbose)

    except Exception as e:
        print(f"\n❌ 查询失败: {e}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("💡 提示: AlphaVantage 免费版限制 25 次/天，如遇限制请稍后重试")
    print("   情绪评分范围: -1 (强烈看跌) 到 +1 (强烈看涨)")


if __name__ == "__main__":
    main()
