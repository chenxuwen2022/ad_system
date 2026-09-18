"""意图分类器 —— LLM + 关键词快速路径，路由 /api/chat 输入到工作流意图。

设计原则（v2：用显式选择替代猜测）：
  1. **方向词+第X步 是唯一精确定位 target 的路径**：用户说"回到第一步/重做第2步"时，
     返回明确的 backward_to_nodeX（如果不允许则被守卫拦截）。
  2. **所有其他 redo 类输入（bare "重做"、产物词、抱怨词、微调词）统一返回 "redo"**，
     不再猜测 target_node。dispatch 层在 redo + 无 target_node 时发 SSE selection_required，
     让前端弹 radio 让用户选。
  3. **confirm / select_topics / start_task / chat_outside / skip / edit 保持原路径**，
     不涉及 target_node 选择。
  4. **redo 守卫**：redo 只允许在有 current_node 的情况下触发（c1/c2/c3/c4），
     finalize 后（phase=done）禁止任何 redo。
  5. **LLM 兜底**：关键词返回 None 时才走 LLM（doubao-seed-1-6-flash，reason_effort=close）。
     LLM 也只判断 redo vs confirm vs 其他，不再猜 node。

意图矩阵见 INTENT 类型注释；关键词短路优先级见 _try_keywords 注释。
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
        score = max(
            fuzz.WRatio(lower, t),
            fuzz.partial_ratio(lower, t),
        )
        if score > best:
            best = score
            if best >= FUZZ_THRESHOLD:
                break
    return best


def _fuzz_any(text: str, words: Iterable[str]) -> bool:
    return _fuzz_match(text, words) >= FUZZ_THRESHOLD


# ---------------------------------------------------------------------------
# 意图全集
# ---------------------------------------------------------------------------
# v2 核心变化：
#   - 所有 redo 类统一为 "redo"（不带 target_node，由前端 radio 选）
#   - backward_to_nodeX 仅在方向词+第X步显式指定时返回（唯一精确路径）
#   - redo_generation 废弃（并入 redo）
#
# c1/c2/c3 是 Node 完成后的 HITL interrupt 节点：
#   c1: Node1(商品分析)完成 → 确认报告
#   c2: Node2(方案)完成 → 选方案
#   c3: Node3(Prompt)完成 → 确认 prompt
#   c4: Node4(生图)完成 → 确认/重做（可回退 node2/node3/node4）
#
# backward_to_nodeX 是方向词+第X步显式指定的精确定位回退，
# 由 graph 的 _route_cX_decision 清理下游 state。

INTENT = Literal[
    "start_task",                    # 无任务 → 开新任务
    "confirm_current",               # c1/c2/c3 通用确认（好的/继续/ok）
    "confirm_generation",            # 仅 c4：确认生图结果（满意/保存/结束）
    "edit_and_confirm_c1",           # 仅 c1：自然语言修改报告
    "edit_and_confirm_c2",           # 仅 c2：自然语言微调/修改商拍方案
    "edit_and_confirm_c3",           # 仅 c3：自然语言微调/修改提示词
    "select_topics",                 # 仅 c2：选方案（全选/用第1个）
    "redo",                          # 重做但没说哪一步 → 前端弹 radio 选
    "backward_to_node1",             # 方向词+第1步显式指定
    "backward_to_node2",             # 方向词+第2步显式指定
    "backward_to_node3",             # 方向词+第3步显式指定
    "backward_to_node4",             # 方向词+第4步显式指定
    "skip_forward",                  # 拦截跳步（不允许跳过）
    "chat_outside",                  # 闲聊/非商拍
    "unknown",                       # LLM 返回了不在白名单里的意图
]

ALLOWED_INTENTS: set[str] = {
    "start_task",
    "confirm_current",
    "confirm_generation",
    "edit_and_confirm_c1",
    "edit_and_confirm_c2",
    "edit_and_confirm_c3",
    "select_topics",
    "skip_forward",
    "chat_outside",
    "unknown",
    "redo_blocked",  # v4: 用户想重做/回退/换节点 —— 已禁用重做，引导用微调
}


# ---------------------------------------------------------------------------
# LLM System Prompt（意图列表与上面 INTENT 保持同步）
# ---------------------------------------------------------------------------

CLASSIFIER_SYSTEM = """你是 Wellflow 图像创作工作台的意图路由器。把用户输入归类到以下意图之一，返回合法 JSON。

## 节点定义（c1-c4）
- c1: 商品分析完成，等用户确认报告
- c2: 方案策划完成，等用户选方案
- c3: Prompt 生成完成，等用户确认 prompt
- c4: 生图完成，等用户确认

## 🔴 核心原则（最重要）
**本工作台不支持"完全重做"或"回到某一步"。** 用户如果表达"重做/重来/换一张/回到第X步/退到上一步/撤回/不满意想重来"这类意图，**一律归为 redo_blocked**，而不是 redo 或 backward_to_nodeX。系统会统一提示用户"可以微调"。

## 意图列表

### 所有节点都能返回
- **chat_outside**: 闲聊/商拍无关（"你好"/"天气"/"帮我写代码"）
- **redo_blocked**: 用户想完全重做、回退、回到某一步、重来、重新生成（**凡是这类意图都归这里**）
- **skip_forward**: 试图跳过工作流步骤（"帮我出图"/"直接生成图"/"跳过分析"）

### 仅特定节点
- c1（报告确认）:
  - **confirm_current**: "好的/继续/就这样/ok/可以"
  - **edit_and_confirm_c1**: 自然语言修改报告（"品牌定位改成轻奢"/"帽子应该是可拆卸的"）—— **修改内容 / 补充内容 / 指出错误 都是 edit_and_confirm_c1，不是 redo_blocked**
- c2（选题确认）:
  - **confirm_current**: "好的/继续"（默认全选）
  - **select_topics**: "全选/用第1和第3/选方案1和2"
  - **edit_and_confirm_c2**: 自然语言微调/修改商拍方案（"微调第一个方案"/"把方案二改成极简风"/"核心卖点加上环保"/"方案三的模特换成短发"）—— **凡是要修改已有方案内容的，都是 edit_and_confirm_c2，不是 select_topics 或 redo_blocked**
- c3（Prompt 确认）:
  - **confirm_current**: "好的/继续/就这样/ok/可以"
  - **edit_and_confirm_c3**: 自然语言微调/修改提示词（"换背景"/"把提示词改得更自然"/"第二个提示词的模特换成长发"）—— **凡是要修改提示词内容的，都是 edit_and_confirm_c3，不是 redo_blocked**
- c4（生图确认）:
  - **confirm_generation**: "就这样/满意/保存/确认/结束"

### 无任务（无进行中 interrupt）
- **start_task**: 有图 + 非闲聊 → 开始新任务
- **chat_outside**: 闲聊 → 拦截

### 禁止返回的意图
- **redo** / **backward_to_nodeX**: 完全重做已禁用。**绝对不要返回这两个意图**，所有重做类统一归 redo_blocked
- **skip_forward**: 跳过前置步骤直接往下走是不允许的，**绝不要返回 skip_forward**
- edit_and_confirm_c1 仅在 **c1 节点**下有效，edit_and_confirm_c2 仅在 **c2 节点**下有效，edit_and_confirm_c3 仅在 **c3 节点**下有效。在其他节点下返回它们会导致 dispatch 层丢数据

## 关键判定法则（请严格遵守）
1. **区分"重做"vs"微调"vs"选择"**:
   - "重新做"/"重来"/"回到第X步"/"退回"/"撤回" → redo_blocked
   - "改一下标题"/"增加一个卖点"/"微调第一个方案"/"把方案二改成极简风" → edit_and_confirm_cX（根据 current_node 选 c1/c2/c3）
   - "用第一个"/"选方案1和2"/"全选" → select_topics（仅 c2 下）
2. "继续/进入" 单独出现且无步骤编号时 → confirm_current（或 confirm_generation 在 c4）
3. "保存/结束/完成" 只有在 c4 下才是 confirm_generation

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
# 节点编号映射（给方向词+第X步 做判断用）
# ---------------------------------------------------------------------------
# Node → 第几步 → interrupt 节点
_STEP_ORDER: list[str] = ["node1", "node2", "node3", "node4"]
_STEP_NUM: dict[str, int] = {"node1": 1, "node2": 2, "node3": 3, "node4": 4}
_NODE_OF_STEP: dict[int, str] = {1: "c1", 2: "c2", 3: "c3", 4: "c4"}
_INTENT_OF_NODE: dict[str, str] = {
    "node1": "backward_to_node1",
    "node2": "backward_to_node2",
    "node3": "backward_to_node3",
    "node4": "backward_to_node4",
}
_CURRENT_NODE_NUM: dict[str, int] = {"c1": 1, "c2": 2, "c3": 3, "c4": 4}

# ---------------------------------------------------------------------------
# 方向词 + "第X步" —— v2 唯一能精确定位 target_node 的路径
# ---------------------------------------------------------------------------
_DIRECTION_STEP_PATTERN = re.compile(r"第\s*([一二三四1-4])\s*步")
_CN_STEP_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _match_direction_step(text: str, current_node: str | None) -> str | None:
    """方向词 + "第X步" → 返回 backward_to_nodeX / confirm_current / confirm_generation。

    这是 v2 唯一能精确定位 target_node 的路径。
    所有其他 redo 输入都走 redo → 前端 radio 选择。

    规则：
      目标步 < 当前节点的编号 → backward_to_node{目标步}（向前回退）
      目标步 ≥ 当前节点编号  → 确认（向后走）
    """
    if current_node not in _CURRENT_NODE_NUM:
        return None
    m = _DIRECTION_STEP_PATTERN.search(text)
    if not m:
        return None
    if _has_negation(text):
        return None
    if not _fuzz_any(text, [*_CONFIRM_DIRECTION_WORDS, *_BACKWARD_VERBS]):
        return None
    target = _CN_STEP_NUM[m.group(1)]
    cur_num = _CURRENT_NODE_NUM[current_node]
    if target < cur_num:
        return _INTENT_OF_NODE[f"node{target}"]
    # 向后走 → 确认（c4 用 confirm_generation）
    if current_node == "c4":
        return "confirm_generation"
    return "confirm_current"


# ---------------------------------------------------------------------------
# redo 关键词 —— v2 所有 redo 类统一返回 redo，不再猜 target_node
# ---------------------------------------------------------------------------
# 动作词（redo 触发词）
# 动作词（完全重做 / 回退 触发词）
# 注意：不要把"改"、"换"这种过宽的单字放进来——"品牌改成轻奢"、"换个背景"是**微调**不是重做，
#   要靠更精确的词（如"换一下"/"换成"/"改回去"）或让 LLM 意图判断兜底
_BACKWARD_VERBS = [
    "重新", "重做", "重来", "重跑", "回到", "退到", "撤回",
    "重新做", "重新出", "换一下", "退回", "回到上一步", "撤回到",
    "完全重做", "全重做", "推翻重来", "从头开始", "重新来",
]
_BARE_REDO_WORDS = ["重做", "重来", "重跑"]
# 动作词（完全重做 / 回退 触发词）
# 注意：只保留 ≥2 字的明确动作，去掉单字（"换"/"再"/"改" 太宽，会误吞微调）
_REDO_VERBS = [
    "重新生成", "重画", "重来", "重做",
    "再来一张", "换一张", "重新做", "重新画",
    "重做这张", "重来一张", "换一下", "换一个",
]
# 抱怨词（redo 触发词，不需要配动词）
_REDO_COMPLAINT_WORDS = ["不满意", "不太满意", "不好看", "不行", "画得不好",
                         "这张不行", "不太行", "不好", "太差", "糟糕"]
# 再试词 —— 必须带"再试"/"重新试"明确动作，避免"换个X"这种微调被误判
_RETRY_WORDS = ["再试一下", "再试试", "再试", "重新试", "重新尝试", "再尝试一次"]

# c3 微调（旧版归 backward_to_node3，v2 归入 redo 让前端选）
_C3_TUNE_WORDS = ["换背景", "换个背景", "换颜色", "换个颜色", "调暗", "调亮",
                  "调一下", "换个风格", "换姿势", "换角度", "换滤镜", "修一下"]

# c4 改图（旧版归 redo_generation，v2 归入 redo 让前端选）
_C4_EDIT_WORDS = ["换背景", "换颜色", "换色调", "调暗", "调亮", "调白", "调粉",
                  "换个风格", "换姿势", "换pose", "换角度", "换滤镜", "换构图",
                  "换光线", "换模特", "换衣服", "换服装", "改一下", "修一下",
                  "再做一下", "重新画"]


# ---------------------------------------------------------------------------
# confirm / select_topics / skip / chat_outside / start_task —— 不变
# ---------------------------------------------------------------------------

# confirm：短短语（去掉容易被 WRatio 误匹配的单字）
_CONFIRM_SHORT_WORDS = [
    "好的", "ok", "没问题", "通过", "就这样", "确认", "可以",
    "可以了", "没问题了", "就这样吧", "满意", "确定", "没错",
    "继续", "下一步", "下一个",
    "很好", "不错", "不错的", "行", "行的", "挺好", "挺好的",
    "棒", "很棒", "好", "对", "好呀", "好啊",
]
# confirm 专属 c4 入库/保存词
_CONFIRM_SAVE_WORDS = ["保存", "结束", "完成", "导出", "落库", "入库", "采纳", "采用"]
# 单字/极短正向确认词 —— c4 专属
_C4_CONFIRM_SHORT = ["好", "对", "行", "落", "可", "棒"]

# 方向词（带"第X步"时用）
_CONFIRM_DIRECTION_WORDS = ["继续", "进入", "走到", "来到", "前往", "回到"]

# select_topics
_SELECT_ALL_WORDS = ["全选", "都要", "全部方案", "所有方案", "每个方案"]

# skip_forward（必须在闲聊之前拦截）
_SKIP_WORDS = ["跳过", "绕过去", "省掉", "不用分析", "直接出图", "直接生成图",
               "直接做图", "直接画图", "帮我出图", "帮我做图"]

# 纯文本 start_task 兜底（无图）
_START_TASK_TEXT_WORDS = ["我想做商拍", "帮我做商拍", "我要做商拍", "商拍图",
                          "广告图", "商品主图", "出图", "做图", "画出来", "生成图片"]

# 闲聊拦截（最高优先级）
_CHAT_OUTSIDE_SHORT_WORDS = ["你好", "hi", "hello", "在吗", "喂", "嘿", "嗨",
                              "你是谁", "你叫什么"]
_CHAT_OUTSIDE_TOPIC_WORDS = ["天气", "今天怎么样", "几点了", "现在时间", "日期",
                              "写 python", "写 java", "写代码", "帮我写代码",
                              "推荐电影", "推荐书", "推荐音乐", "推荐餐厅"]

# --- 结构提取正则（只负责抽数字/选项，不做意图判定）---
_SELECT_INDICES_PATTERNS = [
    r"第(\d+)[、,，和\s]+第?(\d+)",
    r"第?(\d+)\s*和\s*第?(\d+)",
    r"用第?(\d+)[、,，]\s*第?(\d+)",
]

# --- 选择语义动词前缀（_match_select_indices 仅在这些动词后才抽 indices）---
_SELECT_VERB_PREFIXES = [
    "选", "用", "要", "就选", "就用", "就这", "就第一个", "就第",
    "选这", "用这", "选第", "用第", "就",
]

# --- 修改语义动词（_match_select_indices 遇到这些动词时跳过 indices 匹配，交给 edit_and_confirm_cX）---
_EDIT_VERBS = [
    "微调", "改", "修改", "调整", "换", "换成", "改成", "换成",
    "加上", "增加", "去掉", "删除", "换一下", "重写", "优化",
    "润色", "完善", "补充", "去掉", "移除", "换成",
]

# --- 尾部语气词剥离 ---
_TONE_SUFFIXES = ("吧", "啊", "哦", "啦", "呀", "哈", "呢", "咧", "咯", "嘞", "噻")

# --- 否定词：前置出现时，redo 动作词命中不算 ---
_NEGATION_PREFIXES = ("不要", "别", "不用", "无需", "算了", "算了吧",
                      "暂时不", "先不要")


def _strip_tone(text: str) -> str:
    t = text.strip()
    for suffix in _TONE_SUFFIXES:
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
            break
    return t


def _has_negation(text: str) -> bool:
    return any(text.startswith(n) or n in text for n in _NEGATION_PREFIXES)


# ---------------------------------------------------------------------------
# redo 匹配器 —— v2 统一返回 redo（不带 target_node）
# ---------------------------------------------------------------------------

def _match_redo_any(text: str, current_node: str | None) -> bool:
    """v4 redo 判定（只拦完全重做 / 回退语义，微调类不拦）。

    匹配范围（统一归 redo_blocked）：
      1. bare_redo（短短语 ≤4字）："重做"、"重来"
      2. 抱怨词（≥2字，防止单字误匹配）："不满意"、"不好看"、"不太行"
      3. redo 动作词："重新生成"、"再来一张"
      4. 再试词："再试一下"、"重新试"
      5. backward 动词（"回到"/"退到"/"撤回"）—— 排除 confirm 语境

    🔴 v4 不再拦 c3 微调 / c4 改图（"换背景"/"调暗" 等）—— 这些属于微调范畴，
    让 LLM 意图判断兜底，不要提前拦截。
    """
    if not current_node:
        return False
    if _has_negation(text):
        return False
    t = _strip_tone(text).strip()

    # 1. bare_redo（短短语 ≤4字）
    if len(t) <= 4 and _fuzz_any(t, _BARE_REDO_WORDS):
        return True

    # 2. 抱怨词独立命中（≥2字）
    for w in _REDO_COMPLAINT_WORDS:
        if len(w) >= 2 and _fuzz_any(text, [w]):
            return True

    # 3. redo 动作词
    if _fuzz_any(text, _REDO_VERBS):
        return True

    # 4. 再试词
    if _fuzz_any(text, _RETRY_WORDS):
        return True

    # 5. backward 动词（"回到"/"退到"/"撤回" 等）—— 排除 confirm 语境
    if _fuzz_any(text, _BACKWARD_VERBS):
        if not _fuzz_any(text, _CONFIRM_SHORT_WORDS):
            return True

    return False


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------

def _match_confirm(text: str, current_node: str | None) -> bool:
    """confirm —— 短短语完全相等匹配（避免 WRatio 把 "不满意" 里的 "满意" 误吞）。"""
    if not current_node:
        return False
    t = _strip_tone(text).strip()
    if t.lower() in {w.lower() for w in _CONFIRM_SHORT_WORDS}:
        return True
    return False


def _match_confirm_save(text: str, current_node: str | None) -> bool:
    """confirm_generation 的 c4 专属正向词 —— 入库/保存/结束/落库/采纳等。

    这些词在 c1/c2/c3 下不出现（"保存"在 c1 下可能是 edit_and_confirm_c1），
    但在 c4 下明确指向确认入库。
    另外极短正向词（"好"/"对"/"行"/"棒"）也只在 c4 下生效——c1/c2/c3 下容易误吞。
    """
    if current_node != "c4":
        return False
    if _fuzz_any(text, _CONFIRM_SAVE_WORDS):
        return True
    # 单字/极短正向词在 c4 下额外放行
    t = _strip_tone(text).strip()
    if t in _C4_CONFIRM_SHORT:
        return True
    return False


# ---------------------------------------------------------------------------
# select_topics（仅 c2）
# ---------------------------------------------------------------------------

def _match_select_all(text: str, current_node: str | None) -> bool:
    if current_node != "c2":
        return False
    t = _strip_tone(text).strip()
    return t in _SELECT_ALL_WORDS


def _match_select_indices(text: str, current_node: str | None) -> list[int] | None:
    """匹配 c2 下用户选择哪些方案的 indices。

    v2 增加动词过滤：只有当用户意图是"选择"（选/用/要）时才抽 indices，
    如果前面有"修改语义动词"（微调/改/调整），则跳过匹配——让 Step8 或 LLM
    把它分类成 edit_and_confirm_c2，而不是 select_topics。
    """
    if current_node != "c2":
        return None

    # ── 排除修改语义：如果 text 里包含 _EDIT_VERBS 里的词，跳过 indices 匹配 ──
    for verb in _EDIT_VERBS:
        if verb in text:
            # 但要排除"就用第一个方案"里的"就"——"就"是确认词不是修改词
            if verb == "就":
                continue
            # "用"本身是选择动词，不算修改
            if verb == "换" and "换成" not in text and "换一下" not in text:
                continue
            print(f"[intent] _match_select_indices ⛔ 遇到修改动词 '{verb}'，跳过 indices 匹配", flush=True)
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
# skip / chat_outside / start_task
# ---------------------------------------------------------------------------

def _match_skip(text: str) -> bool:
    return _fuzz_any(text, _SKIP_WORDS)


def _match_chat_outside(text: str) -> bool:
    t = _strip_tone(text).strip().lower()
    if _fuzz_any(t, _CHAT_OUTSIDE_SHORT_WORDS):
        return True
    if _fuzz_any(text, _CHAT_OUTSIDE_TOPIC_WORDS):
        return True
    return False


def _match_start_task_text(text: str) -> bool:
    return _fuzz_any(text, _START_TASK_TEXT_WORDS)


# ---------------------------------------------------------------------------
# 意图 × current_node 配对合法性守卫 —— 所有返回值必经此关
# ---------------------------------------------------------------------------
# redo 守卫：所有有 current_node 的情况（c1/c2/c3/c4）都允许 redo
# —— 让前端 radio 选。finalize 后由 chat.py dispatch 层额外拦截。
#
# backward_to_nodeX 守卫 = 严格的 "target_node_num ≤ current_node_num"
# —— 只能回退到已经跑过的节点。
#
# node1 永久锁定：backward_to_node1 只能在 c1 还没确认报告时触发。
#
# redo_generation 已废弃（并入 redo）。

_INTENT_ALLOWED_CURRENT_NODES: dict[str, tuple[str, ...]] = {
    # redo：所有 cX 都允许 + 允许 current_node=None 时走 redo
    # （graph_context.py 的 state_products 兜底会尽量把 current_node 推出来，
    #  但如果真推不出来，也不要硬拦 redo——让它降级成 chat_outside 是最糟糕的 UX）
    "redo": (None, "c1", "c2", "c3", "c4"),
    # 回退到 nodeX —— target 必须 ≤ current（只能回到已经走过的节点）
    "backward_to_node1": ("c1",),        # 1 ≤ 1 且 node1 永久锁定
    "backward_to_node2": ("c2", "c3", "c4"),
    "backward_to_node3": ("c3", "c4"),
    "backward_to_node4": ("c4",),
    # confirm_current 在 c1/c2/c3 通用确认阶段
    "confirm_current": ("c1", "c2", "c3"),
    # c4 专属
    "confirm_generation": ("c4",),
    # edit / select 只在对应节点
    "edit_and_confirm_c1": ("c1",),
    "edit_and_confirm_c2": ("c2",),
    "edit_and_confirm_c3": ("c3",),
    "select_topics": ("c2",),
}


def _validate_intent_node(
    intent: str, current_node: str | None,
) -> bool:
    """校验 intent 在给定 current_node 下是否合法。

    当 allowed 表里包含 None 时，current_node=None 也被认为是合法的
    （比如 redo：虽然我们会尽量用 state 产物兜底推 current_node，
    但真推不出来时也不要硬拦）。
    """
    allowed = _INTENT_ALLOWED_CURRENT_NODES.get(intent)
    if allowed is None:
        return True
    if current_node is None:
        return None in allowed
    return current_node in allowed


def _enforce_intent_guard(
    result: dict[str, Any], current_node: str | None,
) -> dict[str, Any]:
    """对意图分类结果强制应用守卫表。

    如果 intent 在当前 current_node 下不合法，返回 blocked_result（带友好消息），
    而不是降级 chat_outside——用户应该明确知道"为什么不行"。
    """
    intent = result.get("intent", "")
    if _validate_intent_node(intent, current_node):
        return result

    # 构建给用户的友好提示 —— 带上"为什么不允许"的上下文
    _FRIENDLY_TIPS: dict[str, str] = {
        # redo 相关：current_node 决定具体原因
        "redo": f"当前在 {current_node} 阶段，"
                + {
                    "c1": "商品分析报告尚未确认，可直接在当前步骤修改或确认后继续。",
                    "c2": "商品分析报告已确认永久锁定，可回退到方案策划/提示词/生图等后续步骤重新执行。",
                    "c3": "商品分析报告已确认永久锁定，可回退到方案策划或提示词重新执行。",
                    "c4": "商品分析报告已确认永久锁定，可回退到方案策划、提示词或图像生成。",
                    None: "当前任务已结束或尚未开始，暂无法重做。如想重新生成，请开启新任务。",
                }.get(current_node or "", "当前阶段不允许此操作。"),
        # backward_to_nodeX 精确回退
        "backward_to_node1": f"商品分析报告已在 c1 阶段确认并进入方案策划，"
                             f"node1 永久锁定不可重跑。如需重做请从方案策划重新开始。",
        "backward_to_node2": f"当前在 {current_node} 阶段，"
                             f"尚未到达方案策划（第2步），无法回退。请先确认前面的步骤。",
        "backward_to_node3": f"当前在 {current_node} 阶段，"
                             f"尚未到达提示词生成（第3步），无法回退。请先确认前面的步骤。",
        "backward_to_node4": f"当前在 {current_node} 阶段，"
                             f"尚未到达图像生成（第4步），无法回退。请先确认前面的步骤。",
        # 其他
        "confirm_current": f"当前阶段（{current_node}）不支持直接确认，"
                           f"可能需要先选择方案或编辑报告后再继续。",
        "edit_and_confirm_c1": f"编辑报告仅在 c1 阶段允许，"
                               f"当前在 {current_node}，报告已确认进入后续步骤。",
        "select_topics": f"选择方案仅在 c2 阶段允许，"
                         f"当前在 {current_node}。",
    }
    reason = _FRIENDLY_TIPS.get(
        intent,
        f"操作 {intent} 在当前阶段（{current_node}）不允许。",
    )
    print(f"[intent] 🛡️ 意图守卫拦截: intent={intent} 在 current_node={current_node}，提示: {reason}",
          flush=True)
    return {
        "intent": "chat_outside",
        "reasoning": f"意图守卫拦截: {intent} 在 {current_node} 下不允许",
        "selected_indices": result.get("selected_indices"),
        "blocked_step": result.get("blocked_step"),
        "parsed_content": result.get("parsed_content"),
        "blocked": True,
        "blocked_reason": reason,
    }


# ---------------------------------------------------------------------------
# 主入口 —— 关键词优先，否则走 LLM，出口统一过守卫
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
        kw = _enforce_intent_guard(kw, current_node)
        print(f"[intent] 关键词命中 → {kw.get('intent')}", flush=True)
        return kw

    try:
        result = await _classify_via_llm(
            message,
            has_task=has_task,
            current_node=current_node,
            completed_mask=completed_mask,
            has_images=has_images,
            product_description=product_description,
        )
        return _enforce_intent_guard(result, current_node)
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

    v2 优先级设计：
      Step 0  空消息
      Step 1  方向词+第X步          —— 唯一精确定位 target_node 的路径
      Step 2  confirm               —— 短短语完全相等；c4 保存词→confirm_generation
      Step 3  redo（统一，不带 node） —— 所有 redo 类输入归 redo，前端弹 radio
      Step 4  select_topics         —— 仅 c2
      Step 5  skip_forward          —— 必须在闲聊之前拦截
      Step 6  chat_outside          —— 闲聊拦截
      Step 7  start_task 兜底
      Step 8  节点专属兜底          —— c1 自然语言→edit_and_confirm_c1；c2→None 走 LLM

    注意：redo 放到 confirm 之后 —— 避免"不满意"里的"满意"被 confirm 误吞。
    """
    text = message.strip()

    # ────────────────────────────────────── Step 0: 空消息 ──────────────────────────────────────
    if not text:
        if not has_task and has_images:
            return {"intent": "start_task", "reasoning": "空消息+有图+无任务",
                    "selected_indices": None, "blocked_step": None, "parsed_content": ""}
        if has_task and current_node:
            intent = "confirm_generation" if current_node == "c4" else "confirm_current"
            return {"intent": intent, "reasoning": "空消息+在 interrupt→默认确认",
                    "selected_indices": None, "blocked_step": None, "parsed_content": ""}
        return {"intent": "chat_outside", "reasoning": "空消息",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 1: 方向词+"第X步" ──────────────────────────────────────
    # v4：backward_to_nodeX 一律拦（不允许任何完全重做）；forward（前往/走到第X步）仍正常
    direction_step = _match_direction_step(text, current_node)
    if direction_step:
        if direction_step.startswith("backward_to_node"):
            return {"intent": "redo_blocked",
                    "reasoning": "关键词: 方向词+第X步 backward→拦截，引导用微调",
                    "selected_indices": None, "blocked_step": None, "parsed_content": None}
        return {"intent": direction_step,
                "reasoning": "关键词: 方向词+第X步,目标在当前之后→确认继续",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 2: confirm ──────────────────────────────────────
    # 先于 redo："好的/继续" 是确认不是 redo
    # 但抱怨词含"满意"（"不满意"）—— redo 在这之后，避免 confirm 误吞
    if _match_confirm(text, current_node):
        if current_node == "c4":
            return {"intent": "confirm_generation",
                    "reasoning": "关键词: c4 下确认生图结果",
                    "selected_indices": None, "blocked_step": None, "parsed_content": None}
        return {"intent": "confirm_current",
                "reasoning": "关键词: 确认当前步骤继续",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}
    if _match_confirm_save(text, current_node):
        return {"intent": "confirm_generation",
                "reasoning": "关键词: c4 下保存/结束生图",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 3: redo → 一律拦 ──────────────────────────────────────
    # v4：完全重做已禁用。所有 redo 关键词都返回 redo_blocked，引导用户用微调
    if _match_redo_any(text, current_node):
        return {"intent": "redo_blocked",
                "reasoning": "关键词: redo 触发词→拦截，引导用微调",
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
    if _match_chat_outside(text):
        return {"intent": "chat_outside",
                "reasoning": "关键词拦截: 闲聊/非商拍",
                "selected_indices": None, "blocked_step": None, "parsed_content": None}

    # ────────────────────────────────────── Step 7: start_task 兜底 ──────────────────────────────────────
    if not has_task and has_images:
        return {"intent": "start_task", "reasoning": "关键词兜底: 无 task + 有图",
                "selected_indices": None, "blocked_step": None, "parsed_content": text}
    if not has_task and _match_start_task_text(text):
        return {"intent": "start_task", "reasoning": "关键词: 纯文本+商拍相关→开始任务",
                "selected_indices": None, "blocked_step": None, "parsed_content": text}

    # ────────────────────────────────────── Step 8: 节点专属兜底 ──────────────────────────────────────
    if current_node == "c1":
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

    if current_node == "c2":
        # c2 下的自然语言修改指令 → edit_and_confirm_c2
        # 核心判断：包含修改语义动词（_EDIT_VERBS），且不包含明确的选择动词
        _C2_EDIT_SIGNAL_WORDS = [
            "微调", "改", "修改", "调整", "换成", "改成", "换一下",
            "优化", "润色", "重写", "加上", "增加", "去掉", "补充",
            "极简", "轻奢", "环保", "高级", "年轻", "商务",
        ]
        _C2_SELECT_VERBS = ["用", "选", "要", "就选", "就用", "就"]
        has_edit_verb = any(verb in text for verb in _EDIT_VERBS)
        has_edit_signal = _fuzz_any(text, _C2_EDIT_SIGNAL_WORDS)
        has_select_verb = any(verb in text for verb in _C2_SELECT_VERBS)
        # 有修改动词 → edit；有修改信号词但无选择动词 → edit；否则走LLM
        if has_edit_verb or (has_edit_signal and not has_select_verb and not _match_confirm(text, current_node)):
            return {"intent": "edit_and_confirm_c2",
                    "reasoning": "c2 节点自然语言修改指令→微调方案",
                    "selected_indices": None, "blocked_step": None, "parsed_content": text}

    if current_node == "c3":
        # c3 下的自然语言修改指令 → edit_and_confirm_c3
        _C3_EDIT_SIGNAL_WORDS = [
            "微调", "改", "修改", "调整", "换成", "改成", "换一下",
            "优化", "润色", "重写", "加上", "去掉",
            "提示词", "prompt",
            "背景", "颜色", "色调", "姿势", "风格", "构图",
        ]
        has_edit_verb = any(verb in text for verb in _EDIT_VERBS)
        has_edit_signal = _fuzz_any(text, _C3_EDIT_SIGNAL_WORDS)
        if has_edit_verb or (has_edit_signal and not _match_confirm(text, current_node)):
            return {"intent": "edit_and_confirm_c3",
                    "reasoning": "c3 节点自然语言修改指令→微调提示词",
                    "selected_indices": None, "blocked_step": None, "parsed_content": text}

    # c4 走到这里没命中任何规则 —— 交给 LLM
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
        reasoning_effort="close",
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
# 跳步检测工具（来自旧版，保留兼容）
# ---------------------------------------------------------------------------

STEP_ORDER = ["s1", "s2", "s3", "s4", "s5", "s6"]
STEP_NAMES = {
    "s1": "商品分析", "s2": "报告确认(c1)", "s3": "选题生成",
    "s4": "选题确认(c2)", "s5": "Prompt生成", "s6": "生图确认(c3/c4)",
}


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
    confirmed = graph_state.get("confirmations", {}) if graph_state else {}

    if n1.get("product_insight"):
        mask[0] = True
    if confirmed.get("c1") or n2.get("schemes") or interrupt_node in ("c2", "c3", "c4"):
        mask[1] = True
    if n2.get("schemes"):
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


# ---------------------------------------------------------------------------
# 工具：生成当前节点允许的 redo 目标选项
# ---------------------------------------------------------------------------

def build_redo_options(current_node: str | None) -> list[dict[str, str]]:
    """根据 current_node 生成允许的选项列表（供前端 radio 渲染）。

    v3 增量编辑版：node1/2/3 的"重做"统一走 refine（纯 text LLM 增量编辑），
    只有 node4（生图 API）保留完全重做。所以选项表收窄：

      c1 → [{label: "商品分析（第1步）", value: "node1"}]  — refine，1 选项
      c2 → [{label: "方案策划（第2步）", value: "node2"}]  — refine，1 选项
      c3 → [{node2, node3}]                                 — refine，2 选项
      c4 → [{label: "图像生成（第4步）", value: "node4"}]  — 唯一的完全重做选项
      None → []

    注意：node1 refine 可跨阶段生效（即使过了 c1 也能通过 refine 修改报告），
    但 build_redo_options 只反映"当前阶段合理的选项"，跨阶段 refine 由 chat.py 的
    守卫（_IS_REFINE）负责放开。
    """
    if not current_node or current_node not in _CURRENT_NODE_NUM:
        return []

    # v3: 收窄允许表
    _OPTIONS = {
        "c1": [("node1", "商品分析", 1)],
        "c2": [("node2", "方案策划", 2)],
        "c3": [("node2", "方案策划", 2), ("node3", "Prompt 生成", 3)],
        "c4": [("node4", "图像生成", 4)],  # c4 只允许 node4 完全重做
    }
    allowed = _OPTIONS.get(current_node, [])
    return [
        {"label": f"{label}（第{num}步）", "value": node}
        for node, label, num in allowed
    ]
