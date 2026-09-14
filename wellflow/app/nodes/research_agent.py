"""Node 1：ResearchAgent（final.md 第 5.5 节）。

有界 Tool-Calling Agent，最多 3 步（= 2 轮 tool call + 1 次最终回答）。
搜索 / 平台趋势 / 竞品 / 素材库 四选一或组合调用。

架构：
  ┌───────────────────────────────────────────────┐
  │   ResearchAgent (Tool-Calling Agent)           │
  │                                                 │
  │   LLM ──tool_call──▶ ToolNode ──执行工具┐     │
  │        ◀────────────────────────────────┘     │
  │        (最多 2 轮)                              │
  │        ↓                                       │
  │   LLM 汇总结果 → 输出调研 JSON                  │
  └───────────────────────────────────────────────┘

依赖：langgraph、langchain-core。

注意：本模块不再做任何 mock 兜底或降级。tool agent 失败、LLM 返回
非法 JSON 等场景会直接抛异常，由上层错误处理分类（可恢复 / 临时 / 不可恢复）。
"""

from __future__ import annotations

import json
from typing import Any

from wellflow.app.llm.factory import get_llm_client
from wellflow.app.prompt.constant import RESEARCH_AGENT_SYSTEM_PROMPT


MAX_TOOL_CALL_ROUNDS = 2  # 有界 Agent：最多 2 轮 tool call 后强制出答案

# RESEARCH_AGENT_SYSTEM_PROMPT 要求 LLM 必须输出的 9 个字段
_RESEARCH_REQUIRED_FIELDS = {
    "market_positioning": str,
    "target_audience": list,
    "competitor_themes": list,
    "trend_signals": list,
    "selling_point_opportunities": list,
    "platform_suggestions": list,
    "sources": list,
    "confidence": (int, float),
    "evidence_insufficient": bool,
}


async def research(
    *,
    product_profile: dict[str, Any],
    platform: str,
    marketing_goal: str,
    brand_config: dict[str, Any] | None = None,
    max_tool_rounds: int = MAX_TOOL_CALL_ROUNDS,
    use_tools: bool = True,
) -> dict[str, Any]:
    """生成调研结论。

    use_tools=True → LangGraph Tool-Calling Agent 调 tools（主路径）
    use_tools=False → 纯 LLM 单次调用（仅靠世界知识写调研报告）

    任何异常直接抛出，不降级、不 mock。
    """
    if not use_tools:
        return await _run_single_llm(
            product_profile=product_profile,
            platform=platform,
            marketing_goal=marketing_goal,
            brand_config=brand_config,
        )

    return await _run_tool_agent(
        product_profile=product_profile,
        platform=platform,
        marketing_goal=marketing_goal,
        brand_config=brand_config,
        max_tool_rounds=max_tool_rounds,
    )


# ---------------------------------------------------------------------------
# 主路径：Tool-Calling Agent
# ---------------------------------------------------------------------------


async def _run_tool_agent(
    *,
    product_profile: dict[str, Any],
    platform: str,
    marketing_goal: str,
    brand_config: dict[str, Any] | None,
    max_tool_rounds: int,
) -> dict[str, Any]:
    """用 LangGraph Tool-Calling Agent 跑调研。"""
    from langgraph.prebuilt import create_react_agent
    from wellflow.app.tools import ALL_NODE1_TOOLS

    # 获取 base client
    client = get_llm_client("text")
    llm = client.langchain_compat() if hasattr(client, "langchain_compat") else client

    # 构建 system prompt + user message
    system_prompt = f"""{RESEARCH_AGENT_SYSTEM_PROMPT}

你可以使用以下工具收集信息：
- web_search: 搜索互联网上的市场数据、趋势报告
- get_platform_trends: 查询指定平台某品类的热词、趋势标签、需求指数
- get_competitor_snapshot: 查询竞品的品牌、价格、销量、卖点

请优先使用工具收集真实数据，再输出调研结论。
最多调用 {max_tool_rounds} 轮工具，之后必须给出最终 JSON 结论。
"""

    user_message = f"""
商品属性：{json.dumps(product_profile, ensure_ascii=False)}
平台：{platform}
营销目标：{marketing_goal}
品牌配置：{json.dumps(brand_config or {}, ensure_ascii=False)}

请先使用工具收集相关信息，然后输出严格 JSON。
"""

    # 创建 ReAct Agent
    agent = create_react_agent(
        model=llm,
        tools=ALL_NODE1_TOOLS,
        prompt=system_prompt,
    )

    messages = [{"role": "user", "content": user_message}]

    # 有界执行
    result = await agent.ainvoke(
        {"messages": messages},
        config={"recursion_limit": max_tool_rounds * 2 + 1},  # 每轮 = (tool_call + tool_result)
    )

    # 取最后一条 ai message 的 content
    final_content = ""
    for msg in reversed(result.get("messages", [])):
        if msg.type == "ai" and msg.content:
            final_content = msg.content
            break

    # 解析 + 校验 JSON（缺字段直接抛错）
    insight = _parse_and_validate_research_json(final_content)

    # 检查工具调用的 sources，写入 insight
    sources = _extract_tool_call_sources(result.get("messages", []))
    if sources:
        insight["sources"] = list(set(insight.get("sources", []) + sources))

    tool_called = any(msg.type == "tool" for msg in result.get("messages", []))
    if tool_called:
        prev_conf = insight.get("confidence", 0.5)
        insight["confidence"] = min(0.95, float(prev_conf) + 0.25)
        insight["evidence_insufficient"] = False

    return insight


# ---------------------------------------------------------------------------
# 纯 LLM 路径（use_tools=False 时主动选择，不是异常降级）
# ---------------------------------------------------------------------------


async def _run_single_llm(
    *,
    product_profile: dict[str, Any],
    platform: str,
    marketing_goal: str,
    brand_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """use_tools=False 时的纯 LLM 调研路径。"""
    client = get_llm_client("text")

    user_message = f"""
商品属性：{product_profile}
平台：{platform}
营销目标：{marketing_goal}
品牌配置：{brand_config or {}}

请输出严格的 JSON。
"""

    resp = await client.chat(
        system=RESEARCH_AGENT_SYSTEM_PROMPT,
        user=user_message,
        response_format={"type": "json_object"},
    )

    return _parse_and_validate_research_json(resp.content)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _parse_and_validate_research_json(content: str) -> dict[str, Any]:
    """从 LLM 输出提取 JSON + 校验必填字段。

    JSON 解析失败 / 缺必填字段 → 直接 raise，不静默补默认值。
    """
    # 先尝试直接 parse
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 尝试从 ```json ... ``` 或 { ... } 里提取（纯格式容错，不是 mock）
        import re
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise ValueError(
                f"ResearchAgent LLM 输出无法提取 JSON：{content[:500]}"
            )
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"ResearchAgent LLM 输出 JSON 解析失败：{exc}. "
                f"原始响应前 500 字符: {content[:500]}"
            ) from exc

    # 校验必填字段：缺字段 / 类型不符 → raise
    missing = [k for k in _RESEARCH_REQUIRED_FIELDS if k not in data]
    if missing:
        raise ValueError(
            f"ResearchAgent LLM 输出缺必填字段：{missing}. "
            f"实际输出字段: {list(data.keys())}"
        )

    for k, expected_type in _RESEARCH_REQUIRED_FIELDS.items():
        val = data[k]
        if not isinstance(val, expected_type):
            raise TypeError(
                f"ResearchAgent LLM 字段类型错误：{k} 应为 {expected_type.__name__}，"
                f"实际为 {type(val).__name__} (value={val})"
            )

    return data


def _extract_tool_call_sources(messages: list) -> list[str]:
    """从 agent 的 message 历史里提取工具调用的来源。"""
    sources = []
    for msg in messages:
        if msg.type == "tool":
            try:
                payload = json.loads(msg.content)
                if isinstance(payload, dict) and "source" in payload:
                    sources.append(f"{msg.name}: {payload['source']}")
            except (json.JSONDecodeError, AttributeError):
                pass
    return sources
