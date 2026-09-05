# S6 ask-evidence.spec.ts Gate 例外条目（ADR-012 §2）

> 登记日期：2026-09-04（e2e 收敛轮，执行方）　阶段：S6　对应反例：ADR-013 反例 9
> 状态：本条目使 S6 Gate 为「有条件收口」项；解锁前不得宣称该项通过。

## 例外四要素

- **阻塞项**：specs/s6 §7 Gate 命令 `npm --prefix apps/web run test:e2e -- ask-evidence.spec.ts`
  不可执行——spec 文件不存在，且本轮评估结论为无法诚实落盘（见根因）。复现输出：
  `Error: No tests found.`（2026-09-04，capability-hub.spec.ts 同型验证）。

- **根因**：**被测前端能力缺失，非测试环境阻塞**。spec §2 Required modules 要求
  `apps/web/src/features/{ask,evidence,cases}/`，§5 Workbench 定义三栏 UI（App/Case
  navigation；主 Ask 交互与 structured artifact；Run/Evidence/Tool/Context/Cost/Memory
  panels；点击 Claim 打开 source locator/canonical value/stale/classification/verify
  result；刷新后从 Run projection 恢复）——前端现状：`apps/web/src/features/` 仅
  approvals/auth/members/organizations/runs/workbench/workspaces（S1 tenancy shell + S2
  runtime 面），无 ask/evidence/cases 视图，`App.tsx` 单视图无路由。Playwright journey 是
  真实前端行为的契约——对永不渲染的 DOM 写等待断言的 spec 永远无法转绿，属造假 spec，
  本轮任务纪律明确禁止。**对照证据**：S2 `runtime-approval.spec.ts` 已按 mock 模式落盘并
  通过（2026-09-04，3/3 passed），证明 mock 通道本身可行——S6 缺的不是测试通道而是被测
  UI。后端 evidence/cases 域模块已存在（`src/zhiwei/evidence/`、`src/zhiwei/cases/`），
  前端无消费方。

- **解锁条件**：按 spec §5 实现 features/{ask,evidence,cases} 视图并绑定真实 API 后，以
  `runtime-approval.spec.ts` 的 mock 模式先例落盘 spec。spec 至少覆盖：Ask 提问 →
  Claim/Evidence 渲染（Fact/Quote 与 Inference/Recommendation 的 verified 标注差异可见）→
  点击 Claim 打开 source locator/verify result → 创建 Case → 刷新后从 Run projection 恢复。
  partial/abstain 渲染与 tamper 矩阵 UI 反例（spec §3/§7）随视图能力扩断言。

- **复执行时点**：features/{ask,evidence,cases} 实现（RED→GREEN，spec 即 RED 契约）后的
  S6 Gate 复执行；若 S6 收口时仍未实现，最迟并入 S10 Studio 阶段 Gate 清单复执行。

## 证据（2026-09-04 检查）

- `ls apps/web/src/features/` → 无 ask/evidence/cases
- `grep -rin "evidence\|ask\|case" apps/web/src/` → 零 UI 命中（仅 task_id 等字段误命中）
- mock 模式可行性参照：`apps/web/e2e/runtime-approval.spec.ts` 3/3 passed
