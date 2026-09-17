"""意图分类器 —— LLM + 关键词快速路径，路由 /api/chat 输入到工作流意图。

设计原则：
  1. **节点优先**：大多数关键词规则都绑定到特定 current_node（c1/c2/c3/c4），
     所以同样的短语在不同节点会有不同路由。例："重新生成图" 在 c3 → redo_generation，
     在 c1 → backward_to_c1（因为 c1 还没图，用户一定是想重跑前面的分析）。
  2. **backward > redo > edit**：任何明确说"回/重做第X步/重新分析"的短语，
     必须排在 redo/confirm/edit 之前判断，否则会被后面的规则吞掉。
  3. **LLM 兜底**：关键词返回 None 时再走 LLM，model = volcengine/doubao-seed-1-6-flash，
     reason_effort=None（关闭推理提速），response_format=json_object。
  4. **UTF-8 容错**：chat.py 入口做 latin-1→utf-8 纠正（macOS curl 常见）。

10 个意图 × 4 个 interrupt 节点的路由矩阵见 _try_keywords 注释。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Literal

from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# 模糊匹配参数
# ---------------------------------------------------------------------------
# WRatio 对词序/否定词更敏感，partial_ratio 容忍子串错位；
# 两者取最大值做子串级模糊匹配，阈值 82 足够容忍错别字/同义词，
# 又能挡住 "不要重做"/"别重新来" 这种否定句（WRatio 会明显掉分）。
FUZZ_THRESHOLD = 82


def _fuzz_match(text: str, words: Iterable[str]) -> int:
    """返回关键词表在 text 中的最高相似度（0-100），没命中则 0。"""
    if not text:
        return 0
    t = text.strip().lower()
    best = 0
    for w in words:
        lower = w.lower()
        # WRatio 关注词序 + 否定词（"不要重做" vs "重做"）
        score = max(
            fuzz.WRatio(lower, t),
            fuzz.partial_ratio(lower, t),
        )
        if score > best:
            best = score
            if best >= FUZZ_THRESHOLD:
                break  # 够高就提前停
    return best


def _fuzz_any(text: str, words: Iterable[str]) -> bool:
    return _fuzz_match(text, words) >= FUZZ_THRESHOLD


INTENT = Literal[
    "start_task", "confirm_current", "edit_and_confirm_c1",
    "select_topics", "redo_generation", "confirm_generation",
    "backward_to_c1", "backward_to_c2", "backward_to_c3",
    "skip_forward", "chat_outside", "unknown",
]

# 意图注册集合 —— LLM 返回值必须在里面
ALLOWED_INTENTS: set[str] = {
    "start_task", "confirm_current", "edit_and_confirm_c1",
    "select_topics", "redo_generation", "confirm_generation",
    "backward_to_c1", "backward_to_c2", "backward_to_c3",
    "skip_forward", "chat_outside", "unknown",
}


CLASSIFIER_SYSTEM = """你是 Wellflow 图像创作工作台的意图路由器。把用户输入归类到以下意图之一，返回合法 JSON。

## 意图列表（按 interrupt 节点）

### 所有节点都能返回
- **chat_outside**: 闲聊/商拍无关（"你好"/"天气"/"帮我写代码"）
- **backward_to_c1**: 明确想回到第1步（报告/商品分析）——"回到报告/重新分析/重来第一步/换商品"
- **backward_to_c2**: 明确想回到第2步（选题/方案）——"回到选题/重新选方案/重来第二步/换方案"
- **backward_to_c3**: 明确想回到第3步（提示词）——"回到提示词/重做提示词/重来第三步"

### 仅特定节点
- c1（报告确认）:
  - **confirm_current**: "好的/继续/就这样/ok/可以"
  - **edit_and_confirm_c1**: 自然语言修改报告（"品牌定位改成轻奢"）
- c2（选题确认）:
  - **confirm_current**: "好的/继续"（默认全选）
  - **select_topics**: "全选/用第1和第3"
  - 想改 brief/方案 → **backward_to_c1** 或 **backward_to_c2**
- c3（生图确认）:
  - **redo_generation**: "重做这张/换一批/换一张/重新生成图"
  - **confirm_generation**: "就这样/满意/保存/确认"
- c4（任务完成）:
  - **redo_generation**: "重新生成图/换一张"（回到 c3 重跑生图）
  - **confirm_generation**: "保存/满意/结束"

### 无任务（无进行中 interrupt）
- **start_task**: 有图 + 非闲聊 → 开始新任务
- **chat_outside**: 闲聊 → 拦截

### 禁止返回的意图
- **skip_forward**: 跳过前置步骤直接往下走是不允许的，**绝不要返回 skip_forward**
- edit_and_confirm_c1 仅在 **c1 节点**下有效，c2/c3/c4 下返回它会导致 dispatch 层丢数据

## 关键判定法则（请严格遵守）
1. 当用户说"重新生成"但没有明确说"图"时，**根据 current_node 判断目标**：
   - c1/c2 下一律走 backward（因为还没到生图）
   - c3/c4 下若没说图也可能是 redo，但优先看有没有 backward 信号
2. 当用户说"不行/不满意/再试/不好看"时，**永远不要返回 edit_and_confirm_c1**，
   这是用户想重跑的信号，按 current_node 走 backward 或 redo
3. "重新生成XXX"：如果 XXX 是"报告/分析/方案/选题"等前面步骤产物 → backward；
   如果 XXX 是"图/图片" → redo_generation；无 XXX 则按 current_node 判断

## 输出格式
```json
{
  "intent": "<上面列表中的一个>",
  "reasoning": "<简短说明，关键：必须提到 current_node 和为什么选这个意图>",
  "selected_indices": ["0","2"] 或 "all" 或 null,
  "blocked_step": null,
  "parsed_content": "<用户消息提取的实质性内容>"
}
```"""


# ---------------------------------------------------------------------------
# Step 编号映射（给 LLM prompt 里的参考）
# ---------------------------------------------------------------------------
STEP_ORDER = ["s1", "s2", "s3", "s4", "s5", "s6"]
STEP_NAMES = {
    "s1": "商品分析", "s2": "报告确认(c1)", "s3": "选题生成",
    "s4": "选题确认(c2)", "s5": "图片生成", "s6": "生图确认(c3)",
}


# ---------------------------------------------------------------------------
# 动作词典（统一用 rapidfuzz 模糊匹配，正则仅保留结构化提取）
# ---------------------------------------------------------------------------
# 每个词典 key 是路由意图组，value 是一组容忍说法变体的触发词。
# 词典写得比原正则宽松（"识别"/"商品"/"报告" 单独也能命中），
# 但所有 _match_* 函数都会叠加 current_node 守卫和"否定词排除"。

# ---------------------------------------------------------------------------
# 动作词典（统一用 rapidfuzz 模糊匹配，正则仅保留结构化提取）
# ---------------------------------------------------------------------------
# 每个路由组 = {动作词} × {产物词}，两边都命中才算匹配。
# 这样 "重跑一下报告分析" / "再来一次方案" / "换个 prompt" 这类
# 语序/说法变体都能命中，而不需要在词典里穷举每个组合。

_BACKWARD_VERBS = ["重新", "重做", "重来", "重跑", "再", "换", "改", "回到", "退到", "撤回", "重新做", "重新出"]
_REDO_VERBS = ["重新生成", "重做", "重来", "再来", "换", "再做", "再出", "再画", "重新画", "重做这张", "重来一张"]

# 产物词 —— 明确指向各个节点的产物
_C1_PRODUCTS = ["报告", "商品分析", "分析", "识别", "商品", "brief", "briefing", "第一步"]
_C2_PRODUCTS = ["方案", "选题", "方向", "第二步"]
_C3_PRODUCTS = ["提示词", "prompt", "第三步"]

# 裸重做：短短语，必须在"不要/别"之外才命中（见 _match_bare_redo 的否定词过滤）
_BARE_REDO_WORDS = ["重做", "重来", "重跑"]

# redo 抱怨词（独立命中，不需要配动词）
_REDO_COMPLAINT_WORDS = ["不满意", "不太满意", "不好看", "不行", "画得不好", "这张不行", "不太行"]

# confirm：短短语（去掉容易被 WRatio 误匹配的单字）+ 有方向的"进入第X步"
_CONFIRM_SHORT_WORDS = [
    "好的", "ok", "没问题", "通过", "就这样", "确认", "可以",
    "可以了", "没问题了", "就这样吧", "满意", "确定", "没错", "保存", "结束",
    "继续",
    # 去掉单字 "行" / "过" / "走" —— WRatio 会把 "跳过"/"过一下" 里的单字误命中
]
_CONFIRM_DIRECTION_WORDS = ["继续", "进入", "走到", "来到", "前往"]

# select_topics："全选" 用完全匹配（避免 "全选换一批" 这种被误吞）
_SELECT_ALL_WORDS = ["全选", "都要", "全部方案", "所有方案", "每个方案"]

# skip_forward：必须在闲聊之前拦截
_SKIP_WORDS = ["跳过", "绕过去", "省掉", "不用分析", "直接出图", "直接生成图", "直接做图", "直接画图", "帮我出图", "帮我做图"]

# c3 微调（归 redo）
_C3_TUNE_WORDS = ["换背景", "换个背景", "换颜色", "换个颜色", "调暗", "调亮", "调一下", "换个风格", "换姿势", "换角度", "换滤镜", "修一下"]

# c4 微调/改图（归 redo_generation）—— 产物 + 修改动作
_C4_EDIT_WORDS = ["换背景", "换颜色", "换色调", "调暗", "调亮", "调白", "调粉", "换个风格", "换姿势", "换 pose", "换角度", "换滤镜", "换构图", "换光线", "换模特", "换衣服", "换服装", "改一下", "修一下", "再做一下", "重新画"]

# 纯文本 start_task 兜底（无图）
_START_TASK_TEXT_WORDS = ["我想做商拍", "帮我做商拍", "我要做商拍", "商拍图", "广告图", "商品主图", "出图", "做图", "画出来", "生成图片"]

# 闲聊拦截（最高优先级）
_CHAT_OUTSIDE_SHORT_WORDS = ["你好", "hi", "hello", "在吗", "喂", "嘿", "嗨", "你是谁", "你叫什么"]
_CHAT_OUTSIDE_TOPIC_WORDS = ["天气", "今天怎么样", "几点了", "现在时间", "日期", "写 python", "写 java", "写代码", "帮我写代码", "推荐电影", "推荐书", "推荐音乐", "推荐餐厅"]

# --- 结构提取正则（只负责抽数字/选项，不做意图判定）---
_SELECT_INDICES_PATTERNS = [
    r"第(\d+)[、,，和\s]+第?(\d+)",
    r"第?(\d+)\s*和\s*第?(\d+)",
    r"用第?(\d+)[、,，]\s*第?(\d+)",
]

# --- 尾部语气词剥离 ---
_TONE_SUFFIXES = ("吧", "啊", "哦", "啦", "呀", "哈", "呢", "咧", "咯", "嘞", "噻")

# --- 否定词：前置出现时，redo 动作词命中不算 ---
_NEGATION_PREFIXES = ("不要", "别", "不用", "无需", "算了", "算了吧", "暂时不", "先不要")


def _strip_tone(text: str) -> str:
    t = text.strip()
    for suffix in _TONE_SUFFIXES:
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
            break
    return t


# ---------------------------------------------------------------------------
# 匹配器 —— 每个返回 bool 或具体数据，caller 决定是否短路
# ---------------------------------------------------------------------------

def _has_negation(text: str) -> bool:
    return any(text.startswith(n) or n in text for n in _NEGATION_PREFIXES)


def _match_backward_c1(text: str, current_node: str | None) -> bool:
    if current_node not in ("c1", "c2", "c3", "c4"):
        return False
    if _has_negation(text):
        return False
    # 动作动词 × C1 产物词 —— 两边都命中才算
    return _fuzz_any(text, _BACKWARD_VERBS) and _fuzz_any(text, _C1_PRODUCTS)


def _match_backward_c2(text: str, current_node: str | None) -> bool:
    if current_node not in ("c1", "c2", "c3", "c4"):
        return False
    if _has_negation(text):
        return False
    return _fuzz_any(text, _BACKWARD_VERBS) and _fuzz_any(text, _C2_PRODUCTS)


def _match_backward_c3(text: str, current_node: str | None) -> bool:
    if current_node not in ("c1", "c2", "c3", "c4"):
        return False
    if _has_negation(text):
        return False
    return _fuzz_any(text, _BACKWARD_VERBS) and _fuzz_any(text, _C3_PRODUCTS)


def _match_bare_redo(text: str, current_node: str | None) -> str | None:
    """裸'重做'——短短语 + 不含否定词。根据 current_node 动态路由。"""
    if current_node not in ("c1", "c2", "c3", "c4"):
        return None
    if _has_negation(text):
        return None
    # 只匹配短短语（<= 4 字），避免吞 "重新生成方案" 这种带后缀的
    t = _strip_tone(text).strip()
    if len(t) > 4:
        return None
    if not _fuzz_any(t, _BARE_REDO_WORDS):
        return None
    return {
        "c1": "backward_to_c1",
        "c2": "backward_to_c1",
        "c3": "backward_to_c2",
        "c4": "backward_to_c3",
    }[current_node]


def _match_redo(text: str, current_node: str | None) -> bool:
    """redo_generation —— 明确指向重做生图。

    只有 c3 / c4 节点才返回 redo_generation。
    两条命中路径：
      1. 抱怨词（"不满意"/"不好看"/"不太行"）独立命中
      2. redo 动作动词 × 图类产物词 —— 两边都命中才算
    """
    if current_node not in ("c3", "c4"):
        return False
    if _has_negation(text):
        return False
    # 抱怨词独立命中（c3/c4 守卫已保证上下文正确）
    if _fuzz_any(text, _REDO_COMPLAINT_WORDS):
        return True
    # 动作动词 × 图产物 组合命中
    _redo_products = ["图", "画", "图片", "生图", "出图", "这张", "这些"]
    if _fuzz_any(text, _REDO_VERBS) and _fuzz_any(text, _redo_products):
        return True
    return False


def _match_confirm(text: str, current_node: str | None) -> bool:
    if not current_node:
        return False
    t = _strip_tone(text).strip()
    # 短短语：完全相等匹配（避免 WRatio 把 "不满意" 里的 "满意" 吞掉 confirm）
    if t.lower() in {w.lower() for w in _CONFIRM_SHORT_WORDS}:
        return True
    if t in ("下一步", "下一个"):
        return True
    # "继续/进入/走到..." —— 方向动词本身即确认信号（否定句除外）。
    # 不再强制要求带"第X步"：单独的"继续推进"/"进入下一步"也是确认。
    if not _has_negation(t) and _fuzz_any(t, _CONFIRM_DIRECTION_WORDS):
        return True
    return False


def _match_select_all(text: str, current_node: str | None) -> bool:
    if current_node != "c2":
        return False
    # "全选" 类用完全相等判断，避免 "全选换一批" 被提前吞掉 redo
    t = _strip_tone(text).strip()
    return t in _SELECT_ALL_WORDS


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


def _match_c3_tune(text: str) -> bool:
    return _fuzz_any(text, _C3_TUNE_WORDS)


def _match_c4_edit(text: str) -> bool:
    return _fuzz_any(text, _C4_EDIT_WORDS)


def _match_skip(text: str) -> bool:
    return _fuzz_any(text, _SKIP_WORDS)


def _match_start_task_text(text: str) -> bool:
    return _fuzz_any(text, _START_TASK_TEXT_WORDS)


def _match_chat_outside(text: str) -> bool:
    t = _strip_tone(text).strip().lower()
    if _fuzz_any(t, _CHAT_OUTSIDE_SHORT_WORDS):
        return True
    if _fuzz_any(text, _CHAT_OUTSIDE_TOPIC_WORDS):
        return True
    return False


# ---------------------------------------------------------------------------
# 主入口 —— 关键词优先，否则走 LLM
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
    kw = _try_keywords(
        message, has_task=has_task, current_node=current_node, has_images=has_images,
    )
    if kw:
        print(f"[intent] 关键词命中 → {kw.get('intent')}", flush=True)
        return kw

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


def _try_keywords(
    message: str, *, has_task: bool, current_node: str | None, has_images: bool,
) -> dict[str, Any] | None:
    """关键词快速路径 —— 9 步短路，命中即返回。

    优先级设计：
      Step 0/1  backward/bare_redo —— 最先匹配，因为 "重做"/"重新生成XXX" 这种
                  短语很容易被后面的 redo/confirm/edit 误吞。backward 永远优先。
      Step 2    confirm             —— 极短词，只在有 current_node 时生效
      Step 3    redo_generation     —— 必须 c3/c4 节点 + 有图信号
      Step 4    select_topics       —— 只在 c2
      Step 5    skip_forward        —— 必须在闲聊之前，否则会被 "帮我出图" 这种 skip 吞
      Step 6    chat_outside        —— 最高优先级的闲聊拦截
      Step 7    start_task 兜底    —— 无 task + 有图 + 非闲聊
      Step 8    start_task 纯文本  —— 无 task + 无图 + 商拍关键词
      Step 9    节点专属 edit 兜底 —— c1 自然语言 → edit_and_confirm_c1;
                                     c3 微调 → redo_generation;
                                     c4 微调 → redo_generation;
                                     c2 改 brief → backward_to_c1
    """
    text = message.strip()

    # ────────────────────────────────────── 空消息 ──────────────────────────────────────
    if not text:
        if not has_task and has_images:
            return {"intent": "start_task", "reasoning": "空消息+有图+无任务",
                    "selected_indices": None, "blocked_step": None, "parsed_content": ""}
        if has_task and current_node:
            return {"intent": "confirm_current", "reasoning": "空消息+在interrupt→默认确认",
                    "selected_indices": None, "blocked_step": None, "parsed_content": ""}
        return {"intent": "chat_outside", "reasoning": "空消息",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 0: bare_redo ──────────────────────────────────────
    # "重做"/"重来"/"重新" —— 单字完全匹配（^锚定，避免吞 "重新生成方案"）
    bare = _match_bare_redo(text, current_node)
    if bare:
        return {"intent": bare,
                "reasoning": f"关键词: 裸重做→动态路由",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 1: backward 三件套 ──────────────────────────────────────
    # backward 永远在 redo 之前 —— 任何 "回到报告"/"重新分析"/"改方案" 都不能被 redo 吞
    if _match_backward_c1(text, current_node):
        return {"intent": "backward_to_c1",
                "reasoning": "关键词: 目标=报告/商品分析/第一步",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}
    if _match_backward_c2(text, current_node):
        return {"intent": "backward_to_c2",
                "reasoning": "关键词: 目标=选题/方案/第二步",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}
    if _match_backward_c3(text, current_node):
        return {"intent": "backward_to_c3",
                "reasoning": "关键词: 目标=提示词/第三步",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 2: confirm ──────────────────────────────────────
    # 极短词（剥离语气词后）
    if _match_confirm(text, current_node):
        if current_node in ("c3", "c4"):
            return {"intent": "confirm_generation",
                    "reasoning": "关键词: 确认生图结果/保存",
                    "selected_indices": None, "blocked_step": None, "parsed_content": None}
        return {"intent": "confirm_current",
                "reasoning": "关键词: 确认当前步骤继续",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 3: redo_generation ──────────────────────────────────────
    # 必须 c3/c4 节点 + 有图信号（"重做图"/"重新生成图"/"不满意"）
    if _match_redo(text, current_node):
        return {"intent": "redo_generation",
                "reasoning": "关键词: 重做生图（c3/c4 节点）",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 4: select_topics（仅 c2） ──────────────────────────────────────
    if _match_select_all(text, current_node):
        return {"intent": "select_topics", "reasoning": "关键词: 全选所有选题",
                "selected_indices": "all", "blocked_step": None, "parsed_content": None}
    indices = _match_select_indices(text, current_node)
    if indices:
        return {"intent": "select_topics",
                "reasoning": f"关键词: 选中 indices={indices}",
                "selected_indices": [str(i) for i in indices],
                "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 5: skip_forward ──────────────────────────────────────
    # 允许在任何节点触发（包括无节点时）—— 命中即拦截，告诉用户不能跳
    # 放这步是因为 skip 信号里有"帮我出图"这种短语，容易被后面的 start_task 或 chat_outside 吞
    if _match_skip(text):
        step_idx = 1
        step_name = "商品分析"
        idx_map = {"c1": 2, "c2": 4}
        name_map = {"c1": "报告确认", "c2": "选题确认"}
        if current_node in idx_map:
            step_idx = idx_map[current_node]
            step_name = name_map[current_node]
        return {"intent": "skip_forward",
                "reasoning": f"关键词: 试图跳过{step_name}",
                "selected_indices": None,
                "blocked_step": {"index": step_idx, "name": step_name},
                "parsed_content": None}

    # ────────────────────────────────────── Step 6: chat_outside ──────────────────────────────────────
    # 最高优先级闲聊拦截 —— 有/无任务都生效
    if _match_chat_outside(text):
        return {"intent": "chat_outside",
                "reasoning": "关键词拦截: 闲聊/非商拍",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 7: start_task（无 task + 有图） ──────────────────────────────────────
    if not has_task and has_images:
        return {"intent": "start_task", "reasoning": "关键词兜底: 无 task + 有图",
                "selected_indices": None, "blocked_step": None, "parsed_content": text}

    # ────────────────────────────────────── Step 8: start_task（无 task + 无图 + 商拍关键词） ──────────────────────────────────────
    # 场景：用户只打了一句"我想做商拍"就回车 —— 没有图也没有闲聊 → 当作开始任务请求
    # 前端拿到后会自动提示用户上图（start_task dispatch 时前端会 showUploadPanel）
    if not has_task and _match_start_task_text(text):
        return {"intent": "start_task", "reasoning": "关键词: 纯文本+商拍相关→开始任务",
                "selected_indices": None, "blocked_step": None, "parsed_content": text}

    # ────────────────────────────────────── Step 9: 节点专属兜底 ──────────────────────────────────────
    # 走到这里说明：有 current_node 但上面 8 步都没命中 → 看节点专属规则

    if current_node == "c1":
        # c1 下的自然语言 —— 默认当作编辑报告（改 brief/品牌定位/用户群...）
        # 但如果用户说的话里含有 redo/抱怨/图相关信号，让 LLM 去判断更安全
        # （c1 还没图，用户说"重新生成图"可能是想回 c1 重跑、也可能想跳过 → LLM 知道 context）
        _C1_REDO_SIGNAL_WORDS = [
            "图", "画", "背景", "颜色", "色调", "姿势", "风格", "图片",
            "重做", "重新生成", "重新做", "重新出", "再试", "再做", "再出", "再来",
            "不行", "不满意", "不好看",
        ]
        if _fuzz_any(text, _C1_REDO_SIGNAL_WORDS):
            return None  # 交给 LLM
        return {"intent": "edit_and_confirm_c1",
                "reasoning": "c1 节点自然语言输入→编辑报告",
                "selected_indices": None, "blocked_step": None, "parsed_content": text}

    if current_node == "c3":
        # c3 下的微调（换背景/调暗/换风格）—— 归 redo（让用户基于当前图调）
        if _match_c3_tune(text):
            return {"intent": "redo_generation",
                    "reasoning": "c3 微调描述→重做生图",
                    "selected_indices": None, "blocked_step": None, "parsed_content": text}

    if current_node == "c4":
        # c4 下还想改图 —— 归 redo_generation（让 graph 回到 c3 resume redo）
        # 或者如果明显是想回到更早的步骤，会在 backward 里被命中（Step 1）
        if _match_c4_edit(text):
            return {"intent": "redo_generation",
                    "reasoning": "c4 下改图描述→重做生图（回到 c3）",
                    "selected_indices": None, "blocked_step": None, "parsed_content": text}

    if current_node == "c2":
        # c2 下还没命中任何规则 —— 用户可能想改 brief/改方案
        # dispatch 层 chat.py 对 edit_and_confirm_c1 只在 c1 分支处理，
        # c2 下发 edit_and_confirm_c1 会导致丢数据 → 走 LLM 兜底
        # 让 LLM 按 current_node=c2 去判断（大概率会给 backward_to_c1/c2 或 redo）
        return None

    # 兜底 None → 让 LLM 处理
    return None


# ---------------------------------------------------------------------------
# LLM classifier —— 关键词返回 None 时调用
# ---------------------------------------------------------------------------

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
        system=CLASSIFIER_SYSTEM,
        user=user_prompt,
        response_format={"type": "json_object"},
        temperature=0.3,
        reasoning_effort=None,  # 简单分类关掉推理提速
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
    if intent not in ALLOWED_INTENTS:
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
# 跳步检测工具
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
        idx_map = {"c1": 1, "c2": 3, "c3": 5, "c4": 6}
        idx = idx_map.get(interrupt_node)
        if idx:
            for i in range(idx):
                mask[i] = True

    return mask
