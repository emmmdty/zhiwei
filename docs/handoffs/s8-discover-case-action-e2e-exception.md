# S8 discover-case-action.spec.ts Gate 例外条目（ADR-012 §2）

> 登记日期：2026-09-04（e2e 收敛轮，执行方）　阶段：S8　对应反例：ADR-013 反例 9
> 状态：本条目使 S8 Gate 为「有条件收口」项；解锁前不得宣称该项通过。

## 例外四要素

- **阻塞项**：specs/s8 §8 Gate 命令
  `npm --prefix apps/web run test:e2e -- discover-case-action.spec.ts` 不可执行——spec 文件
  不存在，且本轮评估结论为无法诚实落盘（见根因）。复现输出：`Error: No tests found.`
  （2026-09-04，capability-hub.spec.ts 同型验证）。

- **根因**：**被测前端能力缺失，非测试环境阻塞**。spec §2 Required modules 要求
  `apps/web/src/features/{discover,cases,actions}/`，§3/§4 定义 journey（DiscoveryProgram →
  RiskFingerprint/dedupe → Feed/Triage → Case → Approval/ActionReceipt → HumanResolution →
  lesson candidate；启发式 score 不称 probability）——前端现状：`apps/web/src/features/`
  仅 approvals/auth/members/organizations/runs/workbench/workspaces（S1 tenancy shell + S2
  runtime 面），无 discover/cases/actions 视图，`App.tsx` 单视图无路由。Playwright journey
  是真实前端行为的契约——对永不渲染的 DOM 写等待断言的 spec 永远无法转绿，属造假 spec，
  本轮任务纪律明确禁止。**对照证据**：S2 `runtime-approval.spec.ts` 已按 mock 模式落盘并
  通过（2026-09-04，3/3 passed），证明 mock 通道本身可行——S8 缺的不是测试通道而是被测
  UI。后端 discover/cases 域模块已存在（`src/zhiwei/discover/`、`src/zhiwei/cases/`），
  前端无消费方。

- **解锁条件**：按 spec §3/§4 实现 features/{discover,cases,actions} 视图并绑定真实 API
  后，以 `runtime-approval.spec.ts` 的 mock 模式先例落盘 spec。spec 至少覆盖：Discover feed
  （RiskHypothesis 列表，score 呈现不得标注 probability）→ 人工 triage（owner/status 状态
  迁移）→ 创建 Case → approved action 提交（高风险动作不默认执行，server-driven 门禁）→
  HumanResolution 记录。数据面 D0–D6 断言由 eval suite 承担，不在此 spec 伪装。

- **复执行时点**：features/{discover,cases,actions} 实现（RED→GREEN，spec 即 RED 契约）
  后的 S8 Gate 复执行；若 S8 收口时仍未实现，最迟并入 S10 Studio 阶段 Gate 清单复执行。

## 证据（2026-09-04 检查）

- `ls apps/web/src/features/` → 无 discover/cases/actions
- `grep -rin "discover\|hypothesis\|triage" apps/web/src/` → 零 UI 命中
- mock 模式可行性参照：`apps/web/e2e/runtime-approval.spec.ts` 3/3 passed

## Operator 确认（ADR-012 §2 要求项，2026-09-05 补登记）

- **确认人**：operator（本轮清理指令确认，2026-09-05 会话记录）
- **确认内容**：本例外四要素齐全、根因为被测前端能力缺失（非环境阻塞），同意维持
  「有条件收口」；复执行时点（最迟并入 S10 Studio 阶段 Gate 清单）确认有效。
- **同步核验**：S1 tenancy e2e 已于 2026-09-05 真实栈全绿（13/13，见
  s1-tenancy-e2e-repair.md），S2 runtime-approval mock 3/3——mock 通道先例持续有效。


## Operator 确认与关闭（2026-09-06）

- **复执行证据**：`discover-case-action.spec.ts` **2 passed**（2026-09-06，HEAD 54fd3dd，S10 Gate
  复执行轮；mock 模式对齐真实 Pydantic 投影，未模拟路径 fail loud）。
- **解锁条件核验**：被测前端能力已实现并入库（apps/web/src/renderers/discover/ + api/discover.py（S10-T4c）+ discover-case-action.spec.ts）， journey 覆盖与本条目
  「解锁条件」逐项一致（独立验收 R1 测试工程师 + R3 增量复核确认）。
- **operator 确认**：ADR-012 §2 要求项——operator 于 2026-09-06 会话指令
  （「需要operator的两件事情，你自行判断一下」）授权按证据链判定并记录关闭；
  本条目状态由「有条件收口」转为 **关闭（例外解除）**。
