"""competitor 工具 — 竞品快照（免 API Key）。

真实后端：
  1. Bing 公开搜索找竞品落地页 URL
  2. httpx + trafilatura 清洗页面正文
  3. 正则提取价格/品牌

全部公开后端，零 Key。支持 http_proxy_url 代理绕过网络限制。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from langchain_core.tools import tool

from wellflow.app.config import settings


_PLATFORM_KEYWORD_MAP = {
    "taobao": "淘宝",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "pinduoduo": "拼多多",
    "default": "",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _get_proxy() -> str | None:
    """返回 httpx 能用的 proxy URL。httpx >= 0.28 用 proxy=（单数）。"""
    return settings.http_proxy_url or None


def _bing_search_urls(query: str, count: int) -> list[str]:
    """Bing 公开搜索，提取结果 URL。"""
    from bs4 import BeautifulSoup
    import urllib.parse

    proxy = _get_proxy()
    with httpx.Client(timeout=10, follow_redirects=True, headers=_HEADERS, proxy=proxy) as client:
        resp = client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": count + 3, "setlang": "zh-CN"},
        )
        if resp.status_code != 200:
            return []

    soup = BeautifulSoup(resp.text, "lxml")
    urls = []
    for li in soup.select("li.b_algo")[:count + 2]:
        a = li.select_one("h2 a")
        if not a:
            continue
        url = a.get("href", "")
        # 解包 Bing 跳转
        if url.startswith("https://www.bing.com/ck/a?"):
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            if "u" in qs:
                url = qs["u"][0]
        if url.startswith("http"):
            urls.append(url)
    return urls[:count]


def _fetch_and_clean(url: str, timeout: float = 8.0) -> str:
    """httpx 抓页面 + trafilatura 清洗。"""
    import trafilatura
    proxy = _get_proxy()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=_HEADERS, proxy=proxy) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return ""
            html = resp.text
    except Exception:
        return ""

    return trafilatura.extract(
        html, include_links=False, include_comments=False, favor_precision=True,
    ) or ""


def _extract_price(text: str) -> str:
    m = re.search(r"[¥￥]\s*(\d{2,6})(?:\s*[~到\-至]\s*[¥￥]?\s*(\d{2,6}))?", text)
    if m:
        low, high = m.group(1), m.group(2)
        return f"¥{low}" if not high else f"¥{low} ~ ¥{high}"
    return ""


def _extract_brand(text: str) -> str:
    common = ["优衣库", "无印良品", "ZARA", "UR", "H&M", "GAP", "Only", "乐町", "茵曼", "太平鸟"]
    for b in common:
        if b in text[:500]:
            return b
    return ""


def _real_competitors(category: str, platform: str, count: int) -> dict[str, Any]:
    platform_cn = _PLATFORM_KEYWORD_MAP.get(platform, "")
    query = f"{platform_cn} {category} 热销 价格 品牌 电商" if platform_cn else f"{category} 热销 价格 品牌"

    urls = _bing_search_urls(query, count + 2)
    if not urls:
        raise RuntimeError("Bing search returned no URLs")

    competitors = []
    for url in urls[:count]:
        text = _fetch_and_clean(url)
        competitors.append({
            "brand": _extract_brand(text),
            "sample_title": f"[待用户确认] {category}",
            "price_range": _extract_price(text) or "未知（需用户确认）",
            "estimated_monthly_sales": None,
            "selling_points": text[:200] if text else "",
            "source_url": url,
        })

    return {"category": category, "platform": platform, "competitors": competitors}


@tool
def get_competitor_snapshot(category: str, platform: str = "taobao", count: int = 3) -> str:
    """查询某平台上某品类的主要竞品快照（品牌、价格段、卖点）。

    Args:
        category: 商品品类，如 "连衣裙"、"毛衣"、"上衣"
        platform: 平台，可选 "taobao"、"xiaohongshu"、"douyin"
        count: 竞品数量，默认 3，最大 5

    Returns:
        JSON 字符串，包含 competitors 列表
    """
    count = max(1, min(count, 5))

    data = _real_competitors(category, platform, count)
    data["source"] = "bing+trafilatura"
    return json.dumps(data, ensure_ascii=False)
