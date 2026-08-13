@AGENTS.md

## Claude Code 专属

`AGENTS.md` 是本仓库对所有编码代理的唯一指令源，上面一行把它导入本会话——**不要在此文件重复
其内容**，两份会漂移。Claude Code 不读 `AGENTS.md`，opencode 优先读它、仅在缺失时回退到本文件。

以下是仅对 Claude Code 生效的补充：

- **承担 A 档任务**（安全边界、并发/事务、密码学与 digest、核心不变量、契约冻结、统计方法）。
  这类改动先用 plan mode 过一遍设计再动手。
- **写 RED 时对 B 档产出交接单**，格式见 `docs/DEV_ALLOCATION.md` §4.1。交接前必须先提交 RED，
  否则 `make handoff-check` 没有比对基线。
- **回收 B 档产出时做第三层 review**：自动检查拦不住「刚好骗过测试」的实现——重点看有没有
  `if input == <测试里的值>`、`except: pass`、只覆盖测试用到的分支、TODO 占位。
- **阶段 Gate 由 Claude Code 跑**，不交给 opencode。
- live 模型调用（S3-T7、S9 live suite）只由 operator 手动触发，任何代理都不得自动执行。
