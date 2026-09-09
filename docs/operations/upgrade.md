# 升级流程（specs/s11 §4；Task 3）

> 状态：**设计冻结（A 档 RED 形态设计评审，F-R8-04 修订承接）**——本文 §2 的契约
> 先于实现冻结；实现与测试以此为准，修改须回到 RED 重新确认。

## 1. 交付物

- Alembic **expand / migrate(回填) / contract** 三段式迁移（0025 expand、0026 contract）
  ——全仓首个部署级三段式（先例 0006 是单表 CHECK 收窄，非部署级编排）。
- **event/outbox 行 backward reader**：跨迁移代读取（旧代码写的行可被新代码读）。
- **Temporal version marker**：升级清单记录 worker build id，preflight 校验。
- **OpenSearch rebuild + alias switch**：对真实 OpenSearch 执行新索引构建与原子切换。
- **Agent/Tool/Skill version pin**：升级前后 release claims pin 校验。
- **preflight / abort / rollback 规则**；destructive contract 迁移需显式 checkpoint。

## 2. 冻结契约（RED）

### 2.1 previous 版本如何构造

不 checkout 旧代码、不伪造旧二进制。previous = **revision 0019 的 schema + 旧形态数据行**：

1. 空 DB `alembic upgrade 0019`；
2. 按 0019 形态写入旧数据（outbox 行无 `dispatch_deadline`、`schema_version=1`）；
3. `alembic upgrade head` 执行三段式；
4. 断言旧行可读（backward reader）+ 新约束生效。

「previous 版本」的定义 = 迁移链的历史状态，由 Alembic 本身保证可复现。

### 2.2 三段式语义（0025/0026）

载体：`outbox.dispatch_deadline TIMESTAMPTZ`——崩溃窗口 #2（commit 后、dispatch 前
进程死亡）的「pending 超龄命令」判定锚点，有真实消费方（Task 5 fault runner），
不是为演练制造的假列。

| 阶段 | revision | 动作 | 兼容性 |
| --- | --- | --- | --- |
| expand | 0025 | `ADD COLUMN dispatch_deadline`（NULLable） | 旧代码不感知；新旧混跑安全 |
| migrate(回填) | 0026 内 | `UPDATE … SET dispatch_deadline = available_at + 300s WHERE NULL` | 旧行获得锚点 |
| contract | 0026 | `SET NOT NULL` | **destructive**：需显式 checkpoint；执行后 NULL 不可能 |

- 回滚窗口：0026 未执行（停在 expand 后）时 `downgrade` 安全；0026 执行后回滚
  属破坏性动作，超出本机制承诺（走备份恢复，Task 4）。
- contract 的 checkpoint 语义：`upgrade_run --checkpoint` 显式确认后执行；缺省
  在 expand 后停下并报告——旧代码可继续跑（NULLable 列无害）。

### 2.3 backward reader

`read_outbox_rows_cross_era(rows, now)`：

- `schema_version ∈ {1, 2}` 之外的行 → **拒绝**（fail closed，未知 schema 一律拒绝）；
- `dispatch_deadline IS NULL` 的行 → 旧代行：fallback `available_at + 300s`，
  结果标注 `era='v1'`；
- 非 NULL → `era='v2'`。
- 前提：读取发生在 **expand 之后**（列存在）；expand 之前该 reader 不可调用。

### 2.4 Temporal version marker

升级清单（UpgradeManifest JSON）记录 `worker_build_id`。worker 组装经
`ZHIWEI_WORKER_BUILD_ID` 环境变量注入（缺省 `dev-local`）；preflight 校验清单与
当前 worker build id 一致性——不匹配即 abort（旧 worker 仍在跑旧 build 时，
新 build 的 rollout 属 operator 动作，不在自动升级序列内）。

### 2.5 OpenSearch rebuild + alias switch

对真实 OpenSearch（local-product compose 发布在 127.0.0.1:9201）：

1. 创建 `knowledge-v{n+1}` 索引；
2. bulk 写入文档集；
3. 原子切换 alias `knowledge` → v{n+1}（同一 _alias 动作内 remove+add）；
4. 校验：alias 指向新索引且文档数一致。Source Ledger 全程不动。

### 2.6 preflight / abort / rollback

- preflight：DB revision == manifest.previous_revision；Temporal 可达；claims pin
  与清单一致；checkpoint 需求已声明。任一失败 → abort（不执行、不自动降级）。
- 执行序列：preflight → expand → (checkpoint gate) → contract → marker 更新 →
  OpenSearch rebuild（可选步骤，由调用方声明）→ 后置校验。
- rollback：仅当 contract 未执行且 expand 已执行 → `downgrade` 到
  previous_revision；contract 后不提供自动回滚。

### 2.7 Agent/Tool/Skill version pin（已实现，2026-09-08）

`UpgradeManifest.claims_snapshot_digest`（可选）：声明时 preflight 强制校验——
系统级读取 claim_registry 全行（claim_id/status/evidence）的 canonical JSON
digest，与清单不符即 abort；不声明即不声称已校验（诚实口径）。
`UpgradeManifest.temporal_target`（可选）：声明时 preflight 强制 TCP 探活，
不可达即 abort。生产入口：`zhiwei ops upgrade-run`（--from/--to/--checkpoint/
--contract；destructive contract 需显式 --checkpoint）。
