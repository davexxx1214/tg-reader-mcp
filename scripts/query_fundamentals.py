#!/usr/bin/env python3
"""
查询股票近一年关键财务数据 - 通过 AlphaVantage 基本面接口

用法:
    python query_fundamentals.py --tickers NVDA
    python query_fundamentals.py --tickers NVDA,MSFT --days 365 --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# 将 scripts 目录加入 Python 路径以导入 _config
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import get_alphavantage_key, load_config

BASE_URL = "https://www.alphavantage.co/query"
_config = load_config()
API_KEY = get_alphavantage_key(_config)
DEFAULT_ALPHA_REQUEST_INTERVAL = 0.8  # 付费版 75 次/分钟


def _to_float(value: Any) -> Optional[float]:
    if value in (None, "", "None", "null", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_div(n: Optional[float], d: Optional[float]) -> Optional[float]:
    if n is None or d in (None, 0):
        return None
    return n / d


def _parse_date(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _fetch_alpha_vantage(function_name: str, symbol: str, timeout: int = 30) -> Dict[str, Any]:
    params = {
        "function": function_name,
        "symbol": symbol,
        "apikey": API_KEY,
    }
    response = requests.get(BASE_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if "Error Message" in payload:
        raise RuntimeError(f"{function_name} 接口错误: {payload['Error Message']}")
    if "Note" in payload:
        raise RuntimeError(f"{function_name} 调用受限: {payload['Note']}")
    return payload


def _extract_overview(overview: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": overview.get("Symbol"),
        "name": overview.get("Name"),
        "sector": overview.get("Sector"),
        "industry": overview.get("Industry"),
        "market_cap": _to_float(overview.get("MarketCapitalization")),
        "pe_ratio": _to_float(overview.get("PERatio")),
        "peg_ratio": _to_float(overview.get("PEGRatio")),
        "price_to_book": _to_float(overview.get("PriceToBookRatio")),
        "eps_ttm": _to_float(overview.get("EPS")),
        "profit_margin": _to_float(overview.get("ProfitMargin")),
        "operating_margin_ttm": _to_float(overview.get("OperatingMarginTTM")),
        "roe_ttm": _to_float(overview.get("ReturnOnEquityTTM")),
        "roa_ttm": _to_float(overview.get("ReturnOnAssetsTTM")),
        "revenue_ttm": _to_float(overview.get("RevenueTTM")),
        "gross_profit_ttm": _to_float(overview.get("GrossProfitTTM")),
        "analyst_target_price": _to_float(overview.get("AnalystTargetPrice")),
    }


def _extract_recent_reports(reports: List[Dict[str, Any]], cutoff: datetime, max_items: int = 4) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    for item in reports:
        dt = _parse_date(item.get("fiscalDateEnding", ""))
        if dt is None or dt < cutoff:
            continue
        filtered.append(item)
    filtered.sort(key=lambda x: x.get("fiscalDateEnding", ""), reverse=True)
    return filtered[:max_items]


def _build_quarterly_metrics(
    income_reports: List[Dict[str, Any]],
    balance_reports: List[Dict[str, Any]],
    cashflow_reports: List[Dict[str, Any]],
    earnings_reports: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    by_date: Dict[str, Dict[str, Any]] = {}

    for row in income_reports:
        d = row.get("fiscalDateEnding")
        if not d:
            continue
        item = by_date.setdefault(d, {"fiscal_date_ending": d})
        revenue = _to_float(row.get("totalRevenue"))
        gross_profit = _to_float(row.get("grossProfit"))
        operating_income = _to_float(row.get("operatingIncome"))
        net_income = _to_float(row.get("netIncome"))
        item.update(
            {
                "revenue": revenue,
                "gross_profit": gross_profit,
                "operating_income": operating_income,
                "net_income": net_income,
                "gross_margin": _safe_div(gross_profit, revenue),
                "operating_margin": _safe_div(operating_income, revenue),
                "net_margin": _safe_div(net_income, revenue),
            }
        )

    for row in balance_reports:
        d = row.get("fiscalDateEnding")
        if not d:
            continue
        item = by_date.setdefault(d, {"fiscal_date_ending": d})
        assets = _to_float(row.get("totalAssets"))
        liabilities = _to_float(row.get("totalLiabilities"))
        equity = _to_float(row.get("totalShareholderEquity"))
        current_debt = _to_float(row.get("currentDebt"))
        long_term_debt = _to_float(row.get("longTermDebt"))
        total_debt = (current_debt or 0.0) + (long_term_debt or 0.0)
        item.update(
            {
                "total_assets": assets,
                "total_liabilities": liabilities,
                "total_shareholder_equity": equity,
                "current_debt": current_debt,
                "long_term_debt": long_term_debt,
                "debt_to_equity": _safe_div(total_debt, equity),
            }
        )

    for row in cashflow_reports:
        d = row.get("fiscalDateEnding")
        if not d:
            continue
        item = by_date.setdefault(d, {"fiscal_date_ending": d})
        operating_cf = _to_float(row.get("operatingCashflow"))
        capex = _to_float(row.get("capitalExpenditures"))
        free_cf = None
        if operating_cf is not None and capex is not None:
            free_cf = operating_cf + capex  # capex 通常为负数
        item.update(
            {
                "operating_cashflow": operating_cf,
                "capital_expenditures": capex,
                "free_cashflow": free_cf,
            }
        )

    for row in earnings_reports:
        d = row.get("fiscalDateEnding")
        if not d:
            continue
        item = by_date.setdefault(d, {"fiscal_date_ending": d})
        item.update(
            {
                "reported_eps": _to_float(row.get("reportedEPS")),
                "estimated_eps": _to_float(row.get("estimatedEPS")),
                "surprise_pct": _to_float(row.get("surprisePercentage")),
            }
        )

    merged = list(by_date.values())
    merged.sort(key=lambda x: x["fiscal_date_ending"], reverse=True)
    return merged


def fetch_fundamentals_for_symbol(
    symbol: str,
    days: int = 365,
    endpoint_request_interval: float = DEFAULT_ALPHA_REQUEST_INTERVAL,
) -> Dict[str, Any]:
    cutoff = datetime.now() - timedelta(days=max(days, 1))

    funcs = ["OVERVIEW", "INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW", "EARNINGS"]
    payloads: Dict[str, Dict[str, Any]] = {}
    for idx, fn in enumerate(funcs):
        payloads[fn] = _fetch_alpha_vantage(fn, symbol)
        if endpoint_request_interval > 0 and idx < len(funcs) - 1:
            time.sleep(endpoint_request_interval)

    overview = payloads["OVERVIEW"]
    income = payloads["INCOME_STATEMENT"]
    balance = payloads["BALANCE_SHEET"]
    cashflow = payloads["CASH_FLOW"]
    earnings = payloads["EARNINGS"]

    income_reports = _extract_recent_reports(income.get("quarterlyReports", []), cutoff)
    balance_reports = _extract_recent_reports(balance.get("quarterlyReports", []), cutoff)
    cashflow_reports = _extract_recent_reports(cashflow.get("quarterlyReports", []), cutoff)
    earnings_reports = _extract_recent_reports(earnings.get("quarterlyEarnings", []), cutoff)

    quarterly = _build_quarterly_metrics(
        income_reports=income_reports,
        balance_reports=balance_reports,
        cashflow_reports=cashflow_reports,
        earnings_reports=earnings_reports,
    )

    return {
        "symbol": symbol,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "lookback_days": days,
        "company_overview": _extract_overview(overview),
        "quarterly_key_financials": quarterly,
    }


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"${v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:+.2f}%"


def print_human_readable(results: List[Dict[str, Any]]) -> None:
    print("📊 AlphaVantage 近一年关键财务数据")
    print("=" * 72)
    for block in results:
        ov = block.get("company_overview", {})
        print(f"\n🏢 {block.get('symbol')} - {ov.get('name') or 'N/A'}")
        print(f"行业: {ov.get('sector') or 'N/A'} / {ov.get('industry') or 'N/A'}")
        print(
            f"市值: {_fmt_money(ov.get('market_cap'))} | PE: {ov.get('pe_ratio') or 'N/A'} | "
            f"ROE(TTM): {_fmt_pct(ov.get('roe_ttm'))}"
        )
        print(
            f"收入(TTM): {_fmt_money(ov.get('revenue_ttm'))} | "
            f"净利率: {_fmt_pct(ov.get('profit_margin'))} | EPS(TTM): {ov.get('eps_ttm') or 'N/A'}"
        )

        rows = block.get("quarterly_key_financials", [])
        if not rows:
            print("  近一年无季度财务数据")
            continue

        print("  最近季度关键指标:")
        for idx, row in enumerate(rows[:4], 1):
            print(
                f"  {idx}. {row.get('fiscal_date_ending')} | "
                f"Revenue {_fmt_money(row.get('revenue'))} | "
                f"NetIncome {_fmt_money(row.get('net_income'))} | "
                f"FCF {_fmt_money(row.get('free_cashflow'))} | "
                f"EPS {row.get('reported_eps') if row.get('reported_eps') is not None else 'N/A'}"
            )
    print("\n" + "=" * 72)
    print("💡 提示: AlphaVantage 免费版限制 25 次/天, 5 次/分钟")


def main() -> None:
    parser = argparse.ArgumentParser(description="查询股票近一年关键财务数据 (AlphaVantage)")
    parser.add_argument("--tickers", required=True, type=str, help="股票代码，逗号分隔，例如 NVDA,MSFT")
    parser.add_argument("--days", type=int, default=365, help="回看天数，默认 365")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--output-file", type=str, default="", help="保存结果到 JSON 文件")
    parser.add_argument(
        "--request-interval",
        type=float,
        default=DEFAULT_ALPHA_REQUEST_INTERVAL,
        help="每个股票之间的等待秒数（默认 0.8，约 75次/分钟）",
    )
    parser.add_argument(
        "--endpoint-request-interval",
        type=float,
        default=DEFAULT_ALPHA_REQUEST_INTERVAL,
        help="同一股票内各基本面接口调用间隔秒数（默认 0.8）",
    )
    args = parser.parse_args()

    tickers = [x.strip().upper() for x in args.tickers.split(",") if x.strip()]
    if not tickers:
        print("❌ --tickers 不能为空")
        raise SystemExit(1)

    all_results: List[Dict[str, Any]] = []
    for i, ticker in enumerate(tickers):
        try:
            print(f"🔄 获取 {ticker} 基本面数据...")
            payload = fetch_fundamentals_for_symbol(
                ticker,
                days=args.days,
                endpoint_request_interval=max(args.endpoint_request_interval, 0.0),
            )
            all_results.append(payload)
            print(f"✅ {ticker} 完成")
        except Exception as e:
            all_results.append({"symbol": ticker, "error": str(e)})
            print(f"❌ {ticker} 失败: {e}")

        if args.request_interval > 0 and i < len(tickers) - 1:
            time.sleep(args.request_interval)

    output = {
        "source": "AlphaVantage",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "lookback_days": args.days,
        "tickers": tickers,
        "count": len(all_results),
        "results": all_results,
    }

    if args.output_file:
        out_path = Path(args.output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 已写入: {out_path}")

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        valid_rows = [x for x in all_results if not x.get("error")]
        print_human_readable(valid_rows)


if __name__ == "__main__":
    main()
