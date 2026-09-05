# S7 memory-center.spec.ts Gate 例外条目（ADR-012 §2）

> 登记日期：2026-09-04（e2e 收敛轮，执行方）　阶段：S7　对应反例：ADR-013 反例 9
> 状态：本条目使 S7 Gate 为「有条件收口」项；解锁前不得宣称该项通过。

## 例外四要素

- **阻塞项**：specs/s7 §8 Gate 命令 `npm --prefix apps/web run test:e2e -- memory-center.spec.ts`
  不可执行——spec 文件不存在，且本轮评估结论为无法诚实落盘（见根因）。复现输出：
  `Error: No tests found.`（2026-09-04，capability-hub.spec.ts 同型验证）。

- **根因**：**被测前端能力缺失，非测试环境阻塞**。spec §2 Required modules 要求
  `apps/web/src/features/memory/`，§5 Memory Center 定义用户 journey（查看本人和可见
  团队/Case memory，按来源/类型/状态筛选，执行 confirm/correct/resolve/revoke/delete/
  export；团队确认仅 Steward；删除显示 index/cache cascade 状态和历史 tombstone
  boundary）——前端现状：`apps/web/src/features/` 仅 approvals/auth/members/organizations/
  runs/workbench/workspaces（S1 tenancy shell + S2 runtime 面），无 memory 视图，
  `App.tsx` 单视图无路由。Playwright journey 是真实前端行为的契约——对永不渲染的 DOM 写
  等待断言的 spec 永远无法转绿，属造假 spec，本轮任务纪律明确禁止。**对照证据**：S2
  `runtime-approval.spec.ts` 已按 mock 模式落盘并通过（2026-09-04，3/3 passed），证明
  mock 通道本身可行——S7 缺的不是测试通道而是被测 UI。后端 API router
  （`src/zhiwei/api/memory.py`）与 memory 域模块已存在，前端无消费方。

- **解锁条件**：按 spec §5 实现 features/memory 视图并绑定真实 API 后，以
  `runtime-approval.spec.ts` 的 mock 模式先例落盘 spec。spec 至少覆盖：Memory 列表（按
  来源/类型/状态筛选）→ confirm（仅 Steward 可见团队记忆确认入口，server-driven）→
  revoke → 状态投影（candidate/confirmed/superseded/revoked/expired 词汇对齐 DATA_MODEL）
  → 删除展示 cascade/tombstone 边界。correct/resolve/export 随视图能力扩断言。

- **复执行时点**：features/memory 实现（RED→GREEN，spec 即 RED 契约）后的 S7 Gate 复执行；
  若 S7 收口时仍未实现，最迟并入 S10 Studio 阶段 Gate 清单复执行。

## 证据（2026-09-04 检查）

- `ls apps/web/src/features/` → 无 memory
- `grep -rin memory apps/web/src/` → 零 UI 命中
- mock 模式可行性参照：`apps/web/e2e/runtime-approval.spec.ts` 3/3 passed

## Operator 确认（ADR-012 §2 要求项，2026-09-05 补登记）

- **确认人**：operator（本轮清理指令确认，2026-09-05 会话记录）
- **确认内容**：本例外四要素齐全、根因为被测前端能力缺失（非环境阻塞），同意维持
  「有条件收口」；复执行时点（最迟并入 S10 Studio 阶段 Gate 清单）确认有效。
- **同步核验**：S1 tenancy e2e 已于 2026-09-05 真实栈全绿（13/13，见
  s1-tenancy-e2e-repair.md），S2 runtime-approval mock 3/3——mock 通道先例持续有效。
