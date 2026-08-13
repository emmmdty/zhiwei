# 知微 ZhiWei — AI 开发工具须知

本文件对所有参与本仓库开发的 AI 工具生效（Claude Code、opencode 等）。与任何默认行为冲突时以本文为准。

## 1. 这是什么项目

企业 Agent Core 平台。事实源优先级：

1. `docs/superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md` — 冻结总设计（架构约束）
2. `docs/DECISIONS.md` — ADR-001~010（机制级算法，总设计冻结后补齐）
3. `specs/s0-foundation.md` … `specs/s11-production-reference.md` — 各阶段实现规格
4. `docs/superpowers/plans/2026-08-12-s*.md` — 任务级执行计划（checkbox 逐条推进）

发现规格无法实现时：**先提交最小反例、受影响 invariant、候选方案和迁移影响，再改规格**。
不允许在代码里静默创造第二套架构。

## 2. 不可破坏的纪律

- **评测先行**：判分器与基准资产先于被评测的能力。评测走生产 Runtime，不写「评测专用」旁路。
- **声明纪律**：每项主张标 `已验证 / 配置声明 / 计划实现 / 未验证`。**没有 Gate artifact 支撑的数字
  不得写进任何文档、README、UI 或提交信息**——包括「行数」「覆盖率」这类看似无害的数字。
- **冻结资产只读**：`evals/` 下的语料、题集、风险数据、CHECKSUMS 不得修改。任何改动前后
  `make evals && make determinism` 必须全绿。
- **不调用 live 模型**：CI、测试、Compose 启动一律不发真实请求。live 只由 operator 显式触发。
- **不读 `.env`**：测试与工具链不得加载它。凭据键名见 `.env.example`；通用 `OPENAI_*` 键被 ADR-010 禁止。
- **fail closed**：未知 schema、未知能力、ACL 不确定、策略引擎不可用——一律拒绝，不取「常见默认」。

## 3. 开发流程：RED → GREEN → REVIEW

每个 Task 严格按此推进，不允许先写实现再补测试：

```
1. RED     写失败测试，运行并确认它确实失败（且失败原因正确）
2. GREEN   写最小实现让测试通过
3. REVIEW  检查是否为「刚好骗过测试」的假实现；跑该阶段 Gate
4. COMMIT  按 plan 中的 Suggested commit 边界提交
```

**测试文件是契约，不是可调整的障碍。**

- 实现方**不得**修改 `tests/` 下的任何文件——包括放宽断言、加 `skip`、改期望值、注释掉用例。
- 测试写错了要改，必须回到 RED 阶段由测试作者改，并说明为什么原断言是错的。
- 交接后运行 `make handoff-check` 验证这条规则未被破坏。

## 4. 角色分工

工作按风险分三档，详见 `docs/DEV_ALLOCATION.md`：

| 档 | 谁做 | 判据 |
| --- | --- | --- |
| A | Claude Code + Opus 5 | 错误不一定被测试捕获，或后果不可逆：安全边界、并发/事务、密码学/digest、核心不变量、契约冻结 |
| B | Opus 5 写 RED → opencode + deepseek-v4-flash 写 GREEN | 契约明确、测试能完整覆盖行为 |
| C | opencode + deepseek-v4-flash 全包 | 机械转换 + 确定性验证：fixture 数据、样板、同模式的第 N 个 adapter |

**如果你是 B/C 档的实现方**：只改交接单白名单内的文件；遇到规格歧义、需要改测试、或发现设计缺陷时
**停下来报告**，不要自行决策——你看不到的上下文比你看到的多。

## 5. 常用命令

```bash
make evals            # 重建并校验冻结基准资产（110 项）
make determinism      # 两次干净重建，断言逐字节一致
make handoff-check    # 校验交接规则：tests/ 未被实现方改动
uv run pytest -q
uv run ruff check .
uv run pyright
```

各阶段完整 Gate 见对应 `specs/s*.md` 的 Gate 小节，Gate 全绿才能进入下一阶段。

## 6. 提交

- 按 plan 中的 `Suggested commit` 边界提交，一个 Task 一个 commit。
- 提交信息用 Conventional Commits，作用域取模块名（`feat(contracts):`、`fix(runtime):`）。
- 不在提交信息里写未经 artifact 支撑的效果数字。
