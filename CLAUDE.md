@AGENTS.md

## Claude Code 专属

`AGENTS.md` 是本仓库对所有编码代理的唯一指令源，上面一行把它导入本会话——**不要在此文件重复
其内容**，两份会漂移。Claude Code 不读 `AGENTS.md`，opencode 优先读它、仅在缺失时回退到本文件。

以下是仅对 Claude Code 生效的补充：

- 当承担**设计/验收方**职责时，负责规格与计划、A 档不变量和关键测试、UI 视觉设计与验收，以及
  阶段 Gate；不因使用 Claude Code 就自动取得某个 Task 的实现所有权。
- 当承担**执行方**职责时，与其他工具遵守相同规则；B/C 档可完成 RED → GREEN，A 档按已冻结契约
  实现。RED 必须先提交，随后使用 `make handoff-check HANDOFF_BASE=<RED commit>` 验证锁定测试。
- **做独立 review 时检查假实现**：自动检查拦不住「刚好骗过测试」的实现——重点看有没有
  `if input == <测试里的值>`、`except: pass`、只覆盖测试用到的分支、TODO 占位。
- 工具名称不是 Gate 的硬前置；阶段 Gate 应由未直接实现该阶段关键路径的设计/验收方执行。
- live 模型调用（S3-T7、S9 live suite）只由 operator 手动触发，任何代理都不得自动执行。
