# ZhiWei Implementation Plans

这些文件是[冻结总设计](../specs/2026-08-12-zhiwei-enterprise-agent-platform-design.md)和 `specs/s*.md`
的执行层，不创建新产品语义。

## 执行顺序

1. [S0 Platform Foundation](2026-08-12-s0-foundation.md)
2. [S1 Tenancy, Identity and Policy](2026-08-12-s1-tenancy-policy.md)
3. [S2 Agent Runtime](2026-08-12-s2-agent-runtime.md)
4. [S3 Models and Context](2026-08-12-s3-models-context.md)
5. [S4 Capability Hub](2026-08-12-s4-capability-hub.md)
6. [S5 Knowledge Fabric](2026-08-12-s5-knowledge-fabric.md)
7. [S6 Evidence and Ask](2026-08-12-s6-evidence-ask.md)
8. [S7 Memory](2026-08-12-s7-memory.md)
9. [S8 Discover and Actions](2026-08-12-s8-discover-actions.md)
10. [S9 Eval, Release and Observability](2026-08-12-s9-eval-release-observability.md)
11. [S10 Studio and Third App](2026-08-12-s10-studio-third-app.md)
12. [S11 Production Reference](2026-08-12-s11-production-reference.md)

## Agent 执行规则

- 先完整读取总设计、对应 `specs/sN-*.md`、本计划和所有被修改文件；不得只按任务标题猜实现。
- 每个 Task 独立执行 RED→GREEN→focused regression；checkbox 是进度事实，Gate 通过前不开始依赖阶段。
- 当前仓库没有基线 commit，项目所有者建立并确认 baseline 前，计划中的 commit 仅是建议边界，执行 Agent
  不得擅自把全部 untracked assets 提交。
- live、外部 OAuth、load 和 destructive fault 只在步骤明确标注时由操作者触发；普通测试禁止读取真实
  Key，Docker startup 禁止调用真实 LLM。
- 实现发现规范矛盾时停止该 Task，保存最小反例、受影响 invariant、两个候选方案和迁移影响；不得在代码
  中静默创造第二套 contract。
- 不改写 `evals/` 冻结资产来适配代码；若 legacy adapter 暴露真实错误，先报告再按批准的 migration 处理。
- 每个新增 CLI 命令必须在该 Task 列出 `src/zhiwei/cli/*.py`，注册到 `cli/main.py`，并有 CLI runner
  `--help`、invalid input 和 fixture smoke tests；Gate 不得引用尚未注册的命令。

## 统一验证

每个阶段收口至少运行：

```bash
uv sync --extra dev --extra evals
uv run ruff check .
uv run pyright
uv run pytest -m 'not live and not slow'
make evals
make determinism
```

S1 创建前端后还必须运行：

```bash
npm --prefix apps/web ci
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run test
npm --prefix apps/web run test:e2e
```

后两个命令运行前后应确认冻结资产没有意外漂移。规范优先级：冻结总设计 > `specs/s*.md` > 本目录计划
> 其他摘要文档。
