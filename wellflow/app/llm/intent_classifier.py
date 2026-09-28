"""Classify user intent, target product and operation independently.

Whole-product regeneration uses the original generator. Incremental edits use
refinement. Explicit short commands are deterministic; other wording uses the
published classifier prompt plus the execution protocol.
"""

from __future__ import annotations

from wellflow.app.logging import log_message

from wellflow.app.newapi.observability import business_operation, event

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

    originals = graph_state.get("initial_schemes") or []
    if originals:
        lines.append("首次商拍方案（scheme_source=initial，与当前列表独立编号）:")
        for i, scheme in enumerate(originals):
            lines.append(f"  最初方案{i + 1}（selected_indices={i}）: {scheme.get('scheme_name', '')} | "
                         + str(scheme.get("report_text", ""))[:180])
    else:
        lines.append("首次方案尚未加载；明确最初方案编号可返回 scheme_source=initial，由执行层查历史核实。")

    # ---- node2 ----
    n2 = graph_state.get("node2") or {}
    schemes = n2.get("schemes") or []
    selected_indices = n2.get("selected_scheme_indices") or []
    if isinstance(schemes, list) and schemes:
        lines.append(f"node2 商拍方案（共 {len(schemes)} 套）:")
        for i, s in enumerate(schemes):
            if not isinstance(s, dict):
                continue
            name = s.get("scheme_name") or f"方案{i + 1}"
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
            parts = [f"[{is_selected}] 用户可见方案{i + 1}（selected_indices={i}）: {name}"]
            if s.get("report_text"):
                parts.append("正文摘要=" + str(s["report_text"])[:180])
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

def explicit_image_prompt_selection(message: str) -> list[int] | str | None:
    """Only complete commands to generate from existing prompts bypass the LLM."""
    text = re.sub(r"[\s，。！!？?、]", "", message)
    match = re.fullmatch(
        r"(?:请|请帮我|帮我)?(?:使用|用|采用|按照|按)"
        r"(最后(?:一)?[个条]|全部|所有|第[一二三四五六七八九十两0-9]+[个条])"
        r"(?:的)?提示词(?:再|继续|重新)?(?:生图|生成图片|生成图像|生成图)(?:吧|一下)?", text)
    if not match:
        return None
    scope = match[1]
    if scope.startswith("最后"):
        return "last"
    if scope in ("全部", "所有"):
        return "all"
    number = scope[1:-1]
    numbers = {c: i for i, c in enumerate("零一二三四五六七八九十")}
    numbers["两"] = 2
    index = int(number) if number.isdecimal() else numbers.get(number)
    return [index - 1] if index is not None else None


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


# Keep operation separate from the product target: edit is a compatible API
# envelope; edit_mode selects the original generator versus incremental editing.
OPERATION_CONTRACT = """
【执行协议，优先于旧版重做规则】
对已有任务，intent=edit 时必须返回 edit_mode: regenerate 或 refine。
用户要求重新生成/重做/从头生成某层产物，edit_mode=regenerate；
修改、补充、调整已有产物，edit_mode=refine。不能把完整重做解释为微调。
refine_target 只表示目标产物：报告=node1，商拍方案=node2，生图提示词=node3，图片=node4。
明确产物名优先于当前节点；无产物名时采用用户选中的目标或当前节点。
否定重做不属于 regenerate；局部修改后要求重新输出仍是 refine。
node1 已锁定仍返回 redo_blocked；不能跳过上游确认。
例如：重新生成商拍方案 -> edit/node2/regenerate；把方案场景改为室外 -> edit/node2/refine；
重新生成提示词 -> edit/node3/regenerate；重新生图 -> edit/node4/regenerate。
【使用已有提示词继续生图】
图片结果阶段要求用已有提示词生图，返回 edit/node4/regenerate，不是 confirm_generation 或入库。
selected_indices 表示所使用提示词的零基索引数组；“最后一个提示词”返回 "last"，全部返回 "all"，未指定范围返回 null。
例如“使用最后一个提示词生图” -> edit/node4/regenerate, selected_indices="last"。
不得把使用提示词生图误判为重新生成提示词；要求修改提示词才以 node3 为目标。
【商拍方案微调范围与数量｜必须返回】
针对已有商拍方案的字段修改（即使没有“微调”二字）必须判为 edit/node2/refine。
例如“方案2，品牌为星巴克”是修改方案2，不是选择确认，也不是重新生成全部方案。
node2/refine 必须返回 scheme_source（current=当前方案，initial=最开始/首次方案）、selected_indices（非空的零基整数数组）及 scheme_output_count（正整数）。
根据当前方案列表和用户原话判定输入范围与输出数量，二者独立；禁止套用首次生成默认3套。
普通微调输出数量等于目标方案数；指定扩展、融合、删减时按用户要求判定输出数量。
例：当前有3套，“方案2，品牌为星巴克” -> selected_indices=[1], scheme_output_count=1。
“方案1和方案3品牌改为星巴克” -> [0,2], 2；“方案2扩展成3个版本” -> [1], 3。
“融合方案1和方案2” -> [0,1], 1；“全部方案品牌改为星巴克” -> [0,1,2], 3。
没有指定编号时结合已选方案、方案名称和上下文判断；无法确定时返回 chat_outside 并说明需要澄清，禁止默认全选。
“将最开始的方案2，重新调整，品牌改为tims” -> scheme_source=initial, selected_indices=[1], scheme_output_count=1。
索引必须相对于 scheme_source 指定的列表；即使当前只剩1套，也不能把最初方案2改成索引0。
未提历史版本时 scheme_source=current；历史信息不足以定位时澄清，不能用当前结果替代。
仅“选择方案2”仍为选择确认，不是微调。其他目标及非微调操作 scheme_output_count=null。
"""


def explicit_regeneration_target(message: str) -> str | None:
    """Only unambiguous whole-product commands bypass the classifier."""
    text = re.sub(r"[\s，。！!？?、]", "", message)
    match = re.fullmatch(
        r"(?:请|帮我|请帮我)?(?:重新生成|重新做|重做|从头生成|重新制作)"
        r"(商拍方案|商拍策划|方案|生图提示词|提示词)(?:吧|一下)?", text)
    if not match:
        return None
    return "node3" if "提示词" in match[1] else "node2"


async def classify(
    message: str,
    *,
    has_task: bool = False,
    current_node: str | None = None,
    completed_mask: list[bool] | None = None,
    has_images: bool = False,
    selected_finetuning_target: str | None = None,
    graph_state_brief: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    prompt_selection = explicit_image_prompt_selection(message)
    if (has_task and current_node == "c4"
            and selected_finetuning_target in (None, "node4") and prompt_selection is not None):
        return {"intent": "edit", "edit_mode": "regenerate", "refine_target": "node4",
                "refine_instruction": message, "selected_indices": prompt_selection,
                "blocked_step": None, "reasoning": "使用指定的已有提示词继续生图"}
    explicit_target = explicit_regeneration_target(message) if has_task else None
    if explicit_target:
        return {"intent": "edit", "edit_mode": "regenerate", "refine_target": explicit_target,
                "refine_instruction": message, "selected_indices": None,
                "blocked_step": None, "reasoning": "明确要求完整重生成指定产物"}
    if (has_task and current_node == "c4"
            and selected_finetuning_target in (None, "node4")
            and is_image_regeneration_request(message)):
        return {"intent": "edit", "edit_mode": "regenerate", "refine_target": "node4",
                "refine_instruction": message, "selected_indices": None,
                "blocked_step": None, "reasoning": "图片结果阶段明确要求再次生图"}
    try:
        result = await _classify_via_llm(
            message,
            has_task=has_task,
            current_node=current_node,
            completed_mask=completed_mask,
            has_images=has_images,
            selected_finetuning_target=selected_finetuning_target,
            graph_state_brief=graph_state_brief,
            task_id=task_id,
        )

        if not result.get("refine_target") and result.get("intent") not in (
            "chat_outside", "start_task", "redo_blocked", "skip_forward",
            "confirm_current", "confirm_generation", "select_topics",
        ):
            _FALLBACK_NODE = {"c1": "node1", "c2": "node2", "c3": "node3", "c4": "node4"}
            result["refine_target"] = _FALLBACK_NODE.get(current_node)

        if selected_finetuning_target and result.get("intent") == "edit":
            result["refine_target"] = selected_finetuning_target

        _has_node2_product_name = any(k in message for k in ("方案", "商拍方案"))
        if (
            not selected_finetuning_target
            and current_node == "c3"
            and result.get("intent") == "edit"
            and result.get("refine_target") == "node2"
            and not _has_node2_product_name
        ):
            log_message(f"[intent] 🛡️ c3 当前节点优先兜底：LLM 判了 node2 但消息无'方案'字样 → 覆盖为 node3, msg={message[:50]}", page='对话', business='意图识别', status='记录')
            result["refine_target"] = "node3"

        return result
    except RuntimeError as exc:
        # RuntimeError 里明确区分"模型池全挂" vs 其他运行时错误：
        # ModelPool 在全部模型不可用时抛 RuntimeError("模型池全部不可用")
        msg = str(exc)
        if "模型池" in msg and "不可用" in msg:
            log_message(f"[intent] 🛑 模型池全部不可用 → intent 标记为 model_pool_unavailable: {msg}", page='对话', business='意图识别', status='记录')
            return {
                "intent": "model_pool_unavailable",
                "reasoning": msg,
                "refine_target": None,
                "refine_instruction": None,
                "selected_indices": None,
                "blocked_step": None,
            }
        # 其他 RuntimeError 仍按通用异常处理（见下面）
        log_message(f"[intent] ⚠️ RuntimeError（非模型池）→ fallback chat_outside: {msg}", page='对话', business='意图识别', status='警告')
        return {
            "intent": "chat_outside",
            "reasoning": f"LLM 调用失败: {msg}",
            "refine_target": None,
            "refine_instruction": None,
            "selected_indices": None,
            "blocked_step": None,
        }
    except Exception as exc:
        log_message(f"[intent] ❌ LLM 分类失败 → fallback chat_outside: {exc}", page='对话', business='意图识别', status='失败')
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

@business_operation("意图识别")
async def _classify_via_llm(
    message: str, *, has_task: bool, current_node: str | None,
    completed_mask: list[bool] | None, has_images: bool,
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
            f"请务必尊重这个选择，把 intent 判为 edit 并使用该 refine_target；edit_mode 仍按用户要求区分重做与微调"
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
    from wellflow.app.newapi.client_factory import get_llm_client
    client = get_llm_client("text", model_override=settings.classifier_model)
    resp = await client.chat(
        system=get_active_prompt("intent_classifier") + OPERATION_CONTRACT, user=user_prompt,
        response_format={"type": "json_object"}, temperature=0.2,
        reasoning_effort=settings.text_reasoning_effort,
    )
    used_model = settings.classifier_model

    try:
        data = json.loads(resp.content.strip())
    except Exception:
        event("结果解析失败", model=used_model, fallback="chat_outside", reason="模型输出非JSON")
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
        log_message(f"[intent] ⚠️ LLM 返回非白名单 intent='{intent}' → 降级 chat_outside", page='对话', business='意图识别', status='警告')
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
        "edit_mode": "regenerate" if data.get("edit_mode") == "regenerate" else "refine",
        "reasoning": data.get("reasoning", ""),
        "refine_target": data.get("refine_target"),
        "refine_instruction": message.strip() if intent == "edit" else refine_instruction,
        "selected_indices": data.get("selected_indices"),
        "scheme_output_count": data.get("scheme_output_count"),
        "scheme_source": data.get("scheme_source"),
        "blocked_step": data.get("blocked_step"),
        "parsed_content": data.get("parsed_content"),  # 保留兼容旧消费者
    }
    log_message(f"[intent] {used_model} → {intent} refine_target={result['refine_target']}: "
          f"mode={result['edit_mode']} {result.get('reasoning', '')[:80]}", page='对话', business='意图识别', status='记录')
    return result
