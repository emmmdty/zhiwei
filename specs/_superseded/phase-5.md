# P5 · 产品界面 Spec：对话工作台 + 风险视图 + 管理台

## 1. Goal / Non-Goals
**Goal**：前端三视图：①对话工作台（多轮问答 + 溯源面板 + 钻取）②风险视图（假设卡片/监控看板/事件/复盘）③管理台（用户组/数据源授权/模型管理/审计）。配套后端 API（见 docs/API.md）。
**Non-Goals**：不做移动端；不做可视化大屏（表格/卡片为主）。

## 2. 契约
- 技术栈：React 18 + TS + Vite + TanStack Query + SSE；溯源面板（TraceRef 四类渲染：SQL 重放/单元格/文档/外部快照）。
- 页面：`ChatWorkspace` / `RiskBoard` / `DataSources` / `AdminUsers` / `AdminModels` / `ReportView` / `ProjectList`。
- 权限渲染：按用户组隐藏/禁用无权限操作（后端仍强制）。
- 组件：TraceRefPanel / RiskHypothesisCard / MonitorTable / ModelManager / ACLForm。

## 3. 测试计划
1. 组件级（Vitest + RTL）：溯源面板四类渲染；权限按钮隐藏；风险卡片编辑留痕。
2. E2E（Playwright + mock API）：登录 -> 提问 -> 溯源展开 -> 钻取；风险分析 -> 看板；viewer 看不到管理入口与敏感列。
3. 可访问性冒烟。

## 4. 验收标准
- [ ] 三视图完整可演示；权限演示（viewer 无敏感列/无管理入口）。
- [ ] E2E 绿（CI，mock API）。
- [ ] 导出（对话+溯源+风险）可用。

## 5. 风险
- 前端膨胀 -> 组件化 + 测试先行；按页面裁剪（管理台可合并）。
- SSE 与状态一致性 -> 事件 sequence 幂等。

## 6. 工作量
约 9-10 个工作日。
