# P3 · FactQA 引擎 Spec：多轮对话 + 溯源聚合 + 会话/项目

## 1. Goal / Non-Goals
**Goal**：多轮对话式事实问答（可钻取、查不到就澄清、绝不编造）+ 会话/项目记录（复现与审计）。
**Non-Goals**：不做开放域闲聊；不做长报告生成。

## 2. 契约
- `conversation/intent.py`：factual/risk/mixed 路由（LLM + 关键词 + 可人工纠正）。
- `factqa/planner.py`：问题 + 上下文约束 -> 子查询计划（类型/数据源/参数）；歧义 -> 澄清计划。
- `factqa/executors/`：sql / report / doc / external 四类执行器（返回结果 + TraceRef）。
- `factqa/aggregator.py`：去重 / 冲突检测（不同源数值不一致 -> 分歧展示 + 各自出处）/ 口径对齐（时点/单位/币种/范围）。
- `factqa/answerer.py`：答案文本（数值必须取自结果集，模板化渲染）+ trace_refs + 置信；clarification 语义。
- `conversation/store.py`：项目 -> 会话 -> 消息（约束/trace/模型快照/成本）持久化（见 DATA_MODEL）。
- SSE：answer.token / answer.done / clarification.required。

## 3. 测试计划
1. intent：三型路由；西游记题（第 80 难 -> factual；推断第 81 难 -> risk）。
2. planner：约束继承（时间/区域/产品线）；歧义 -> 澄清计划。
3. executors：四类返回值 + TraceRef 正确性（FakeLLM + 内存库）。
4. aggregator：去重；冲突分歧输出；单位/币种归一。
5. answerer：数值与结果集一致断言（契约）；trace_refs 非空；无证据 -> 澄清。
6. conversation：钻取约束继承；无关问题不继承；上下文过期；模型快照留痕；成本累计。
7. 集成（FakeLLM）：完整问答流 + 3 轮钻取；查不到 -> 澄清不编造（fabrication 断言）。
8. 真实运行：西游记 10 问（含钻取 3 轮）+ 1 个含图问题（VISION 通道）。

## 4. 验收标准
- [ ] 契约强制测试绿（数值绑溯源；无证据 -> 澄清）；fabrication 抽检为 0。
- [ ] 多轮钻取 3 轮正确承接（chat_turn_success >= 0.9）。
- [ ] 冲突多源展示与澄清语义可用。
- [ ] 验收集 A3 通过。

## 5. 风险
- 数值表述"顺手改数" -> 模板化渲染 + 契约 + 抽查。
- 会话上下文膨胀 -> 约束显式化 + 历史摘要 + 过期清理。

## 6. 工作量
约 9 个工作日。
