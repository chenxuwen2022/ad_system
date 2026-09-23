"""意图分类器 —— 全 LLM 驱动，无关键词硬编码、无 UI 旁路。

职责：把用户自然语言 + 当前任务状态 → 8 类意图之一 + refine_target。

唯一入口：classify()
唯一实现：_classify_via_llm()
唯一兜底：LLM 异常 / JSON 异常 / 非白名单 → chat_outside

模型固定：deepseek-v4-flash，通过 get_llm_client() 直接调用，不进入模型池。
"""

from __future__ import annotations

import json
import re

from wellflow.app.config import settings
from typing import Any, Literal
from wellflow.app.prompt.registry import get_active_prompt


# ---------------------------------------------------------------------------
# 工具：把 refine 指令归一化（用于"本轮 == 上一轮完全重复"检测）
# ---------------------------------------------------------------------------

_FULL_WIDTH_PUNCT = {
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":",
    "（": "(", "）": ")", "【": "[", "】": "]", "“": '"', "”": '"',
    "‘": "'", "’": "'", "—": "-", "～": "~", "、": ",",
}


def normalize_instruction(text: str) -> str:
    """归一化 refine 指令 —— 用于检测「本轮是否和上一轮完全重复」。

    只做不损失语义的规整：
    - 全角标点 → 半角
    - 去掉所有空白字符（空格/Tab/换行）
    - 去掉标点（中英文）
    - 英文 → 小写
    """
    if not text:
        return ""
    s = text.strip()
    for fw, hw in _FULL_WIDTH_PUNCT.items():
        s = s.replace(fw, hw)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s, flags=re.UNICODE)
    return s.lower()


# ---------------------------------------------------------------------------
# graph_state → 产物摘要（方案 B：精选字段，≤200 字/段）
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    # 尽量切到最近的标点
    cut_at = text.rfind("。", limit - 30, limit)
    if cut_at < limit // 2:
        cut_at = limit
    return text[:cut_at] + "…"


def summarize_graph_state(graph_state: dict[str, Any] | None) -> str:
    """从 LangGraph checkpoint state 抽一份给 LLM 意图分类用的产物摘要。

    目标：让 LLM 知道每层产物"长什么样"（有几套方案、每套叫什么主题、
    node1 报告有没有被锁定等），才能精准判意图和 refine_target。
    控制 token 成本：每层 ≤ 200 字，总共 ≤ 1.5k token。
    """
    if not isinstance(graph_state, dict):
        return ""

    lines: list[str] = []

    # ---- node1 ----
    n1 = graph_state.get("node1") or {}
    report = (n1.get("product_insight") or "").strip()
    locked = bool(n1.get("report_locked"))
    if report:
        tag = "（已锁定，不可修改）" if locked else ""
        # 按段落切：每个 ## 标题算一段，保留前 4 个段落（品牌定位 + 卖点 + 人群 + 场景）
        paragraphs = [p.strip() for p in report.split("\n\n") if p.strip()]
        kept: list[str] = []
        budget = 600
        for p in paragraphs:
            if not kept:
                # 第一段（通常是一级标题 + 品牌定位）全留
                kept.append(p)
                budget -= len(p)
            elif budget > 0:
                kept.append(p)
                budget -= len(p)
            else:
                break
        report_brief = "\n".join(kept)
        # 硬封顶，兜底（超长段落或非 Markdown 纯文本）
        if len(report_brief) > 800:
            report_brief = report_brief[:800] + "…"
        lines.append(f"node1 商品报告{tag}:\n{report_brief}")

    # ---- node2 ----
    n2 = graph_state.get("node2") or {}
    schemes = n2.get("schemes") or []
    selected_indices = n2.get("selected_scheme_indices") or []
    if isinstance(schemes, list) and schemes:
        lines.append(f"node2 商拍方案（共 {len(schemes)} 套）:")
        for i, s in enumerate(schemes):
            if not isinstance(s, dict):
                continue
            name = s.get("scheme_name") or f"方案{i}"
            pos = s.get("positioning") or {}
            scene = s.get("scene") or {}
            mod = s.get("model") or {}
            selling = s.get("selling_points") or {}
            lighting = s.get("lighting") or {}
            theme = (pos.get("visual_theme") or "").strip()[:40]
            env = (scene.get("shooting_environment") or scene.get("usage_scenes") or "").strip()[:40]
            temperament = (mod.get("facial_temperament") or mod.get("overall_state") or "").strip()[:30]
            # 核心卖点取前 3 条（对意图分类最有价值）
            core_points = selling.get("core_selling_points") if isinstance(selling, dict) else None
            if isinstance(core_points, list):
                points_str = "/".join(str(p)[:20] for p in core_points[:3])
            else:
                points_str = ""
            light_style = ""
            if isinstance(lighting, dict):
                light_style = (lighting.get("lighting_design") or lighting.get("color_system") or "").strip()[:30]
            is_selected = "✓" if i in selected_indices else " "
            parts = [f"[{is_selected} {i}] {name}"]
            if theme:
                parts.append(f"视觉主题={theme}")
            if env:
                parts.append(f"场景={env}")
            if temperament:
                parts.append(f"模特={temperament}")
            if points_str:
                parts.append(f"卖点={points_str}")
            if light_style:
                parts.append(f"光影={light_style}")
            lines.append("  " + " | ".join(parts))
        if selected_indices:
            lines.append(f"已选方案索引: {selected_indices}")

    # ---- node3 ----
    n3 = graph_state.get("node3") or {}
    prompts = n3.get("generate_prompts") or []
    details = n3.get("prompts_detail") or []
    if isinstance(prompts, list) and prompts:
        total = len(prompts)
        # 只给前 2 条 prompt 开头，足够 LLM 判断用户说的"提示词"到底是什么
        preview = ""
        for p in prompts[:2]:
            if isinstance(p, str):
                preview += p[:80] + "；"
        preview = preview.rstrip("；")
        lines.append(f"node3 提示词（共 {total} 条）: {_truncate(preview, 160)}")
    elif isinstance(details, list) and details:
        lines.append(f"node3 提示词详情（共 {len(details)} 条，已生成）")

    # ---- node4 ----
    n4 = graph_state.get("node4") or {}
    outputs = n4.get("outputs") or []
    work = n4.get("work_items") or []
    if isinstance(outputs, list) and outputs:
        lines.append(f"node4 生图结果（共 {len(outputs)} 张）")
    elif isinstance(work, list) and work:
        done_count = sum(1 for w in work if isinstance(w, dict) and w.get("status") == "done")
        lines.append(f"node4 生图执行中：{done_count}/{len(work)} 完成")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 意图全集 —— 8 类
# ---------------------------------------------------------------------------
INTENT = Literal[
    "start_task",              # 无任务 → 开新任务
    "confirm_current",         # c1/c2/c3 通用确认（好的/继续/就这样/ok）
    "confirm_generation",      # 仅 c4：确认生图结果（满意/保存/结束）
    "redo_blocked",            # 已锁定报告或目标不明的重做请求
    "edit",                    # 微调/修改某层产物（配合 refine_target）
    "select_topics",           # 仅 c2：选方案（全选/用第1个）
    "skip_forward",            # 试图跳过工作流步骤 → 一律拦
    "chat_outside",            # 闲聊/非商拍 → 拦截
]

ALLOWED_INTENTS: set[str] = {
    "start_task",
    "confirm_current",
    "confirm_generation",
    "redo_blocked",
    "edit",
    "select_topics",
    "skip_forward",
    "chat_outside",
}



# ---------------------------------------------------------------------------
# 主入口 —— 图片阶段明确的重生短句确定路由，其余交给 LLM
# ---------------------------------------------------------------------------

def is_image_regeneration_request(message: str) -> bool:
    """Recognize short image rerun requests, never edits to upstream products."""
    import re
    text = re.sub(r"[\s，。！!？?、]", "", message).strip()
    return bool(re.fullmatch(
        r"(?:请|帮我|请帮我|我想|我要)?(?:"
        r"(?:重新|再次|再)(?:生成|生|出)(?:(?:几|一|两|二|三|四|五|六|[1-9]\d*)张)?(?:图片|图像|图)?"
        r"|再来(?:几|一|两|二|三|四|五|六|[1-9]\d*)张(?:图片|图)?"
        r"|重做(?:图片|图)|重跑node4)(?:吧|一下)?", text, re.IGNORECASE,
    ))


async def classify(
    message: str,
    *,
    has_task: bool = False,
    current_node: str | None = None,
    completed_mask: list[bool] | None = None,
    has_images: bool = False,
    product_description: str | None = None,
    # 前端 dispatch 层回传：用户显式指定要微调哪一步（node1/node2/node3）
    # 传给 LLM 作为"强引导"上下文，不是绕过 LLM 的旁路
    selected_finetuning_target: str | None = None,
    # chat.py 外部会先调 summarize_graph_state(graph_state) 生成精选摘要
    # 再通过这个参数喂进来，让 LLM 知道每层产物"长什么样"
    graph_state_brief: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    if (has_task and current_node == "c4"
            and selected_finetuning_target in (None, "node4")
            and is_image_regeneration_request(message)):
        return {"intent": "edit", "refine_target": "node4",
                "refine_instruction": message, "selected_indices": None,
                "blocked_step": None, "reasoning": "图片结果阶段明确要求再次生图"}
    try:
        result = await _classify_via_llm(
            message,
            has_task=has_task,
            current_node=current_node,
            completed_mask=completed_mask,
            has_images=has_images,
            product_description=product_description,
            selected_finetuning_target=selected_finetuning_target,
            graph_state_brief=graph_state_brief,
            task_id=task_id,
        )
        # refine_target 兜底：若 LLM 没给但明显有编辑意图，用当前 cX 对应 node
        if not result.get("refine_target") and result.get("intent") not in (
            "chat_outside", "start_task", "redo_blocked", "skip_forward",
            "confirm_current", "confirm_generation", "select_topics",
        ):
            _FALLBACK_NODE = {"c1": "node1", "c2": "node2", "c3": "node3", "c4": "node4"}
            result["refine_target"] = _FALLBACK_NODE.get(current_node)
        # 前端显式选了微调目标 → 用它覆盖（LLM 不一定能从短消息猜出）
        if selected_finetuning_target and result.get("intent") == "edit":
            result["refine_target"] = selected_finetuning_target

        # 🔴 c3 阶段「当前节点优先」兜底（代码层安全网）
        # LLM 仍可能按旧规则把 c3 的裸字段修改（"模特改为女性"）判成 node2
        # 当 current_node=c3 + refine_target=node2 + 用户消息不含任何 node2 产物名 → 覆盖为 node3
        # 只有用户明确说了"方案"/"商拍方案"/"第X套方案"，才保留 node2
        # 🔴 尊重前端显式选择：selected_finetuning_target 非空时不覆盖，用户主动选了 node2 就保持 node2
        _has_node2_product_name = any(k in message for k in ("方案", "商拍方案"))
        if (
            not selected_finetuning_target
            and current_node == "c3"
            and result.get("intent") == "edit"
            and result.get("refine_target") == "node2"
            and not _has_node2_product_name
        ):
            print(f"[intent] 🛡️ c3 当前节点优先兜底：LLM 判了 node2 但消息无'方案'字样 → 覆盖为 node3, msg={message[:50]}", flush=True)
            result["refine_target"] = "node3"

        return result
    except RuntimeError as exc:
        # RuntimeError 里明确区分"模型池全挂" vs 其他运行时错误：
        # ModelPool 在全部模型不可用时抛 RuntimeError("模型池全部不可用")
        msg = str(exc)
        if "模型池" in msg and "不可用" in msg:
            print(f"[intent] 🛑 模型池全部不可用 → intent 标记为 model_pool_unavailable: {msg}", flush=True)
            return {
                "intent": "model_pool_unavailable",
                "reasoning": msg,
                "refine_target": None,
                "refine_instruction": None,
                "selected_indices": None,
                "blocked_step": None,
            }
        # 其他 RuntimeError 仍按通用异常处理（见下面）
        print(f"[intent] ⚠️ RuntimeError（非模型池）→ fallback chat_outside: {msg}", flush=True)
        return {
            "intent": "chat_outside",
            "reasoning": f"LLM 调用失败: {msg}",
            "refine_target": None,
            "refine_instruction": None,
            "selected_indices": None,
            "blocked_step": None,
        }
    except Exception as exc:
        print(f"[intent] ❌ LLM 分类失败 → fallback chat_outside: {exc}", flush=True)
        return {
            "intent": "chat_outside",
            "reasoning": f"LLM 调用失败: {exc}",
            "refine_target": None,
            "refine_instruction": None,
            "selected_indices": None,
            "blocked_step": None,
        }


# ---------------------------------------------------------------------------
# LLM classifier —— 唯一的分类实现
# ---------------------------------------------------------------------------

async def _classify_via_llm(
    message: str, *, has_task: bool, current_node: str | None,
    completed_mask: list[bool] | None, has_images: bool,
    product_description: str | None,
    selected_finetuning_target: str | None = None,
    graph_state_brief: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:

    ctx_lines = [
        f"has_task={has_task}",
        f"current_node={current_node or '(无任务/初始态)'}",
        f"has_images={has_images}",
    ]
    if completed_mask:
        ctx_lines.append(f"completed_mask={completed_mask} (s1-s6)")
    if selected_finetuning_target:
        ctx_lines.append(
            f"⚠️ 用户显式指定了要微调的目标：refine_target={selected_finetuning_target}，"
            f"请务必尊重这个选择，把 intent 判为 edit 并使用该 refine_target"
        )

    # 产物摘要 —— 方案 B：每层精选字段，让 LLM 知道当前产物"长什么样"
    # （node1 有没有锁定、node2 有几套方案各叫什么、node3 提示词数量等）
    brief = (graph_state_brief or "").strip()
    if brief:
        ctx_lines.append("")
        ctx_lines.append("## 各层产物摘要（这是分类时判断意图和 refine_target 的关键依据）")
        ctx_lines.append(brief)

    user_prompt = (
        "## 当前任务状态\n"
        + "\n".join(ctx_lines)
        + f"\n\n## 用户消息\n{message or '(空消息)'}\n\n"
        "请返回意图分类 JSON。"
    )

    # 无论是否已有 task_id，意图识别始终使用固定模型，不参与模型池选择或降级。
    from wellflow.app.llm.factory import get_llm_client
    client = get_llm_client("text", model_override=settings.classifier_model)
    resp = await client.chat(
        system=get_active_prompt("intent_classifier"), user=user_prompt,
        response_format={"type": "json_object"}, temperature=0.2,
        reasoning_effort=settings.text_reasoning_effort,
    )
    used_model = settings.classifier_model

    try:
        data = json.loads(resp.content.strip())
    except Exception:
        return {
            "intent": "chat_outside",
            "reasoning": f"LLM 输出非 JSON ({used_model}): {resp.content[:200]}",
            "refine_target": None,
            "refine_instruction": None,
            "selected_indices": None,
            "blocked_step": None,
        }

    intent = data.get("intent", "")
    if intent not in ALLOWED_INTENTS:
        print(f"[intent] ⚠️ LLM 返回非白名单 intent='{intent}' → 降级 chat_outside", flush=True)
        intent = "chat_outside"

    # refine_instruction：LLM 可能会"精炼/改写"用户原话，导致丢细节
    # 兜底策略：如果 LLM 给的 instruction 比原消息短 40% 以上，直接用原消息
    _raw_instr = (data.get("refine_instruction") or "").strip()
    if _raw_instr and len(_raw_instr) >= len(message.strip()) * 0.6:
        refine_instruction = _raw_instr
    else:
        refine_instruction = message.strip()

    result = {
        "intent": intent,
        "reasoning": data.get("reasoning", ""),
        "refine_target": data.get("refine_target"),
        "refine_instruction": refine_instruction,
        "selected_indices": data.get("selected_indices"),
        "blocked_step": data.get("blocked_step"),
        "parsed_content": data.get("parsed_content"),  # 保留兼容旧消费者
    }
    print(f"[intent] {used_model} → {intent} refine_target={result['refine_target']}: "
          f"{result.get('reasoning', '')[:80]}", flush=True)
    return result
