"""增量编辑（refine）节点 —— 替代 Node1/Node2/Node3 的完全重做。

核心思路：用户说"要改 Node1/2/3 的产出"时，不走 VLM 全量重跑（昂贵、慢、不稳定），
而是把【旧产物】+【用户修改指令】喂给纯 text LLM（走 model_pool.chat 轮询池），
LLM 做最小必要增量修改，直接产出更新后的完整产物。

调用链：
  interrupt resume(decision="refine", refine_instruction="...")
    → _cX_confirm_interrupt 识别 decision="refine"
    → 写 _refine_target + _refine_instruction 到 state
    → 路由到对应 refine 节点
    → refine 节点调用 pool.chat()
    → 路由回同一个 cX interrupt（用户再次确认）

⚠️ Node4 不做 refine —— Node4 是生图 API（openai/gpt-image-2），没有可"增量修改"的文本产物，
   所以 C4 redo→node4 仍然是完全重置 work_items 的方式，保持不变。
"""

from __future__ import annotations

import json as json_mod
import time
from typing import Any


# ---------------------------------------------------------------------------
# Node1 refine：商品识别报告 增量修改（Markdown 文本）
# ---------------------------------------------------------------------------

async def refine_node1_report(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node1 的商品识别报告做增量修改。

    输入：state.node1.product_insight（旧报告） + state._refine_instruction（用户指令）
    输出：更新后的 node1.product_insight（完整 Markdown 报告）+ report_sections（重归一化）

    🔴 锁定守卫：若 state.node1.report_locked=True，说明报告已被用户确认并锁定，
    当前任务内不得再修改 —— 直接拒绝，返回 phase=c1_confirm 且不改动 node1 任何字段。
    """
    from wellflow.app.llm.model_pool import get_model_pool
    from wellflow.app.prompt.constant import REFINE_NODE1_REPORT_SYSTEM_PROMPT
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    old_report: str = state.get("node1", {}).get("product_insight", "") or ""

    # —— 锁定守卫（第一行就查，避免已锁定后还白白调 LLM）——
    if bool((state.get("node1") or {}).get("report_locked")):
        print("[refine_node1] 🛡️ node1 报告已锁定，拒绝 refine", flush=True)
        if task_id:
            publish(task_id, "phase", {"phase": "c1_confirm"})
            publish(task_id, "message", {
                "text": (
                    "产品报告已确认并锁定，本任务内无法再修改。"
                    "如需调整商品信息，请新建任务重新生成报告。"
                ),
            })
        return {
            "phase": "c1_confirm",
            "_refine_target": None,
            "_refine_instruction": None,
        }

    if not refine_instruction:
        print("[refine_node1] ⚠️ 没有 refine_instruction，跳过编辑", flush=True)
        return {"phase": "c1_confirm"}

    if not old_report:
        print("[refine_node1] ⚠️ 旧报告为空，无法编辑", flush=True)
        return {"phase": "c1_confirm"}

    if task_id:
        publish(task_id, "phase", {"phase": "node1_refining"})

    pool = get_model_pool()

    # 多轮 refine 历史（**只取 node1 自己的**——避免 node2/node3 的 refine 指令混进来）
    from wellflow.app.workflows.state import get_node_refine_history
    refine_history: list[str] = get_node_refine_history(state, "node1")
    history_block = ""
    if len(refine_history) >= 2:
        # 倒数第一条是本轮（已经在【修改指令】里单独列），前面的才是"历史"
        prev_history = refine_history[:-1]
        history_block_lines = [f"第{i + 1}轮：{h}" for i, h in enumerate(prev_history)]
        history_block = (
            f"\n【历史 refine 指令（多轮对话中用户之前提过的修改要求，"
            f"请结合本轮指令一并理解，处理指令间的重叠/矛盾/去重）】\n"
            + "\n".join(history_block_lines)
            + "\n\n"
        )

    user_message = (
        f"【商品识别报告原文】\n{old_report}\n\n"
        f"{history_block}"
        f"【本轮修改指令】\n{refine_instruction}\n\n"
        f"请基于本轮指令（参考历史指令做去重/整合），输出更新后的完整报告。"
    )

    print(f"[refine_node1] 📤 chat → refine report (instruction_len={len(refine_instruction)})", flush=True)
    t0 = time.time()

    resp, used_model = await pool.chat(
        system=REFINE_NODE1_REPORT_SYSTEM_PROMPT,
        user=user_message,
        reasoning_effort="close",
        temperature=0.3,
    )
    raw_report: str = resp.content or ""

    # 去除可能存在的 ```markdown / ``` 包裹
    raw_report = _strip_code_fence(raw_report)

    # 拆分：报告正文 vs 引导语（LLM 按 prompt 用 ---NEXT--- 分隔）
    new_report, next_actions = _split_next_actions(raw_report)

    from wellflow.app.prompt.report_sections import build_report_sections
    new_sections = build_report_sections(new_report) if new_report else None

    total_ts = time.time() - t0
    print(f"[refine_node1] ✅ 完成: model={used_model}, "
          f"旧报告={len(old_report)}字 → 新报告={len(new_report)}字, 耗时={total_ts:.1f}s", flush=True)
    print(f"[refine_node1] 📝 新报告前300字: {new_report[:300]!r}", flush=True)

    # —— next_actions 同样不再独立 publish SSE message，改为存入 node1 state，
    #    由 parent_graph 在 C1 interrupt 时作为 hint 下发。

    return {
        "phase": "c1_confirm",
        "node1": {
            "product_insight": new_report,
            "report_sections": new_sections,
            "next_actions": next_actions,
            # 保留 VLM 缓存图、thinking 等，refine 只改文本
            "compressed_images": state.get("node1", {}).get("compressed_images", []),
            "input_analysis": state.get("node1", {}).get("input_analysis"),
            "thinking_text": state.get("node1", {}).get("thinking_text", ""),
        },
        "_refine_target": None,
        "_refine_instruction": None,
    }


# ---------------------------------------------------------------------------
# Node2 refine：商拍方案 增量修改（JSON schemes）
# ---------------------------------------------------------------------------

async def refine_node2_schemes(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node2 的商拍方案做增量修改。

    输入：state.node2.schemes（旧方案 list[dict]） + state._refine_instruction
    输出：更新后的 node2.schemes + node2.scheme_raw
    """
    from wellflow.app.llm.model_pool import get_model_pool
    from wellflow.app.prompt.constant import REFINE_NODE2_SCHEMES_SYSTEM_PROMPT
    from wellflow.app.event_bus import publish
    import re  # 用于路径2降级时正则提取 _meta 片段

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    node2_raw = state.get("node2", {}) or {}
    old_schemes: list[dict[str, Any]] = node2_raw.get("schemes", []) or []
    # 锚点：Node2 正向产出时写入，表示"本任务原始应该有几套方案"
    # 用来防御 LangGraph checkpoint 异常合并导致 schemes 被污染（如出现 3×4=12 条脏方案）
    _anchor_count = node2_raw.get("base_scheme_count")

    if not refine_instruction:
        print("[refine_node2] ⚠️ 没有 refine_instruction，跳过编辑", flush=True)
        return {"phase": "c2_select"}

    if not old_schemes:
        print("[refine_node2] ⚠️ 旧 schemes 为空，无法编辑", flush=True)
        return {"phase": "c2_select"}

    # —— 🔑 诊断 + 防御：检测 schemes 数量是否异常 ——
    # 正常：1~5 套；异常：> 锚点×2 或 > 10（明显是 checkpoint 污染）
    _EXPECTED_MAX = max(int(_anchor_count or 5) * 2, 5)
    _EXPECTED_MAX = min(_EXPECTED_MAX, 10)  # 硬封顶
    _count_anomaly = len(old_schemes) > _EXPECTED_MAX

    if _count_anomaly:
        print(
            f"[refine_node2] 🚨 schemes 数量异常！len={len(old_schemes)}, "
            f"anchor={_anchor_count}, 阈值={_EXPECTED_MAX}。"
            f"疑似 checkpoint 污染，开始诊断+清洗...",
            flush=True,
        )
        # 诊断：打印每个 scheme 的 index/name/字段完整度 摘要
        for _di, _ds in enumerate(old_schemes):
            if not isinstance(_ds, dict):
                print(f"[refine_node2]  scheme[{_di}]: 类型={type(_ds).__name__} 非 dict", flush=True)
                continue
            _si = _ds.get("scheme_index", "?")
            _sn = (_ds.get("scheme_name") or "")[:30]
            _n_keys = len(_ds.keys())
            # 粗略完整度：看顶层非空 key 的比例
            _non_empty = sum(1 for k, v in _ds.items() if not k.startswith("_") and v not in (None, "", [], {}))
            print(f"[refine_node2]  scheme[{_di}]: index={_si}, name={_sn!r}, keys={_n_keys}, 非空字段={_non_empty}", flush=True)

        # 清洗策略：
        #   1) 按 scheme_index 去重（每个 index 只留最早出现的那条）
        #   2) 剩余数量还是异常多 → 按锚点截断（优先保留 index 较小的，通常是正向产出的"正经方案"）
        _seen_idx: set[Any] = set()
        _deduped: list[dict[str, Any]] = []
        for _ds in old_schemes:
            if not isinstance(_ds, dict):
                continue
            _idx = _ds.get("scheme_index")
            if _idx is None or _idx in _seen_idx:
                continue
            _seen_idx.add(_idx)
            _deduped.append(_ds)
        _before = len(old_schemes)
        old_schemes = _deduped
        print(f"[refine_node2]  去重后剩余 {len(old_schemes)} 套（原 {_before} 套）", flush=True)

        # 二次防御：去重后如果仍 > 锚点×2（锚点存在）或 > 5（无锚点），截断
        if _anchor_count is not None and len(old_schemes) > _anchor_count * 2:
            _cut = _anchor_count
            old_schemes = old_schemes[:_cut]
            print(f"[refine_node2]  锚点防御：截断到锚点 {_anchor_count} 套", flush=True)
        elif len(old_schemes) > 5:
            old_schemes = old_schemes[:5]
            print(f"[refine_node2]  无锚点兜底：截断到前 5 套", flush=True)
        print(f"[refine_node2]  ✅ 清洗完成，最终 schemes={len(old_schemes)} 套", flush=True)

    # ---- 按意图分类器返回的 selected_indices 过滤 old_schemes ----
    # 用户明确点名了哪几套（fusion / 指定方案序号修改）→ 只传被点名的子集给 LLM
    # "all" / None / 空列表 / 格式异常 → 兜底全传
    _sel_raw = state.get("_refine_selected_indices")
    _sel_indices = _parse_refine_selected_indices(_sel_raw, len(old_schemes))
    _subset_context_note = ""  # 只有真子集时才填，用来告诉 LLM 它拿到的是"原方案里的哪几套"
    if _sel_indices is not None and len(_sel_indices) < len(old_schemes):
        # 用户确实点名了一个真子集 —— 只把子集喂给 refine LLM
        _orig_picked = sorted(_sel_indices)  # 原 index，比如 [0, 2]
        _filtered = [s for i, s in enumerate(old_schemes) if i in _sel_indices]
        # 🔴 过滤后对子集 scheme_index 重编号为 0..N-1，避免 LLM 看到跳跃的 index（0,2）
        for _new_idx, _s in enumerate(_filtered):
            if isinstance(_s, dict):
                _s["scheme_index"] = _new_idx
        # 给 LLM 的显式映射提示：你收到的子集里第 0 套 = 原方案的第 1 套（1-based）
        _mapping_lines = [
            f"  - 子集第{i + 1}套（scheme_index={i}）= 原方案第{_orig_picked[i] + 1}套"
            for i in range(len(_orig_picked))
        ]
        _subset_context_note = (
            f"\n【输入方案来源说明】你收到的 schemes 不是全部方案，而是从原 {len(old_schemes)} 套中"
            f"按意图分类器挑出来的 {len(_filtered)} 套子集。映射关系：\n"
            + "\n".join(_mapping_lines)
            + "\n请直接操作这些子集即可，不要假设还有其他方案存在。\n"
        )
        print(
            f"[refine_node2] 🎯 按意图分类器过滤：原 {len(old_schemes)} 套 → "
            f"只传 {len(_filtered)} 套 (orig_indices={_orig_picked}) 给 refine LLM",
            flush=True,
        )
        old_schemes = _filtered
    else:
        print(f"[refine_node2] ℹ️ selected_indices={_sel_raw} → 全部 {len(old_schemes)} 套都传", flush=True)

    if task_id:
        publish(task_id, "phase", {"phase": "node2_refining"})

    pool = get_model_pool()
    # —— 喂 LLM 前先剥掉内部标记字段，只保留完整商拍方案（12 维）——
    # 完整方案字段绝对不能压缩/裁剪，否则 LLM refine 时会丢失方案定位、视觉主题、
    # 场景设定、模特气质、光影风格等关键维度的上下文
    _clean_old = _strip_internal_keys(old_schemes)
    old_json = json_mod.dumps({"schemes": _clean_old}, ensure_ascii=False, indent=2)

    # 多轮 refine 历史（**只取 node2 自己的**——避免 node1/node3 的 refine 指令混进来）
    from wellflow.app.workflows.state import get_node_refine_history
    refine_history: list[str] = get_node_refine_history(state, "node2")
    history_block = ""
    if len(refine_history) >= 2:
        prev_history = refine_history[:-1]
        history_block_lines = [f"第{i + 1}轮：{h}" for i, h in enumerate(prev_history)]
        history_block = (
            f"\n【历史 refine 指令（多轮对话中用户之前提过的修改要求，"
            f"请结合本轮指令一并理解，处理指令间的重叠/矛盾/去重）】\n"
            + "\n".join(history_block_lines)
            + "\n\n"
        )

    user_message = (
        f"【原商拍方案 JSON】\n{old_json}\n\n"
        f"{_subset_context_note}"
        f"{history_block}"
        f"【本轮修改指令】\n{refine_instruction}\n\n"
        f"请基于本轮指令（参考历史指令做去重/整合），输出更新后的完整 JSON。"
    )

    print(f"[refine_node2] 📤 chat → refine schemes (instruction_len={len(refine_instruction)})", flush=True)
    t0 = time.time()

    resp, used_model = await pool.chat(
        system=REFINE_NODE2_SCHEMES_SYSTEM_PROMPT,
        user=user_message,
        reasoning_effort="close",
        temperature=0.3,
    )
    raw_text: str = resp.content or ""
    raw_text = _strip_code_fence(raw_text)

    print(f"[refine_node2] 📥 LLM raw_text=\n{raw_text[:2000]}", flush=True)

    # 尝试解析 JSON —— 三级降级：完整 JSON → trailing comma 修复 → 逐 scheme 解析
    new_schemes = old_schemes  # 最终兜底还是旧方案
    _parsed_ok = False
    _llm_meta: dict[str, Any] | None = None  # LLM 显式声明的意图判断（_meta 字段）

    # 路径 1：完整 JSON 解析（_extract_json 内部已含 trailing comma 修复）
    try:
        parsed = _extract_json(raw_text)
        _raw_schemes = parsed.get("schemes", []) or []
        # 提取 LLM 的显式意图声明（如果有的话）
        _llm_meta = parsed.get("_meta") if isinstance(parsed.get("_meta"), dict) else None
        if isinstance(_raw_schemes, list) and _raw_schemes:
            new_schemes = _raw_schemes
            _parsed_ok = True
    except Exception as exc:
        print(f"[refine_node2] ⚠️ 完整 JSON 解析失败: {exc}", flush=True)

    # 路径 2：逐 scheme 单独解析，跳过损坏的单个 scheme
    #         同时尝试用正则提取 _meta（路径 1 完整 JSON 解析失败但 _meta 片段本身合法的情况）
    if not _parsed_ok:
        _indiv = _extract_schemes_individually(raw_text)
        if _indiv is not None:
            new_schemes = _indiv
            _parsed_ok = True
        # 尝试用正则单独提取 _meta JSON 片段作为补偿
        if _llm_meta is None:
            _meta_match = re.search(r'"_meta"\s*:\s*(\{.*?\})\s*,?\s*"schemes"', raw_text, re.DOTALL)
            if _meta_match:
                try:
                    _llm_meta = json_mod.loads(_meta_match.group(1))
                    print(f"[refine_node2] 🧩 路径2：从 raw_text 正则提取到 _meta", flush=True)
                except Exception:
                    pass

    if not _parsed_ok:
        print(f"[refine_node2] ❌ 所有降级都失败，保留旧方案（{len(old_schemes)} 套）", flush=True)

    # 最终校验：解析出的 schemes 非空才替换；否则保留旧方案
    if not isinstance(new_schemes, list) or not new_schemes:
        print(f"[refine_node2] ⚠️ 解析结果为空，保留旧方案", flush=True)
        new_schemes = old_schemes

    # —— 方案数量校验 / 修正（三层防线，从上到下优先用更精确的判定）——
    #
    # 🔑 第一层（最精确）：LLM 在 _meta.output_count 里显式声明了它想输出几套
    #     信任 LLM 的意图判断，但如果它自己声明 output_count=N 却输出了 M 套，
    #     说明 LLM 没按自己说的做 → 截断到 N 套
    if _llm_meta:
        _declared_count = _llm_meta.get("output_count")
        _intent_type = _llm_meta.get("intent_type", "?")
        _reasoning = _llm_meta.get("reasoning", "")[:100]
        print(
            f"[refine_node2] 🧠 LLM 显式意图: intent={_intent_type}, "
            f"declared_count={_declared_count}, actual={len(new_schemes)}, "
            f"reasoning='{_reasoning}'",
            flush=True,
        )
        if isinstance(_declared_count, int) and _declared_count > 0:
            _actual_before = len(new_schemes)
            if _actual_before > _declared_count:
                # —— 截断策略：fusion 类尽量保留含"融合"标识的方案，
                #    非 fusion 类直接取前 N 套 ——
                _is_fusion_declared = (_intent_type == "fusion")
                _to_keep = _declared_count

                if _is_fusion_declared and _to_keep == 1:
                    # fusion + 只要 1 套 → 优先挑含"融合/整合/混搭"标识的方案，
                    # 找不到再退化成取前 1 套
                    _FUSION_MARKERS = ("融合", "整合", "混搭", "合并", "新方案", "方案融合")
                    _fusion_schemes = [
                        s for s in new_schemes
                        if isinstance(s, dict) and any(
                            mk in (s.get("scheme_name") or "") for mk in _FUSION_MARKERS
                        )
                    ]
                    if _fusion_schemes:
                        new_schemes = [_fusion_schemes[0]]
                        print(
                            f"[refine_node2] 🛡️ fusion 类：从 {len(_fusion_schemes)} 个含融合标识的方案中保留 1 套"
                            f"（原 {_actual_before} 套）",
                            flush=True,
                        )
                    else:
                        new_schemes = new_schemes[:_to_keep]
                        print(
                            f"[refine_node2] 🛡️ fusion 类：未找到融合标识方案，保守保留前 1 套"
                            f"（原 {_actual_before} 套）",
                            flush=True,
                        )
                else:
                    new_schemes = new_schemes[:_to_keep]
                    print(
                        f"[refine_node2] 🛡️ LLM 声明 output_count={_declared_count} 但实际输出了 "
                        f"{_actual_before} 套 → 截断到 {_to_keep} 套",
                        flush=True,
                    )
            elif _actual_before < _declared_count:
                # LLM 说要输出 N 套但只输出了 M 套 → 比较少见，保留 M 套（宁少勿错）
                print(
                    f"[refine_node2] ⚠️ LLM 声明 output_count={_declared_count} 但只输出了 "
                    f"{len(new_schemes)} 套，保留实际数量",
                    flush=True,
                )

    # 第二层（关键词兜底）：LLM 没输出 _meta（路径2 或旧模型），
    #     用关键词检测做保守的融合类兜底 —— 命中融合关键词且输出 >1 套 → 只留第 1 套
    #     宁可漏判也别误杀 batch_modify 场景（所以只识别强信号关键词）
    else:
        _FUSION_KEYWORDS = ("融合", "合并", "混搭", "重组")
        if refine_instruction and any(kw in refine_instruction for kw in _FUSION_KEYWORDS) \
                and len(new_schemes) > 1:
            print(
                f"[refine_node2] 🛡️ （关键词兜底）融合类指令但输出了 {len(new_schemes)} 套 → "
                f"只保留第 1 套",
                flush=True,
            )
            new_schemes = [new_schemes[0]]

    # —— scheme_index 通用重归一化 ——
    # LLM 返回时可能残留旧 index（如融合方案带着 2 或 3），统一按 0..N-1 重编号
    if isinstance(new_schemes, list):
        for _i, _s in enumerate(new_schemes):
            if isinstance(_s, dict):
                _s["scheme_index"] = _i

    total_ts = time.time() - t0
    print(f"[refine_node2] ✅ 完成: model={used_model}, "
          f"旧 schemes={len(old_schemes)} → 新 schemes={len(new_schemes)}, 耗时={total_ts:.1f}s", flush=True)

    node2_state = state.get("node2", {})

    # —— selected_scheme_indices / per_scheme_count 重置策略 ——
    # 方案数量变了（增删/融合）→ 旧索引全失效，清空让用户到 C2 重新选
    # 方案数量没变（rule #2 单套/多套同字段修改）→ 保留用户之前选的索引
    _n_schemes_changed = len(new_schemes) != len(old_schemes)
    if _n_schemes_changed:
        print(
            f"[refine_node2] 🔄 方案数变化 {len(old_schemes)}→{len(new_schemes)}，"
            f"清空 selected_scheme_indices / per_scheme_count 让用户重新选择",
            flush=True,
        )
        _selected_indices: list[int] = []
        _per_count: list[int] = []
    else:
        _selected_indices = list(node2_state.get("selected_scheme_indices", []) or [])
        _per_count = list(node2_state.get("per_scheme_count", []) or [])
        # 额外防御：过滤掉越界的旧索引
        _selected_indices = [i for i in _selected_indices if 0 <= i < len(new_schemes)]

    # —— 对 LLM 返回的 new_schemes 剥掉内部标记字段，再写入 state ——
    # 保证前端看到的永远是完整商拍方案（方案定位、视觉主题、场景设定、
    # 模特气质、光影风格等 12 维），无任何内部校验/降级标记
    new_schemes = _strip_internal_keys(new_schemes)

    # —— schema 对齐兜底（双重保险）——
    # 让 old_schemes 成为"合法字段白名单"模板，递归把 new_schemes 里
    # 任何 LLM 臆造的 schema 外字段剥掉（比如 LLM 乱加的 model.gender）
    # 注意：_template 必须是 list（和 new_schemes 同类型），_align_to_schema
    # 内部会自动用 template[0]（单个 scheme dict）对齐每个元素
    if _clean_old:
        _template = _clean_old  # 必须传整个 list，不能取 [0] 否则类型不匹配
        _pre_count = sum(1 for s in new_schemes if isinstance(s, dict))
        new_schemes = _align_to_schema(new_schemes, _template)
        _post_count = sum(1 for s in new_schemes if isinstance(s, dict))
        if _pre_count != _post_count:
            print(f"[refine_node2] ⚠️ schema 对齐后数量变化 {_pre_count}→{_post_count}", flush=True)
        else:
            print(f"[refine_node2] 🛡️ schema 对齐完成（{_pre_count} 套）", flush=True)

    _scheme_raw_clean = json_mod.dumps({"schemes": new_schemes}, ensure_ascii=False, indent=2)

    # refine 后锚点不变（锚点代表"本任务原始应该有几套方案"，与 refine 增量编辑无关）
    _final_anchor = _anchor_count if isinstance(_anchor_count, int) else len(new_schemes)

    return {
        "phase": "c2_select",
        "node2": {
            "schemes": new_schemes,
            "scheme_raw": _scheme_raw_clean,
            "selected_scheme_indices": _selected_indices,
            "per_scheme_count": _per_count,
            "thinking_text": "",
            "base_scheme_count": _final_anchor,  # 🛑 保留锚点，防止后续 checkpoint 合并污染
        },
        "_refine_target": None,
        "_refine_instruction": None,
        "_refine_selected_indices": None,
    }

 
# ---------------------------------------------------------------------------
# Node3 refine：提示词 增量修改（自然语言 prompt 列表）
# ---------------------------------------------------------------------------

async def refine_node3_prompts(state: dict[str, Any]) -> dict[str, Any]:
    """基于用户指令对 Node3 的生图提示词做增量修改。

    输入：state.node3.generate_prompts（旧 prompt 列表） + state._refine_instruction
    输出：更新后的 node3.generate_prompts（新 prompt 列表）
    """
    from wellflow.app.llm.model_pool import get_model_pool
    from wellflow.app.prompt.constant import REFINE_NODE3_PROMPTS_SYSTEM_PROMPT
    from wellflow.app.event_bus import publish

    task_id = state.get("task_id", "")
    refine_instruction = state.get("_refine_instruction", "").strip()
    old_prompts: list[str] = state.get("node3", {}).get("generate_prompts", []) or []

    if not refine_instruction:
        print("[refine_node3] ⚠️ 没有 refine_instruction，跳过编辑", flush=True)
        return {"phase": "c3_confirm"}

    if not old_prompts:
        print("[refine_node3] ⚠️ 旧 prompts 为空，无法编辑", flush=True)
        return {"phase": "c3_confirm"}

    if task_id:
        publish(task_id, "phase", {"phase": "node3_refining"})

    pool = get_model_pool()
    # 给每条旧 prompt 加 [i] 序号前缀，让 LLM 清楚知道边界和总数
    _numbered_old = [f"[{i + 1}] {p}" for i, p in enumerate(old_prompts)]
    old_prompts_text = "\n---PROMPT_SEP---\n".join(_numbered_old)
    n_total = len(old_prompts)

    # 多轮 refine 历史（**只取 node3 自己的**——避免 node1/node2 的 refine 指令混进来）
    from wellflow.app.workflows.state import get_node_refine_history
    refine_history: list[str] = get_node_refine_history(state, "node3")
    history_block = ""
    if len(refine_history) >= 2:
        prev_history = refine_history[:-1]
        history_block_lines = [f"第{i + 1}轮：{h}" for i, h in enumerate(prev_history)]
        history_block = (
            f"\n【历史 refine 指令（多轮对话中用户之前提过的修改要求，"
            f"请结合本轮指令一并理解，处理指令间的重叠/矛盾/去重）】\n"
            + "\n".join(history_block_lines)
            + "\n\n"
        )

    user_message = (
        f"【原生图提示词列表】（共 {n_total} 条，每条带序号前缀 [i]）\n"
        f"{old_prompts_text}\n\n"
        f"{history_block}"
        f"【本轮修改指令】\n{refine_instruction}\n\n"
        f"请基于本轮指令（参考历史指令做去重/整合），输出更新后的完整提示词列表。\n"
        f"🔴 条数约束：**当前共 {n_total} 条 prompt，请输出恰好 {n_total} 条**。\n"
        f"🔴 输出格式：每条 prompt 单独一段，用 ---PROMPT_SEP--- 分隔。\n"
        f"🔴 未被指令提及的那条必须原封不动复制返回，一字不改。"
    )

    print(f"[refine_node3] 📤 chat → refine prompts (instruction_len={len(refine_instruction)})", flush=True)
    t0 = time.time()

    resp, used_model = await pool.chat(
        system=REFINE_NODE3_PROMPTS_SYSTEM_PROMPT,
        user=user_message,
        reasoning_effort="close",
        temperature=0.3,
    )
    raw_text: str = resp.content or ""
    raw_text = _strip_code_fence(raw_text)

    # 按 "---PROMPT_SEP---" 切分 → 去空 → 清洗
    new_prompts = [p.strip() for p in raw_text.split("---PROMPT_SEP---") if p.strip()]
    # 兜底：如果 LLM 没按分隔符返回（只返回了一个大段），就把整段当作一条
    if not new_prompts:
        new_prompts = [raw_text.strip()] if raw_text.strip() else old_prompts

    # LLM 输出可能也带 [i] 序号前缀（因为喂的时候带了）——统一剥掉
    import re as _re
    _idx_prefix = _re.compile(r"^\s*\[\d+\]\s*")
    new_prompts = [_idx_prefix.sub("", p).strip() for p in new_prompts]

    # ---- 🔴 代码层硬兜底：条数强制对齐 ----
    # 判断用户指令是否明确要求增删 —— 只有用户明确说了才允许条数变化
    _DELETE_KWS = ("删除", "删掉", "去掉", "移除", "减掉", "少一条", "少一条", "删一条", "删第")
    _ADD_KWS = ("增加", "添加", "新增", "加一条", "多一条", "补充一条", "再来一条", "补一条")
    _user_wants_change_quantity = any(kw in refine_instruction for kw in _DELETE_KWS + _ADD_KWS)

    if not _user_wants_change_quantity and len(new_prompts) != n_total:
        print(
            f"[refine_node3] 🛡️ 触发条数兜底：用户未提增删，"
            f"LLM 返回 {len(new_prompts)} 条 ≠ 原 {n_total} 条 → 强制对齐",
            flush=True,
        )
        # LLM 返回的前 M 条按顺序贴到旧列表前 M 个位置，剩余位置用旧内容原封不动回填
        _merged = list(old_prompts)   # 先复制旧列表作底稿
        for _i in range(min(len(new_prompts), n_total)):
            _merged[_i] = new_prompts[_i]
        new_prompts = _merged
        print(f"[refine_node3]  ✅ 对齐完成 → 最终 {len(new_prompts)} 条", flush=True)
    elif _user_wants_change_quantity and len(new_prompts) != n_total:
        print(
            f"[refine_node3] ℹ️ 用户明确要求增删({len(new_prompts)}→{len(old_prompts)})，"
            f"接受 LLM 返回条数变化",
            flush=True,
        )
    elif abs(len(new_prompts) - n_total) > 3:
        print(f"[refine_node3] ⚠️ 返回条数变化较大: {n_total} → {len(new_prompts)}", flush=True)

    total_ts = time.time() - t0
    print(f"[refine_node3] ✅ 完成: model={used_model}, "
          f"旧 prompts={len(old_prompts)} → 新 prompts={len(new_prompts)}, 耗时={total_ts:.1f}s", flush=True)

    # 更新 prompts_detail：保持 scheme_index/variant_index 等元信息，只替换 prompt 内容
    old_details: list[dict[str, Any]] = state.get("node3", {}).get("prompts_detail", []) or []
    new_details = _rebuild_prompts_detail(old_details, new_prompts)

    node3_state = state.get("node3", {})
    return {
        "phase": "c3_confirm",
        "node3": {
            "model_images": node3_state.get("model_images", []),
            "ratio": node3_state.get("ratio"),
            "image_model": node3_state.get("image_model"),
            "generate_prompts": new_prompts,
            "prompts_detail": new_details,
            "prompt_raw": "\n---\n".join(new_prompts),
            "per_prompt_size": node3_state.get("per_prompt_size", ["3:4"] * len(new_prompts)),
            "thinking_text": "",
        },
        "_refine_target": None,
        "_refine_instruction": None,
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _strip_code_fence(text: str) -> str:
    """去掉 LLM 返回中可能带的 ```xxx ``` 包裹。"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉第一行 ```json / ```markdown / ```
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        else:
            t = t[3:]
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t


def _split_next_actions(raw: str) -> tuple[str, str]:
    """按 ---NEXT--- 把 LLM 返回拆成 (正文, 引导语)。

    找不到分隔符时完整 raw 当正文，引导语返回空串（调用方走 hardcoded 兜底）。
    """
    if not raw:
        return "", ""
    marker = "---NEXT---"
    idx = raw.rfind(marker)
    if idx == -1:
        return raw.strip(), ""
    body = raw[:idx].rstrip()
    actions = raw[idx + len(marker):].strip()
    return body, actions


def _extract_json(text: str) -> dict[str, Any]:
    """从可能带前后噪声的文本里提取第一个完整 JSON 对象。

    三级降级：
      1. 直接解析
      2. 取第一个 { 到最后一个 } 之间
      3. trailing comma 修复（LLM 常见错误：数组/对象末尾多了一个逗号）
    """
    import re
    text = text.strip()

    def _try_load(t: str) -> dict[str, Any] | None:
        """尝试 json.loads，失败返回 None。"""
        try:
            return json_mod.loads(t)
        except Exception:
            return None

    def _fix_trailing_commas(t: str) -> str:
        """去掉 JSON 里数组/对象末尾的多余逗号（最常见的 LLM 输出错误）。

        例子：
          {"schemes": [{"a": 1,}, {"b": 2},]} → {"schemes": [{"a": 1}, {"b": 2}]}
        """
        # 去除 ,] 和 ,} 之间的逗号——因为 ]/} 一定不会被字符串里的字符误匹配
        # （字符串里的逗号后面不会紧跟 ]/}，只会紧跟非 ]/} 字符或字符串结束）
        fixed = re.sub(r',\s*([\]\}])', r'\1', t)
        return fixed

    # 1. 直接解析
    if r := _try_load(text):
        return r

    # 2. 取第一个 { 到最后一个 } 之间
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        sub = text[first:last + 1]
        if r := _try_load(sub):
            return r

        # 3. trailing comma 修复 + 截取
        fixed = _fix_trailing_commas(sub)
        if r := _try_load(fixed):
            return r

    raise ValueError("JSON 提取失败（已尝试直接解析、截取、trailing comma 修复）")


def _extract_schemes_individually(raw_text: str) -> list[dict[str, Any]] | None:
    """当整个 JSON 修复后仍解析失败时，尝试把每个 scheme 对象单独抽出来解析。

    扫描 raw_text 里顶层 schemes 数组，逐对大括号匹配抽出每个 scheme JSON，
    逐个解析成功后组装成 list。
    """
    import re

    # 先尝试定位 "schemes" 数组的起始位置
    m = re.search(r'"schemes"\s*:\s*\[', raw_text)
    if not m:
        return None

    start = m.end()
    schemes: list[dict[str, Any]] = []
    depth = 0
    brace_start = -1

    i = start
    while i < len(raw_text):
        ch = raw_text[i]
        # 简单跳过字符串内容（避免把字符串里的 { } 当作结构）
        if ch == '"':
            i += 1
            while i < len(raw_text):
                if raw_text[i] == '\\' and i + 1 < len(raw_text):
                    i += 2
                    continue
                if raw_text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == '{':
            if depth == 0:
                brace_start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and brace_start != -1:
                scheme_text = raw_text[brace_start:i + 1]
                brace_start = -1
                # 尝试解析这一个 scheme
                try:
                    scheme = json_mod.loads(scheme_text)
                    if isinstance(scheme, dict):
                        schemes.append(scheme)
                except Exception:
                    # 单个 scheme 也解析失败就跳过——宁可少一套也不保留坏数据
                    pass
        i += 1

    if schemes:
        print(f"[refine_json_fallback] ✅ 逐 scheme 解析成功: {len(schemes)} 套", flush=True)
        return schemes
    return None


def _rebuild_prompts_detail(old_details: list[dict[str, Any]], new_prompts: list[str]) -> list[dict[str, Any]]:
    """用新 prompt 文本替换 old_details 里的 prompt 字段，保留 scheme_index/variant_index 等元信息。

    如果新 prompts 数量 ≠ old_details 数量，按短者取；多余的 prompt 用占位 detail。
    """
    result: list[dict[str, Any]] = []
    n = min(len(old_details), len(new_prompts))
    for i in range(n):
        detail = dict(old_details[i]) if isinstance(old_details[i], dict) else {}
        detail["prompt"] = new_prompts[i]
        # negative_prompt / prompt_detail 可能没有（refine 后不再解析 JSON 结构）
        detail.pop("negative_prompt", None)
        detail.pop("prompt_detail", None)
        detail["elapsed"] = 0.0
        result.append(detail)

    # 如果新 prompts 数量比 old_details 多，补占位 detail
    for i in range(n, len(new_prompts)):
        result.append({
            "scheme_index": -1,
            "scheme_name": "refine_added",
            "variant_index": 0,
            "variant_total": 1,
            "prompt": new_prompts[i],
            "elapsed": 0.0,
        })

    return result


def _strip_internal_keys(schemes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从商拍方案中剥离所有内部标记字段，只保留完整方案（12 维）。

    内部标记一律以 `_` 开头，包括但不限于：
      - `_placeholder: true` — Node2 VLM 返回方案数不够时的补位占位
      - `_extracted_by_regex: true` — VLM JSON 全坏时正则降级的标记
      - `_meta` — refine 时 LLM 显式声明的意图（intent_type / output_count / reasoning）

    这些字段是后端校验用的，绝对不能给前端看，也不能喂给 refine LLM。
    完整商拍方案字段（scheme_index, scheme_name, scheme_positioning,
    visual_theme, scene_setting, model_profile, lighting_style, target_audience,
    color_palette, composition_structure, prop_selection, post_processing_direction,
    emotional_tone）**一个都不裁剪、一个都不压缩**。
    """
    cleaned: list[dict[str, Any]] = []
    for s in schemes:
        if not isinstance(s, dict):
            continue
        clean = {k: v for k, v in s.items() if not k.startswith("_")}
        cleaned.append(clean)
    return cleaned


def _align_to_schema(
    data: Any, template: Any
) -> Any:
    """以 template 为白名单，递归剥掉 data 中 schema 外的 key。

    - template 是 dict → data 也必须是 dict，按 template 的 key 集合过滤；
      template 的某个值若还是 dict / list，递归对齐。
    - template 是 list → data 也当 list 处理，对 list 内每个元素：
      如果 template[0] 存在且是 dict，按 template[0] 当模板对齐；
      否则原样保留。
    - 其他类型（str/int/float/bool/None）→ 原样返回。

    用途：LLM refine 时偶尔会臆造 schema 外字段（如 model.gender），
    用 old_schemes[0] 当模板把这些脏字段剥掉，防止透传到前端。
    """
    if isinstance(template, dict):
        if not isinstance(data, dict):
            # data 类型都不对了，保守兜底：如果 template 非空，返回 template 原结构
            print(f"[align_to_schema] ⚠️ 类型不匹配: template=dict data={type(data).__name__}，兜底返回 template 结构", flush=True)
            return {k: _align_to_schema(v, v) for k, v in template.items()}
        aligned = {}
        for k, v in data.items():
            if k not in template:
                continue  # schema 外字段 → 直接扔掉
            aligned[k] = _align_to_schema(v, template[k])
        return aligned
    if isinstance(template, list):
        if not isinstance(data, list):
            print(f"[align_to_schema] ⚠️ 类型不匹配: template=list data={type(data).__name__}，兜底返回 []", flush=True)
            return []
        # 找一个模板元素当参考
        _ref = template[0] if template else None
        if isinstance(_ref, dict):
            return [_align_to_schema(item, _ref) for item in data]
        return data  # primitive list（如 visual_keywords）原样保留
    return data  # primitive value 原样返回


def _parse_refine_selected_indices(
    raw: Any, total_schemes: int
) -> set[int] | None:
    """把 LLM 意图分类器返回的 selected_indices 解析成合法的 int 索引集合。

    返回值：
      - set[int] —— 合法且非空的索引集合，调用方据此过滤 old_schemes
      - None —— 以下任何一种兜底情况（让调用方走"全部传"路径）：
          * raw 是 None / "all" / "" / "null"
          * 解析后为空集
          * 解析后所有索引都越界（和 total_schemes 不相交）
          * 类型完全不认识

    🔴 格式兼容：LLM 可能返回
      * ["0", "1"]  (字符串索引)
      * [0, 1]      (int 索引)
      * "all"       (全部)
      * null / None (没填)
      * 甚至可能漏引号写成 [0, 1]
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        _lo = raw.strip().lower()
        if _lo in ("", "all", "null", "none"):
            return None
        # 单数字字符串 → 只有一套被选中
        if _lo.isdigit():
            raw = [int(_lo)]
        else:
            # 格式异常（比如 LLM 输出了一段描述）→ 兜底全传
            return None

    if not isinstance(raw, list) or not raw:
        return None

    indices: set[int] = set()
    for item in raw:
        try:
            indices.add(int(item))
        except (TypeError, ValueError):
            # 单个异常项跳过，不整体失败
            continue

    if not indices:
        return None

    # 越界过滤：只保留合法索引；如果过滤后全空 → 也兜底全传
    valid = {i for i in indices if 0 <= i < total_schemes}
    if not valid:
        return None
    return valid
