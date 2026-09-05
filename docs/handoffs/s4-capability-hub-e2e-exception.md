# S4 capability-hub.spec.ts Gate 例外条目（ADR-012 §2）

> 登记日期：2026-09-04（e2e 收敛轮，执行方）　阶段：S4　对应反例：ADR-013 反例 9
> 状态：本条目使 S4 Gate 为「有条件收口」项；解锁前不得宣称该项通过。

## 例外四要素

- **阻塞项**：specs/s4 §8 Gate 命令 `npm --prefix apps/web run test:e2e -- capability-hub.spec.ts`
  不可执行——spec 文件不存在，且本轮评估结论为无法诚实落盘（见根因）。复现输出：
  `Error: No tests found.`（2026-09-04）。

- **根因**：**被测前端能力缺失，非测试环境阻塞**。spec §6 Web journey（Publisher 从
  Registry/URL/Git/OpenAPI 导入 → 检视 source/version/schema/SBOM/license/vulnerability/
  network/effect/risk/test → 批准 → 创建 Workspace Connection 并 test；Builder 只能绑定
  published CapabilityVersion；Security Admin suspend/revoke）与 §2 Required modules 中的
  `apps/web/src/features/capabilities/` 均未实现。前端现状：`apps/web/src/features/` 仅
  approvals/auth/members/organizations/runs/workbench/workspaces（S1 tenancy shell + S2
  runtime 面），`App.tsx` 单视图无路由。Playwright journey 是真实前端行为的契约——对永不
  渲染的 DOM 写等待断言的 spec 永远无法转绿，属造假 spec，本轮任务纪律明确禁止。
  **对照证据**：S2 `runtime-approval.spec.ts` 已按 mock 模式落盘并通过（2026-09-04，3/3
  passed；网络层后端仿真 + 真实 UI 组件驱动），证明 mock 通道本身可行——S4 缺的不是
  测试通道而是被测 UI。后端 API router（`src/zhiwei/api/capabilities.py`、
  `connections.py`）已存在，前端无消费方。

- **解锁条件**：按 spec §6 实现 `features/capabilities` 视图并绑定真实 API 后，以
  `runtime-approval.spec.ts` 的 mock 模式先例落盘 spec（响应形状对齐 api/capabilities.py /
  api/connections.py 的 Pydantic 投影；未模拟路径 fail loud）。spec 至少覆盖：Publisher
  导入→检视→批准；Builder 绑定 published 版本（未发布版本绑定被拒）；Security Admin
  suspend/revoke 后 UI 显示结构化失败与受影响版本。注意：本 spec 不替代 spec §7 的真实
  provider reference integration（Fake 件边界纪律不变）。

- **复执行时点**：`features/capabilities` 实现（RED→GREEN，spec 即 RED 契约）后的 S4 Gate
  复执行；若 S4 收口时仍未实现，最迟并入 S10 Studio 阶段 Gate 清单复执行。

## 证据（2026-09-04 检查）

- `ls apps/web/src/features/` → approvals auth members organizations runs workbench
  workspaces（无 capabilities）
- `grep -ri capabilit apps/web/src/` → 零匹配（仅 approvals/runs 内的 task 字段误命中）
- `npm --prefix apps/web run test:e2e -- capability-hub.spec.ts` → `Error: No tests found.`
- mock 模式可行性参照：`apps/web/e2e/runtime-approval.spec.ts` 3/3 passed
