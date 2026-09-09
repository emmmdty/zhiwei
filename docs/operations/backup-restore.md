# 备份与恢复（specs/s11 §4；Task 4）

> 状态：机制冻结——backup manifest 组件、隔离恢复校验清单、fail-closed 语义以本文
> 与 `src/zhiwei/operations/{backup,restore}.py` 契约测试为准。

## 1. 事实源边界（spec §4）

| 组件 | 是否事实源 | 处置 |
| --- | --- | --- |
| PostgreSQL（业务 / identity / Temporal persistence） | 是 | pg_dump 全量进备份 |
| ObjectStore（artifacts） | 是 | 逐文件 sha256 进备份 |
| Secret recovery material（keyring 文件） | 是 | 原文进备份（operator 存储纪律：备份本身按 secret 处置） |
| release claims | 是 | 表导出进备份（digest 封存） |
| Redis / OpenSearch | **否** | 不进备份；恢复期由对象/事件**重建**（spec §4：可重建） |

## 2. backup manifest

`backup create` 产出目录：

```
manifest.json          # 组件清单 + 逐项 sha256 + manifest 自身 digest
pg/{app,identity,temporal}.sql       # pg_dump 纯文本
objects/<相对路径>                    # ObjectStore 逐文件
secrets/zhiwei_identity_master_key   # keyring 原文
claims/claim_registry.json           # claims 导出（maintenance DSN 系统级读取）
```

manifest 字段（frozen）：`created_at / profile / pg{db,digest}×3 / objects{path:digest} /
keyring{digest} / claims{digest} / excluded=[redis, search]（重建语义声明）/
manifest_digest`。`backup verify` 逐项重算 sha256，任何不一致 → fail（exit 1）。

## 3. 隔离恢复校验（restore verify --isolated）

隔离 = 同实例新 database（`restore_<uuid>_app/_identity/_temporal`）+ 独立对象目录；
不触碰生产 database。校验清单（全部通过才 exit 0）：

1. **恢复完整性**：三库 pg_restore 成功；对象文件 digest 与 manifest 一致；
2. **RLS**：租户上下文读回种子行可见；跨租户 GUC 读不可见（行级隔离保持）；
3. **artifact digest**：对象复算 = manifest（防恢复通道静默损坏）；
4. **canonical projection**（机制冻结修订 2026-09-08，独立验收后对齐实现）：
   事件 digest 链一致性校验——sequence_no == 事件数、投影 head_event_digest ==
   链尾事件 digest、链无断裂（与 foundation 投影测试同构的确定性锚）。
   不经 `CanonicalUnitOfWork.rebuild_projection`：该入口需要调用方拼装
   SchemaRegistry（恢复场景无法预先声明全部事件 schema），digest 链校验是
   schema 无关的等价确定性验证；
5. **search rebuild**：对象文档经 `opensearch_rebuild_and_switch` 写入新索引并
   切换 alias（spec §4 重建语义，对真实 OpenSearch 执行）——OpenSearch 端点
   不可用 = 校验不可用，**硬失败**（不得静默跳过，对抗审查修复 2026-09-08）；
6. **workflow reconciliation**：runs 终态与 outbox 命令一致性（终态 run 无
   pending/processing 命令；活跃 run 无多重 pending start）；
7. **secret rotation**：keyring 载入 + envelope 往返 + `with_added` 轮换后旧 key
   仍可解密——备份缺 keyring 材料即**硬失败**（local_product 备份的必需组件）；
8. **Claim Registry**：恢复的 claims 导出与 manifest digest 一致且非空。

## 4. fail closed

- manifest 缺组件 / digest 不符 / 版本声明缺失 → verify/restore 立即失败；
- 备份目录含未在 manifest 登记的额外文件 → 失败（防夹带）；
- production_reference 档的备份/恢复由 operator 显式提供 DSN（本机制不自动连接
  外部托管 PG）；local_product 档经 compose 服务名执行。

## 5. CLI

```
zhiwei backup create  [--output DIR]     # 退出码 0 成功 / 1 失败 / 2 用法错误
zhiwei backup verify  <DIR>
zhiwei restore verify <DIR> --isolated
```
