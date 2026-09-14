"""web_search 工具 — Bing 公开搜索（免 API Key）。

真实后端：Bing 公开搜索 HTML（用 httpx + BeautifulSoup 解析）
  - 完全免 Key、免注册、免银行卡
  - 支持 http_proxy_url 代理绕过网络限制

注：国内 IP 访问 Bing 可能返回相关性差的结果。
    配 http_proxy_url 走代理可解决（例如代理到境外）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.tools import tool


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
    from wellflow.app.config import settings
    return settings.http_proxy_url or None


def _bing_search(query: str, max_results: int) -> dict[str, Any]:
    """Bing 公开搜索 HTML。"""
    from bs4 import BeautifulSoup

    proxy = _get_proxy()
    with httpx.Client(timeout=12, follow_redirects=True, headers=_HEADERS, proxy=proxy) as client:
        resp = client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max_results + 3, "setlang": "zh-CN"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Bing returned {resp.status_code}")

    soup = BeautifulSoup(resp.text, "lxml")
    results: list[dict[str, Any]] = []
    for li in soup.select("li.b_algo")[:max_results]:
        a = li.select_one("h2 a")
        title = a.get_text(strip=True) if a else ""
        url = a.get("href", "") if a else ""
        # Bing 的跳转链接要解包
        if url.startswith("https://www.bing.com/ck/a?"):
            # 尝试从 u= 参数提取真实 URL
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            if "u" in params:
                url = params["u"][0]
        snippet_el = li.select_one(".b_caption p, .b_snippet p")
        snippet = snippet_el.get_text(strip=True)[:400] if snippet_el else ""
        results.append({"title": title[:100], "url": url, "snippet": snippet})

    return {"query": query, "results": results}


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网获取与商品相关的市场信息、消费趋势、竞品分析。

    Args:
        query: 搜索关键词，例如 "2025夏季棉麻连衣裙销售趋势"
        max_results: 返回结果数量，默认 5

    Returns:
        JSON 字符串，包含 results（title / url / snippet 列表）
    """
    max_results = max(1, min(max_results, 10))

    data = _bing_search(query, max_results)
    data["source"] = "bing_html"
    return json.dumps(data, ensure_ascii=False)
