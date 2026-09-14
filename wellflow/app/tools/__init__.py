"""Node1 调研工具集。

所有工具同时满足两个条件：
  1. 可独立调用（函数签名清晰）
  2. 兼容 LangChain Tool 协议（用 @tool 装饰器，agent 能直接绑定）

调用方：ResearchAgent（Node1 内），最多 2 轮 tool call。
"""

from wellflow.app.tools.web_search import web_search
from wellflow.app.tools.platform_trend import get_platform_trends
from wellflow.app.tools.competitor import get_competitor_snapshot

ALL_NODE1_TOOLS = [
    web_search,
    get_platform_trends,
    get_competitor_snapshot,
]

__all__ = [
    "web_search",
    "get_platform_trends",
    "get_competitor_snapshot",
    "ALL_NODE1_TOOLS",
]
