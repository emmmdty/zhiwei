# 知微 ZhiWei — AI 编码代理须知

本文件对所有参与本仓库开发的编码代理生效（opencode、Claude Code 等）。与任何默认行为冲突时以本文为准。

## 项目与事实源

企业 Agent Core 平台。`src/` 尚未实现，当前仓库只有冻结设计与基准评测资产。

事实源优先级（冲突时上位优先）：

1. `docs/superpowers/specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md` — 冻结总设计（架构约束）
2. `docs/DECISIONS.md` — ADR-001~010（机制级算法，总设计冻结后补齐）
3. `specs/s0-foundation.md` … `specs/s11-production-reference.md` — 各阶段实现规格
4. `docs/superpowers/plans/2026-08-12-s*.md` — 任务级执行计划，按 checkbox 逐条推进

发现规格无法实现时：**先提交最小反例、受影响 invariant、候选方案和迁移影响，再改规格**。
不允许在代码里静默创造第二套架构。

## 不可破坏的纪律

- **评测先行**：判分器与基准资产先于被评测的能力。评测走生产 Runtime，不写「评测专用」旁路。
- **声明纪律**：每项主张标 `已验证 / 配置声明 / 计划实现 / 未验证`。**没有 Gate artifact 支撑的数字
  不得写进任何文档、README、UI 或提交信息**——包括「行数」「覆盖率」这类看似无害的数字。
- **冻结资产只读**：`evals/` 下的语料、题集、风险数据、CHECKSUMS 不得修改。任何改动前后
  `make evals && make determinism` 必须全绿。
- **不调用 live 模型**：CI、测试、Compose 启动一律不发真实请求。live 只由 operator 显式触发。
- **不读 `.env`**：测试与工具链不得加载它。
- **fail closed**：未知 schema、未知能力、ACL 不确定、策略引擎不可用——一律拒绝，不取「常见默认」。

### 关于 model provider 凭据

Agent 运行时的 provider 统一为 OpenAI 兼容风格，默认 endpoint 使用标准键名
`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`（见 `.env.example`）。

**键名是通用的，值必须是已登记的**：`OPENAI_BASE_URL` 必须与 `config/providers/endpoints.yaml`
中某个 endpoint 的 `base_url` 规范化后完全一致，否则 fail closed。新增 endpoint 必须先写入该配置
并通过策略复核，不得在代码里硬编码 origin。

## 开发流程：RED → GREEN → REVIEW

每个 Task 严格按此推进，不允许先写实现再补测试：

```
1. RED     写失败测试，运行并确认它确实失败（且失败原因正确）
2. GREEN   写最小实现让测试通过
3. REVIEW  检查是否为「刚好骗过测试」的假实现；跑该阶段 Gate
4. COMMIT  按 plan 中的 Suggested commit 边界提交
```

**测试文件是契约，不是可调整的障碍。**

- 实现方**不得**修改 `tests/` 下任何文件——包括放宽断言、加 `skip`、改期望值、注释掉用例。
- 测试写错了要改，必须回到 RED 阶段由测试作者改，并说明为什么原断言是错的。
- 交接后运行 `make handoff-check` 验证这条规则未被破坏。

## 角色分工

工作按风险分三档，逐 Task 分配见 `docs/DEV_ALLOCATION.md`：

| 档 | 承担者 | 判据 |
| --- | --- | --- |
| A | Claude Code + Opus 5 | 错误不一定被测试捕获，或后果不可逆：安全边界、并发/事务、密码学与 digest、核心不变量、契约冻结、统计方法 |
| B | Opus 5 写 RED → opencode + deepseek-v4-flash 写 GREEN | 契约明确、行为可被测试完整覆盖 |
| C | opencode + deepseek-v4-flash 全包 | 机械转换 + 确定性验证 |

**如果你是 B/C 档的实现方**：

- 只改交接单白名单内的文件，不要通读整个仓库。
- 遇到规格歧义、需要改测试、需要动白名单外的文件、或发现设计缺陷时——**停下来报告，不要自行决策**。
  你看不到的上下文比你看到的多。
- 不引入新的第三方依赖。需要时停下来报告。
- 不为了让测试通过而硬编码返回值——这会在 REVIEW 阶段被退回重做。

## 命令

```bash
make evals            # 重建并校验冻结基准资产（110 项）
make determinism      # 两次干净重建，断言逐字节一致
make handoff-check    # 校验交接规则：tests/ 与 evals/ 未被实现方改动
uv run pytest -q
uv run ruff check .
uv run pyright
```

各阶段完整 Gate 见对应 `specs/s*.md` 的 Gate 小节。Gate 全绿才能进入下一阶段。

## 代码约定

- Python 3.11+，uv 管理依赖；行宽 100（`pyproject.toml` 已固定 ruff 规则集，不依赖其默认值）。
- domain 层不导入 FastAPI/Temporal/SQLAlchemy/provider SDK；依赖方向见 `docs/ARCHITECTURE.md` §2。
- 前端固定 Node 22 + npm/package-lock，不经 uv 启动 TS 工具链。
- 注释写「为什么」，不写「是什么」。项目既有代码的注释密度和风格是基准。

## 提交

- 按 plan 中的 `Suggested commit` 边界提交，一个 Task 一个 commit。
- Conventional Commits，作用域取模块名：`feat(contracts):`、`fix(runtime):`。
- 不在提交信息里写未经 artifact 支撑的效果数字。
