# S0 Foundation — Gate Report

> 日期：2026-08-13 18:59:52 +0800  
> 验收 commit：`dae230885dd9815c99ce99014384b3b81ff8ec80`  
> 分支：`feat/s0-foundation`  
> 状态：工作树干净，冻结资产无漂移

## 判定

**PASS（已验证）**。`specs/s0-foundation.md` §6 的完整 Gate 在最终 HEAD 上通过；
`dev doctor` 返回真实 schema revision，empty Run/EvalRun 经 PostgreSQL、ObjectStore、canonical
event、outbox 和 manifest 管线密封并独立复核。

## Gate 逐项结果

| 命令 | 本轮结果 |
|---|---|
| `uv sync --extra dev --extra evals` | exit 0 |
| `make evals` | exit 0；validator 全部通过 |
| `make determinism` | exit 0；两次干净重建逐字节一致 |
| `uv run ruff check .` | exit 0；All checks passed |
| `uv run pyright` | exit 0；无 error/warning |
| `uv run pytest tests/unit tests/contract tests/integration/foundation -q` | exit 0；277 passed in 16.90s |
| `uv run alembic upgrade head` | exit 0 |
| `uv run alembic check` | exit 0；No new upgrade operations detected |
| `uv run zhiwei dev doctor --format json` | exit 0；全部 checks 为 `ok` |
| `uv run zhiwei eval seal-empty --check` | exit 0；`verified=true` |
| `uv run zhiwei assets lock --check` | exit 0 |
| `git diff --exit-code -- evals/` | exit 0；冻结资产无漂移 |
| `make handoff-check` | exit 0 |

完整测试报告：

```text
........................................................................ [ 25%]
........................................................................ [ 51%]
........................................................................ [ 77%]
.............................................................            [100%]
277 passed in 16.90s
```

## Sealed empty Run/EvalRun

```json
{
  "organization_id": "a6ffa4b6-4d57-4f5f-9929-708df35763ec",
  "workspace_id": "987ad653-ff84-4a03-aa7b-40a43238e216",
  "run_id": "92e5055a-9990-426f-a257-7a0f0dd195f6",
  "run_status": "succeeded",
  "eval_run_id": "44822507-331c-4b0a-8a96-d1aeca488092",
  "eval_run_status": "sealed",
  "mode": "fixture",
  "registered_units": 0,
  "verified": true,
  "migration_revision": "0001_foundation",
  "code_digest": "sha256:4c38f4331156ae2c8540bfd5ea372bdf55a53df2f4dc5ba99f8ed0da69c1652d",
  "config_digest": "sha256:45b8f1c63e105f2214eb830474e9f90b7b2effe0d5da6cf355a4771bf1a019ba",
  "schema_digest": "sha256:f6dcc8e764037693c924f648d0f147144d2065e10f436b0500fbc47d09fc6283",
  "seal_digest": "sha256:d5d03abefaf3fe762269e261858a00ec3f40e5951aae80421dcbcd406c374719"
}
```

## Manifest、event 与 outbox

| 类型 | ID | Digest / 状态 |
|---|---|---|
| dataset manifest | `f8d5292e-bfd4-4868-a260-ede3f62cdf34` | `sha256:5022da0411f95ed1c1d84d94b564e4b2e09ca30f66ecc1cc5944114bff853fa1` |
| sealed test-report manifest | `408166ca-20ea-4eb7-b931-e6b135980e47` | `sha256:f639b3ad794675b45c7bb0b5b546421b40481b7371eee726d0b66063aa126c07` |
| sealed eval manifest | `d44e56f4-ddc7-4bda-b491-e0cb48c4dd88` | `sha256:d5d03abefaf3fe762269e261858a00ec3f40e5951aae80421dcbcd406c374719` |
| canonical event | `ab1627fb-bf0e-4816-9824-6a020363e6eb` | `sha256:c1dc1837a5fafeaa63e1bc6657ced0956ddfc338070d46618ceb1f9298814238` |
| outbox | `2278b685-fcff-42f0-b4a4-a73147737e65` | `pending`（test sink 尚未 claim） |

sealed test-report object 记录实际执行命令、退出码与结果摘要：

```json
{
  "scope": "s0-eval-contract-tests",
  "status": "passed",
  "exit_code": 0,
  "summary": "25 passed in 0.53s"
}
```

ObjectStore 根目录：`/tmp/zhiwei-s0-final-dae2308`。seal object 与 test-report object 的
SHA-256 已分别和 PostgreSQL manifest 复核一致。

## 测试契约修订

- 测试作者修订 `dev doctor` 契约：允许短超时连接显式配置的数据库查询 schema revision，绝不
  probe 模型 provider。
- 原“不得发起任何网络连接”是 S0-T1 migration 尚未接入时的占位约束；同步更新了
  `docs/handoffs/s0-t1.md`。
- 修复测试模块 import 分组的 Ruff `I001`，未改动产品实现。

本报告位于 gitignored 的 `artifacts/gates/s0/`；release-safe artifact 仍应由发布流程显式纳入。
