"""意图分类器 —— 用 LLM 判断用户在 /api/chat 里说的话对应哪个工作流意图。

规则：
  - 只允许 forward 通过（S1 → S2 → S3 → S4 → S5 → S6），不能跳过任何步骤
  - 允许 backward 回到任意已完成的步骤（Command(goto=...) 实现）
  - 与工作流无关的闲聊 → 返回 chat_outside，前端显示固定拒绝语

模型：openai/gpt-5.6-luna, reasoning_effort=low, response_format=json_object
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from wellflow.app.config import settings


INTENT = Literal[
    "start_task", "confirm_current", "edit_and_confirm_c1",
    "select_topics", "redo_generation", "confirm_generation",
    "backward_to_c1", "backward_to_c2", "backward_to_c3",
    "skip_forward", "chat_outside", "unknown",
]


STEP_ORDER = ["s1", "s2", "s3", "s4", "s5", "s6"]
STEP_NAMES = {
    "s1": "商品分析", "s2": "报告确认", "s3": "选题生成",
    "s4": "选题确认", "s5": "图片生成", "s6": "生图确认",
}
INTERRUPT_TO_STEP = {"c1": 1, "c2": 3, "c3": 5, "c4": 6}
# step 索引：c1→s2(idx=1), c2→s4(idx=3), c3→s6(idx=5), c4→全步骤完成(idx=6)


_CLASSIFIER_SYSTEM = """你是 Wellflow 图像创作工作台的意图路由器。把用户输入归类到以下意图之一，返回合法 JSON。

## 意图列表
- **start_task**: 无进行中任务 + 用户想开始商拍任务（需有商品图）
- **confirm_current**: 有进行中任务 + 用户说"继续/好的/就这样/ok/可以/通过"等简短确认
- **edit_and_confirm_c1**: 停在 c1（报告确认）+ 用户想修改报告内容（"品牌定位改成轻奢"）
- **select_topics**: 停在 c2（选题确认）+ 用户指定选哪些方案（"全选/用第1和3个"）
- **redo_generation**: 停在 c3（生图确认）+ 用户想重做图片（"重做/换一批/再试"）
- **confirm_generation**: 停在 c3 + 用户想确认保存（"就这样/满意/保存"）
- **backward_to_c1**: 在 c2/c3 + 用户想回到报告（"回到报告/重新分析"）
- **backward_to_c2**: 在 c3 + 用户想回到选题（"回到选题/重新选方案"）
- **skip_forward**: 用户试图跳过尚未完成的步骤直接往下走
- **chat_outside**: 用户说的话与商拍工作流无关（闲聊、其他领域）

## 输出格式
```json
{
  "intent": "<上面列表中的一个>",
  "reasoning": "<简短说明>",
  "selected_indices": ["0","2"] 或 "all" 或 null,
  "blocked_step": {"index":2,"name":"选题生成"} 或 null,
  "parsed_content": "<用户消息提取的实质性内容>"
}
```

## 步骤编号参考
1.s1=商品分析 2.s2=报告确认(c1) 3.s3=选题生成 4.s4=选题确认(c2) 5.s5=图片生成 6.s6=生图确认(c3)"""


# ---------------------------------------------------------------------------
# 关键词快速匹配
# ---------------------------------------------------------------------------

_BACKWARD_TO_C1_PATTERNS = [
    # 明确指向商品分析/报告
    r"回到.*报告", r"重新分析", r"报告.*改", r"报告.*重", r"报告重写",
    r"改.*报告", r"修改报告", r"再分析一次", r"重新做分析",
    r"重做.*(第一|1).*步", r"回到.*(第一|1).*步", r"第.*(一|1).*步",
    r"重做.*(商品|分析|识别)", r"换商品",
    # 带后缀的重做（有明确目标）
    r"^重做.*分析", r"^重新.*分析", r"^重跑.*分析", r"^重做.*识别", r"^重新.*识别",
]
_BACKWARD_TO_C2_PATTERNS = [
    # 明确指向方案/选题
    r"回到.*选题", r"重新选", r"选题.*改", r"改.*选题",
    r"调整方案", r"改方案", r"换方案", r"换方向", r"重新出方案",
    r"重做.*(第二|2).*步", r"回到.*(第二|2).*步", r"第.*(二|2).*步",
    r"重做.*(方案|选题)",
]
_BACKWARD_TO_C3_PATTERNS = [
    # 指向 prompt/生图参数（c3 确认阶段）
    r"回到.*提示词", r"重做.*提示词", r"重做.*(第三|3).*步",
    r"回到.*(第三|3).*步", r"改.*提示词",
]
_CONFIRM_PATTERNS = [
    r"^(继续|没问题|通过|ok|好的|就这样|确认|可以|行|过|没问题了|可以了|结束|保存|就这样吧|满意|确定|没错)$",
]
_REDO_PATTERNS = [
    # 必须明确指向生图结果，不能用裸 "重做"
    r"重做.*图", r"重做生图", r"重做图片", r"重做这张", r"重做这些",
    r"重新生成.*图", r"重新画", r"再画", r"换一张", r"换一批.*图",
    r"再来.*图", r"重画", r"不满意", r"不好看", r"不行",
]
_SELECT_ALL_PATTERNS = [
    r"全选", r"都要", r"全部", r"所有方案", r"每个方案",
]
_SELECT_INDICES_PATTERNS = [
    r"第(\d+)[、,，和\s]+第?(\d+)",
    r"第?(\d+)\s*和\s*第?(\d+)",
    r"用第?(\d+)[、,，]\s*第?(\d+)",
]
# skip 关键词：用户试图跳过当前步骤直接往下走
_SKIP_PATTERNS = [
    r"直接.*(出图|生成|做图|画图|生图)",
    r"(跳|绕|省)过",
    r"不用.*分析",
    r"跳过.*(分析|报告|选题)",
    r"帮我出.*图",  # 无 task 或有 task 但还没完成前置步骤就说出图
]
# c3 下的非 redo 操作（微调描述）—— 走 redo 或 edit
_C3_TUNE_PATTERNS = [
    r"背景", r"颜色", r"调.*暗", r"调.*亮", r"改一下", r"换个",
    r"姿势", r"角度", r"滤镜", r"风格.*换", r"修一下",
]


def _match_backward_c1(text: str, current_node: str | None) -> bool:
    # c1 也允许（刚跑完 Node1 想重跑），加上之前的 c2/c3/c4
    if current_node not in ("c1", "c2", "c3", "c4"):
        return False
    return any(re.search(p, text) for p in _BACKWARD_TO_C1_PATTERNS)


def _match_backward_c2(text: str, current_node: str | None) -> bool:
    if current_node not in ("c2", "c3", "c4"):
        return False
    return any(re.search(p, text) for p in _BACKWARD_TO_C2_PATTERNS)


def _match_backward_c3(text: str, current_node: str | None) -> bool:
    if current_node not in ("c3", "c4"):
        return False
    return any(re.search(p, text) for p in _BACKWARD_TO_C3_PATTERNS)


# 任意节点下说 "重做/重新/重来" —— 目标是当前节点的前一步
# c1 → backward_to_c1（重跑 Node1）
# c2 → backward_to_c1（改报告影响方案）
# c3 → backward_to_c2（改方案影响 prompt）
# c4 → backward_to_c3（改 prompt 影响生图）
_BACKWARD_BARE_REDO_PATTERNS = [r"^重做$", r"^重新$", r"^重来$", r"^重跑$", r"^重画$"]


def _match_bare_redo(text: str, current_node: str | None) -> str | None:
    """任意 interrupt 节点下的裸 '重做' → 返回目标 backward intent。"""
    if current_node not in ("c1", "c2", "c3", "c4"):
        return None
    if not any(re.search(p, text) for p in _BACKWARD_BARE_REDO_PATTERNS):
        return None
    # 决定目标：从 c1/c2/c3/c4 分别后退到 c1
    target_map = {
        "c1": "backward_to_c1",  # c1 下重做 = 重跑 Node1
        "c2": "backward_to_c1",  # c2 下重做 = 回到报告改
        "c3": "backward_to_c2",  # c3 下重做 = 回到方案改
        "c4": "backward_to_c3",  # c4 下重做 = 回到 prompt 改
    }
    return target_map[current_node]


_TONE_SUFFIXES = ("吧", "啊", "哦", "啦", "呀", "哈", "呢", "咧", "咯", "嘞", "噻")


def _strip_tone(text: str) -> str:
    """剥离末尾语气词，如 '继续吧' → '继续'，'好的哦' → '好的'。"""
    t = text.strip()
    for suffix in _TONE_SUFFIXES:
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
            break
    return t


def _match_confirm(text: str, current_node: str | None) -> bool:
    if not current_node:
        return False
    t = _strip_tone(text).lower()
    return any(re.search(p, t) for p in _CONFIRM_PATTERNS)


def _match_redo(text: str, current_node: str | None) -> bool:
    if current_node not in ("c3", "c4"):
        return False
    return any(re.search(p, text) for p in _REDO_PATTERNS)


def _match_select_all(text: str, current_node: str | None) -> bool:
    if current_node != "c2":
        return False
    return any(re.search(p, text) for p in _SELECT_ALL_PATTERNS)


def _match_select_indices(text: str, current_node: str | None) -> list[int] | None:
    if current_node != "c2":
        return None
    for p in _SELECT_INDICES_PATTERNS:
        m = re.search(p, text)
        if m:
            return [int(x) - 1 for x in m.groups()]
    nums = re.findall(r"\b(\d+)\b", text)
    if nums and len(nums) >= 1 and re.search(r"方案|方向|屏|个|号", text):
        return [int(x) - 1 for x in nums]
    return None


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

async def classify(
    message: str,
    *,
    has_task: bool = False,
    current_node: str | None = None,
    completed_mask: list[bool] | None = None,
    has_images: bool = False,
    product_description: str | None = None,
) -> dict[str, Any]:
    kw_result = _try_keywords(
        message, has_task=has_task, current_node=current_node, has_images=has_images,
    )
    if kw_result:
        print(f"[intent] 关键词命中 → {kw_result.get('intent')}", flush=True)
        return kw_result

    try:
        return await _classify_via_llm(
            message,
            has_task=has_task,
            current_node=current_node,
            completed_mask=completed_mask,
            has_images=has_images,
            product_description=product_description,
        )
    except Exception as exc:
        print(f"[intent] LLM 分类失败 → fallback chat_outside: {exc}", flush=True)
        return {
            "intent": "chat_outside", "reasoning": f"LLM 调用失败: {exc}",
            "selected_indices": None, "blocked_step": None, "parsed_content": None,
        }


def _match_skip(text: str) -> bool:
    return any(re.search(p, text) for p in _SKIP_PATTERNS)


def _match_c3_tune(text: str) -> bool:
    return any(re.search(p, text) for p in _C3_TUNE_PATTERNS)


def _try_keywords(
    message: str, *, has_task: bool, current_node: str | None, has_images: bool,
) -> dict[str, Any] | None:
    text = message.strip()
    if not text:
        # 空消息但有图且无 task → 直接 start_task（用户只上传了商品图）
        if not has_task and has_images:
            return {"intent": "start_task",
                    "reasoning": "关键词快速路径: 空消息+有图+无任务，按开始任务处理",
                    "selected_indices": None, "blocked_step": None,
                    "parsed_content": ""}
        # 空消息但有 task 且在 interrupt → 默认 confirm_current
        if has_task and current_node:
            return {"intent": "confirm_current",
                    "reasoning": "关键词快速路径: 空消息+有任务+在interrupt，默认确认继续",
                    "selected_indices": None, "blocked_step": None,
                    "parsed_content": ""}
        return {"intent": "chat_outside", "reasoning": "空消息",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # 0. 裸 "重做/重新" —— 根据当前节点动态决定目标（放在最前面，优先级最高）
    bare_redo = _match_bare_redo(text, current_node)
    if bare_redo:
        targets = {
            "backward_to_c1": "报告/商品分析",
            "backward_to_c2": "选题/方案",
            "backward_to_c3": "提示词",
        }
        return {"intent": bare_redo,
                "reasoning": f"关键词: 裸重做 → 回到{targets[bare_redo]}",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # 1. backward 类（最高优先级，明确要回退 —— 必须在 redo 之前，避免被裸 "重做" 误吞）
    if _match_backward_c1(text, current_node):
        return {"intent": "backward_to_c1", "reasoning": "关键词: 回到报告/重新分析/重做第一步",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}
    if _match_backward_c2(text, current_node):
        return {"intent": "backward_to_c2", "reasoning": "关键词: 回到选题/调整方案/重做第二步",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}
    if _match_backward_c3(text, current_node):
        return {"intent": "backward_to_c3", "reasoning": "关键词: 回到提示词/重做第三步",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # 2. confirm（极短的确认指令）
    if _match_confirm(text, current_node):
        if current_node == "c3":
            return {"intent": "confirm_generation", "reasoning": "关键词: 确认生图结果",
                    "selected_indices": None, "blocked_step": None, "parsed_content": None}
        if current_node == "c4":
            return {"intent": "confirm_generation", "reasoning": "关键词: 确认最终生图并结束",
                    "selected_indices": None, "blocked_step": None, "parsed_content": None}
        if current_node == "c2":
            return {"intent": "confirm_current", "reasoning": "关键词: 确认选题（默认选中继续）",
                    "selected_indices": None, "blocked_step": None, "parsed_content": None}
        if current_node == "c1":
            return {"intent": "confirm_current", "reasoning": "关键词: 确认报告原样继续",
                    "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # 3. redo（重做生图）
    if _match_redo(text, current_node):
        return {"intent": "redo_generation", "reasoning": "关键词: 重做生图",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # 4. select（选题选择）
    if _match_select_all(text, current_node):
        return {"intent": "select_topics", "reasoning": "关键词: 全选所有选题",
                "selected_indices": "all", "blocked_step": None, "parsed_content": None}
    indices = _match_select_indices(text, current_node)
    if indices:
        return {"intent": "select_topics",
                "reasoning": f"关键词: 选中 indices={indices}",
                "selected_indices": [str(i) for i in indices],
                "blocked_step": None, "parsed_content": None}

    # 5. skip_forward（跳过检测 —— 必须在节点兜底之前，否则会被 c1 的 edit 吞掉）
    if not current_node or current_node in ("c1", "c2"):
        if _match_skip(text):
            idx_map = {"c1": 2, "c2": 4}
            name_map = {"c1": "报告确认", "c2": "选题确认"}
            step_idx = idx_map.get(current_node, 1) if current_node else 1
            step_name = name_map.get(current_node, "商品分析") if current_node else "商品分析"
            return {"intent": "skip_forward",
                    "reasoning": f"关键词: 用户试图跳过{step_name}",
                    "selected_indices": None,
                    "blocked_step": {"index": step_idx, "name": step_name},
                    "parsed_content": None}

    # 7. chat_outside 关键词（最高优先级的闲聊拦截 —— 有任务/无任务都生效）
    if _match_chat_outside(text):
        return {"intent": "chat_outside",
                "reasoning": "关键词拦截: 闲聊/非商拍",
                "selected_indices": None, "blocked_step": None,
                "parsed_content": None}

    # 8. start_task 关键词兜底（无 task + 有图 + 非闲聊）
    if not has_task and has_images:
        return {"intent": "start_task",
                "reasoning": "关键词兜底: 无 task + 有图 → 开始任务",
                "selected_indices": None, "blocked_step": None,
                "parsed_content": text}

    # 9. 节点兜底
    if current_node == "c1":
        return {"intent": "edit_and_confirm_c1",
                "reasoning": "c1 节点下的自然语言输入，当作编辑报告内容",
                "selected_indices": None, "blocked_step": None, "parsed_content": text}
    if current_node == "c3" and _match_c3_tune(text):
        # c3 下的"换背景/调暗"等微调描述 —— 当作 redo
        return {"intent": "redo_generation",
                "reasoning": "c3 节点下微调描述，归入重做生图",
                "selected_indices": None, "blocked_step": None,
                "parsed_content": text}

    return None


def _match_chat_outside(text: str) -> bool:
    """简单闲聊关键词，快速拦截避免走 LLM。"""
    CHAT_OUTSIDE_PATTERNS = [
        r"^(你好|hi|hello|在吗|喂|嘿|嗨)$",
        r"^(你是谁|你叫什么|介绍.*自己)",
        r"(天气|今天.*怎么样|几点了|时间|日期)",
        r"(写.*(python|java|脚本|代码)|帮我写)",
        r"(推荐.*(电影|书|音乐|餐厅)|好看的.*电影)",
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in CHAT_OUTSIDE_PATTERNS)


async def _classify_via_llm(
    message: str, *, has_task: bool, current_node: str | None,
    completed_mask: list[bool] | None, has_images: bool,
    product_description: str | None,
) -> dict[str, Any]:
    from wellflow.app.llm.model_pool import get_model_pool

    ctx_lines = [
        f"has_task={has_task}",
        f"current_node={current_node or '(无，即无任务)'}",
        f"has_images={has_images}",
    ]
    if completed_mask:
        ctx_lines.append(f"completed_mask={completed_mask} (s1-s6)")
    if product_description:
        ctx_lines.append(f"product_description={product_description[:200]}")

    user_prompt = (
        "## 当前任务状态\n"
        + "\n".join(ctx_lines)
        + f"\n\n## 用户消息\n{message}\n\n"
        "请返回意图分类 JSON。"
    )

    pool = get_model_pool()
    resp, used_model = await pool.chat(
        system=_CLASSIFIER_SYSTEM,
        user=user_prompt,
        response_format={"type": "json_object"},
        temperature=0.3,             # 固定 0.3（有 temperature 参数的模型）
        reasoning_effort=None,       # 简单分类不需要深度思考，关掉以提速
    )

    try:
        data = json.loads(resp.content.strip())
    except Exception:
        return {
            "intent": "chat_outside",
            "reasoning": f"LLM 输出非 JSON ({used_model}): {resp.content[:200]}",
            "selected_indices": None, "blocked_step": None, "parsed_content": None,
        }

    intent = data.get("intent")
    valid = {
        "start_task", "confirm_current", "edit_and_confirm_c1",
        "select_topics", "redo_generation", "confirm_generation",
        "backward_to_c1", "backward_to_c2", "backward_to_c3",
        "skip_forward", "chat_outside", "unknown",
    }
    if intent not in valid:
        intent = "unknown"

    result = {
        "intent": intent,
        "reasoning": data.get("reasoning", ""),
        "selected_indices": data.get("selected_indices"),
        "blocked_step": data.get("blocked_step"),
        "parsed_content": data.get("parsed_content"),
    }
    print(f"[intent] {used_model} → {intent}: {result.get('reasoning', '')[:80]}", flush=True)
    return result


# ---------------------------------------------------------------------------
# 跳步检测
# ---------------------------------------------------------------------------

def detect_skip(target_step: str, completed_mask: list[bool]) -> dict[str, Any] | None:
    if target_step not in STEP_ORDER:
        return None
    target_idx = STEP_ORDER.index(target_step)
    for i in range(target_idx):
        if i < len(completed_mask) and not completed_mask[i]:
            return {
                "skipped_index": i,
                "skipped_name": STEP_NAMES[STEP_ORDER[i]],
                "message": f"不支持跳过第 {i + 1} 步（{STEP_NAMES[STEP_ORDER[i]]}），请先完成它。",
            }
    return None


def compute_completed_mask(
    graph_state: dict[str, Any],
    interrupt_node: str | None,
) -> list[bool]:
    mask = [False] * 6
    n1 = graph_state.get("node1", {}) if graph_state else {}
    n2 = graph_state.get("node2", {}) if graph_state else {}
    n3 = graph_state.get("node3", {}) if graph_state else {}

    if n1.get("product_insight"):
        mask[0] = True
    if n2.get("generate_prompts") or interrupt_node in ("c2", "c3"):
        mask[1] = True
    if n2.get("generate_prompts"):
        mask[2] = True
    if (n3.get("outputs") or n3.get("work_items")) or interrupt_node == "c3":
        mask[3] = True
    if n3.get("outputs"):
        mask[4] = True

    if interrupt_node:
        idx = INTERRUPT_TO_STEP.get(interrupt_node)
        if idx:
            for i in range(idx):
                mask[i] = True

    return mask
