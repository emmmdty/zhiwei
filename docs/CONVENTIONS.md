# 工程规范

## 1. 工具链

- Python 3.11+，使用 `uv venv`、`uv sync`、`uv run`。
- Pydantic v2 契约；SQL 使用 AST parser/SQLAlchemy，不做正则安全判断。
- Ruff + Pyright + Pytest/Hypothesis；前端固定 Node.js 22 LTS + npm/package-lock + TypeScript/React/Vite/
  Playwright。禁止用 `uv run playwright` 或在同仓库混用 pnpm/yarn。
- 本地只跑 CPU；GPU 命令仅在用户明确批准后通过 `ssh gpu-4090` 执行。

## 2. TDD 循环

1. 写最小失败测试，覆盖一个可观察行为。
2. 运行精确 test node，确认因缺少目标行为而失败。
3. 实现最小完整 vertical slice，同时更新相关类型、调用点和 fixture。
4. 先跑精确测试，再跑所属层回归。
5. 更新 artifact/schema docs；提交只包含同一行为。

## 3. 测试层

| 层 | 内容 | 默认 CI |
| --- | --- | --- |
| L0 | schema、reducer、policy inputs、哈希、scorer、统计 | 必跑 |
| L1 | PG/RLS、Temporal fixture、ObjectStore、provider/capability contract | 必跑 |
| L2 | local-product integration、security、Playwright journeys | 必跑/分片 |
| L3 | Go probes、外部 source、load/fault、live eval/release | 手动、明确 marker |

默认命令：`uv run pytest -m 'not live and not slow'`。live/slow 测试不得因环境缺失悄悄通过；
开发运行可 skip，但正式 Gate 仍为未通过。

## 4. 契约纪律

- 跨模块只传 Pydantic domain model；SDK response、ORM row 和前端 DTO 不越界。
- canonical JSON 使用固定字段顺序无关的 RFC 8785/JCS 语义并记录 schema version。
- canonical event 在 PostgreSQL append-only 表中；JSONL 是导出/eval artifact，SQLite 仅保留历史资产生成用途。
- PostgreSQL 是业务真相，Temporal 是执行位置，OpenSearch/Redis 是可重建派生状态。
- Solution Pack 只能依赖 Core public contracts；architecture test 阻断 Core 导入具体 App。
- 数据、问题、profile、prompt、config、source tree、run 和发布文件均内容寻址。
- 未知模型能力为 `null`，不是推测默认值。

## 5. CI

CI 步骤**按阶段递增**。加入一条其产物尚不存在的命令会让 CI 从该阶段起恒红，实现者只能
删步骤或伪造占位文件，两种做法都会污染后续阶段的 RED 断言。

S0 起（基线，必须全绿）：

```bash
uv sync --extra evals --extra dev
make evals
make determinism
uv run ruff check .
uv run pyright
uv run pytest -m 'not live and not slow'
```

S0 收口时追加：migration、RLS 基础、`uv run zhiwei assets lock --check`、fixture run sealing 与报告复算。

S1 创建 `apps/web/package-lock.json` 后追加：

```bash
npm --prefix apps/web ci
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run test
npm --prefix apps/web run test:e2e
```

**S1 起**加入 PostgreSQL/OPA integration；**S2 起**加入 Temporal fixture；**S4 起**加入 capability security
corpus；**S6 起**加入 Ask browser journey。`compose.yaml` 在 S0 建立最小 test profile，S11 才通过完整
local-product Gate：

```bash
docker compose config --quiet
```

任一资产漂移、tenant escape、partial run、缺少限制声明或 README 引用无效 artifact 时 CI/release
check 失败。live、load、destructive fault 不在普通 PR 自动运行，但缺失时对应 Gate 保持未通过。

## 6. Git 与秘密

- 不提交 `.env`、API Key、原始 reasoning、认证 header 或私有 bundle。
- 配置只引用专用 env 名；live request artifact 先 scrub，再密封。
- 工作区可能有用户改动，不回滚无关内容。
- commit 使用 `feat/fix/test/docs/chore(scope): summary`；每个实施任务包含建议 commit 点。
