#!/usr/bin/env python3
"""
查询 Polymarket 金融市场情绪指标

用法:
    python query_polymarket_sentiment.py             # 查询金融市场情绪
    python query_polymarket_sentiment.py --trending  # 查询热门市场
"""

import sys
import json
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any


class PolymarketClient:
    """Polymarket API 客户端"""
    
    BASE_URL = "https://gamma-api.polymarket.com"
    
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AI-Trader/1.0"
        })
    
    def fetch(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        """发送 GET 请求"""
        url = f"{self.BASE_URL}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise Exception(f"API 请求失败: {e}")
    
    def get_trending(self, limit: int = 10) -> List[Dict]:
        """获取热门市场"""
        return self.fetch("/events", {
            "limit": limit,
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false"
        })


def get_financial_sentiment() -> str:
    """
    获取金融市场实时情绪指标
    
    Returns:
        格式化的情绪指标字符串
    """
    client = PolymarketClient()
    
    # 定义要查询的分类 (tag_slug, 显示名称, limit)
    categories = [
        ("daily", "Finance Daily (每日金融)", 5),
        ("weekly", "Finance Weekly (每周金融)", 5),
        ("stocks", "Stocks (股票)", 15),
        ("earnings", "Earnings (财报)", 10),
        ("commodities", "Commodities (大宗商品)", 5),
    ]
    
    output_lines = [
        "📊 **Polymarket 金融市场实时情绪指标**",
        f"数据时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ""
    ]
    
    for tag_slug, category_name, limit in categories:
        try:
            events = client.fetch("/events", {
                "limit": limit,
                "closed": "false",
                "tag_slug": tag_slug,
                "order": "volume24hr",
                "ascending": "false"
            })
            
            if not events:
                continue
            
            output_lines.append(f"## {category_name}")
            
            for i, event in enumerate(events[:limit], 1):
                title = event.get("title", "Unknown")
                vol24 = event.get("volume24hr", 0)
                
                # 获取第一个市场的价格
                markets = event.get("markets", [])
                if markets:
                    m = markets[0]
                    prices = m.get("outcomePrices", [])
                    if isinstance(prices, str):
                        try:
                            prices = json.loads(prices)
                        except:
                            prices = []
                    
                    yes_pct = float(prices[0]) * 100 if prices else 0
                    output_lines.append(f"{i}. **{title}** | Yes: {yes_pct:.1f}% | 24h Vol: ${vol24:,.0f}")
                else:
                    output_lines.append(f"{i}. **{title}** | 24h Vol: ${vol24:,.0f}")
            
            output_lines.append("")
            
        except Exception as e:
            output_lines.append(f"## {category_name}")
            output_lines.append(f"  ⚠️ 获取失败: {e}")
            output_lines.append("")
    
    return "\n".join(output_lines)


def get_trending_markets(limit: int = 10) -> str:
    """
    获取热门预测市场
    
    Args:
        limit: 返回数量
        
    Returns:
        格式化的热门市场字符串
    """
    client = PolymarketClient()
    
    events = client.get_trending(limit=limit)
    
    output_lines = [
        "🔥 **Polymarket 热门市场**",
        f"数据时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"显示前 {len(events)} 个最活跃市场（按24h交易量排序）",
        ""
    ]
    
    for i, event in enumerate(events, 1):
        title = event.get("title", "Unknown")
        vol24 = event.get("volume24hr", 0)
        total_vol = event.get("volume", 0)
        
        output_lines.append(f"### {i}. {title}")
        output_lines.append(f"24h 交易量: ${vol24:,.0f} | 总交易量: ${total_vol:,.0f}")
        
        # 显示市场详情
        markets = event.get("markets", [])
        for m in markets[:3]:  # 最多显示3个子市场
            question = m.get("question", "")
            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except:
                    prices = []
            
            yes_pct = float(prices[0]) * 100 if prices else 0
            output_lines.append(f"  • {question}")
            output_lines.append(f"    Yes: {yes_pct:.1f}%")
        
        output_lines.append("")
    
    return "\n".join(output_lines)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="查询 Polymarket 市场情绪")
    parser.add_argument("--trending", action="store_true", help="查询热门市场")
    parser.add_argument("--limit", type=int, default=10, help="返回数量 (默认: 10)")
    args = parser.parse_args()
    
    print("=" * 60)
    print("Polymarket 预测市场情绪查询")
    print("=" * 60)
    
    try:
        if args.trending:
            result = get_trending_markets(args.limit)
        else:
            result = get_financial_sentiment()
        
        print(result)
        
    except Exception as e:
        print(f"\n❌ 查询失败: {e}")
        sys.exit(1)
    
    print("=" * 60)
    print("💡 提示: Polymarket 数据反映预测市场参与者的集体判断")
    print("   可用作市场情绪参考，但不代表实际结果")


if __name__ == "__main__":
    main()
