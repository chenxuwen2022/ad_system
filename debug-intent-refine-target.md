# Debug Session: intent-refine-target

## 症状 (Symptom)
- Conversation ID: `6589fc16-0c34-4aa2-8eab-ea82145b4108`
- Task ID: `5f0414cf4650`
- 当前阶段：C2（Node2 商拍方案已完成，Node1 报告已确认锁定）
- 用户输入：`修改报告：补充品牌调性`
- **实际行为**：系统没有拦截，而是尝试执行了 Node2
- **期望行为**：应识别到"报告"=node1 产物，且 node1 已锁定 → 拦截并返回"产品报告已确认并锁定"

## 假设 (Hypotheses)

### H1: LLM 分类器误判 refine_target=node2（最可能）
- **机制**：当前在 c2 阶段，LLM 受 `current_node=c2` 上下文干扰
- "报告"这个词没出现在 CLASSIFIER_SYSTEM 的 node1 关键词表里（只有"商品识别报告"）
- "品牌调性"虽然在 node1 表里，但 LLM 可能错误把它关联到了 node2 的"商拍方案"
- 结果：`intent=edit, refine_target=node2` → 绕过 node1 锁定守卫 → 走 node2_refine_schemes

### H2: LLM 分类器返回 refine_target=null，dispatch 兜底到当前 cX 对应 node
- **机制**：intent_classifier.py line 238-239 的 fallback 逻辑
- LLM 没给 refine_target → fallback `c2 → node2`
- 结果：`intent=edit, refine_target=node2` → 同上

### H3: graph_state 里 node1.report_locked 实际上是 False
- **机制**：C1 确认时锁定字段没正确写入，或 checkpoint 丢失
- 即使 LLM 正确返回 `refine_target=node1`，锁定守卫（chat.py line 514-528）不会触发
- 结果：走了 node1_refine_report，但 refine_node1_report 里有第二道锁定守卫（refine_nodes.py line 48）
- 如果 refine 节点也没拦截，那说明 refine_node1_report 也漏了

### H4: graph_state_brief 里没正确标记 node1 已锁定
- **机制**：summarize_graph_state 里 `report_locked` 检测失败
- LLM 没看到"已锁定，不可修改"标签，所以即使判对了 target 也不阻止 edit
- 但这条路径上 chat.py 有硬编码的锁定守卫，所以 H4 不会导致"执行错误 node"的结果

### H5: LLM 分类器返回了完全不同的 intent（confirm_current/select_topics/redo_blocked）
- **机制**：LLM 没理解"修改报告：补充品牌调性"这个句式
- 比如把"修改报告"误判为 redo_blocked，或干脆判成 chat_outside → 然后 fallback 到 select_topics → 走 Node3
- 但这和"执行 Node2"的症状不太吻合

## 验证方法
1. 看后端日志中 `[intent]` 那一行，找到具体返回了什么 intent + refine_target + reasoning
2. 看 `[chat]` 相关日志，确认锁定守卫是否触发
3. 看 `graph_state_brief` 是否包含"已锁定，不可修改"标签
4. 可模拟调用 classify(message="修改报告：补充品牌调性", current_node="c2") 复现

## 已知修复方向（等待证据确认）
- **修复 A**：在 CLASSIFIER_SYSTEM 的 node1 关键词表显式添加"报告"关键词
- **修复 B**：在 chat.py 的锁定守卫附近增加日志/防御性 fallback
- **修复 C**：在 dispatch 层对 refine_target 做二次校验（比如 target=node1 但在 c2/c3/c4 且已锁定 → 强制拦截）
