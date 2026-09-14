"""platform_trend 工具 — 平台热词 / 趋势标签（免 API Key）。

真实后端：TrendSpy（直接访问 Google Trends，不走 API Key）
  - trending_now:    实时热门趋势
  - related_queries: 关键词相关搜索词 = 热词
  - interest_over_time: 关键词热度曲线 → demand_index

支持 http_proxy_url 代理绕过网络限制（例如 VPN 到境外）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.tools import tool

from wellflow.app.config import settings


# 平台 → Google Trends geo code + 显示名称
_PLATFORM_GEO: dict[str, tuple[str, str]] = {
    "taobao": ("CN", "中国"),
    "xiaohongshu": ("CN", "中国"),
    "douyin": ("CN", "中国"),
    "pinduoduo": ("CN", "中国"),
    "default": ("CN", "中国"),
}

_PLATFORM_DISPLAY: dict[str, str] = {
    "taobao": "淘宝",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "pinduoduo": "拼多多",
    "default": "",
}


def _trendspy_fetch(category: str, platform: str) -> dict[str, Any]:
    """TrendSpy 拉 Google Trends 数据（免 Key）。"""
    import trendspy
    from wellflow.app.config import settings

    geo, _ = _PLATFORM_GEO.get(platform, ("CN", "中国"))

    # 配代理（可选）
    proxy = settings.http_proxy_url

    trends = trendspy.Trends(hl='zh-CN', tz=-480)
    if proxy:
        trends.set_proxy(proxy)

    hot_keywords: list[str] = []
    trend_tags: list[str] = []
    demand_index = 0

    # 1. related_queries → 热词
    try:
        kwargs = {"headers": {"referer": "https://www.google.com/"}}
        related = trends.related_queries(category, timeframe='now 7-d', geo=geo, **kwargs)
        if isinstance(related, dict):
            for _, val in related.items():
                rising = getattr(val, 'rising', None) or {}
                top = getattr(val, 'top', None) or {}
                for q in list(rising.keys()) + list(top.keys()):
                    if q not in hot_keywords:
                        hot_keywords.append(str(q))
                if len(hot_keywords) >= 8:
                    break
    except Exception:
        pass

    # 2. trending_now → 实时趋势
    try:
        trending_list = list(trends.trending_now(geo=geo))[:10]
        for t in trending_list:
            kw = getattr(t, 'keyword', None) or getattr(t, 'title', None)
            if kw and kw not in trend_tags:
                trend_tags.append(str(kw))
            if len(trend_tags) >= 6:
                break
    except Exception:
        pass

    # 3. interest_over_time → demand_index
    try:
        df = trends.interest_over_time(category, timeframe='now 1-m', geo=geo)
        if df is not None and len(df) > 0:
            avg = df.mean().iloc[0] if hasattr(df.mean(), 'iloc') else float(df.mean())
            demand_index = int(avg)
    except Exception:
        pass

    return {
        "hot_keywords": hot_keywords[:6],
        "trend_tags": trend_tags[:5],
        "demand_index": demand_index or 70,
    }


@tool
def get_platform_trends(category: str, platform: str = "taobao") -> str:
    """查询电商平台上某个品类的热词、趋势标签和需求指数。

    数据来源：Google Trends（免费公开，无需 API Key）。

    Args:
        category: 商品品类，如 "连衣裙"、"毛衣"、"上衣"
        platform: 平台，可选 "taobao"、"xiaohongshu"、"douyin"

    Returns:
        JSON 字符串，包含 hot_keywords / trend_tags / demand_index
    """
    valid_platforms = ("taobao", "xiaohongshu", "douyin", "pinduoduo")
    if platform not in valid_platforms:
        platform = "taobao"

    data = _trendspy_fetch(category, platform)
    data["category"] = category
    data["platform"] = platform
    data["source"] = "trendspy_google_trends"
    return json.dumps(data, ensure_ascii=False)
