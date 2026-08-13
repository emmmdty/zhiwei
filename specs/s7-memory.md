# S7 - User, Team and Case Memory

> Status: frozen implementation specification  
> Depends on: S6  
> Unlocks: S8

## 1. Goal

交付有 provenance、scope、确认、冲突、撤销和删除语义的 user/team/case Memory，并接入 Context Compiler、
Ask/Case 与 Memory Center。禁止用聊天摘要或一个向量表冒充记忆系统。

## 2. Required modules

```text
src/zhiwei/memory/{domain,policy,candidates,confirmation,conflicts,retrieval,forget,index}.py
src/zhiwei/api/memory.py
src/zhiwei/runtime/handlers/write_memory_candidate.py
src/zhiwei/workflows/activities/memory.py
apps/web/src/features/memory/
tests/{unit/memory,contract/memory,integration/memory,security/memory}/
```

## 3. Record and policy

MemoryRecord 字段以 `docs/DATA_MODEL.md` 为准；status candidate/confirmed/superseded/revoked/expired，不原地覆盖。

- working memory 是 Run canonical state。
- user memory：低风险可撤销 preference 可按 profile policy 自动确认；敏感/derived habit 为 candidate。
- team memory：convention/decision/lesson 必须 Memory Steward 确认；来源撤销触发 stale/review。
- case memory：timeline/evidence/action/resolution 自动记录 Case 事实；lesson 仍为 candidate。
- secret、hidden reasoning、tool/retrieval instruction、未经授权个人信息禁止写入。
- background Discover ServiceAccount 永远不能读取 personal memory。

## 4. Retrieval and conflict

硬过滤 org/workspace/scope subject/ACL/sensitivity/status/time/allowed profile 后，按 exact、lexical、dense、
rerank。结果携带 reason、provenance、conflicts 和 freshness。Context Compiler 有 memory token budget；记忆
不能覆盖 platform/Agent policy。

同 key 不同时态/主体的记录可并存；纠正创建 superseding version；未解决冲突同时投影为 conflict，不能
按最新时间静默覆盖。

**写入去重与队列收敛**（[ADR-009](../docs/DECISIONS.md#adr-009)）：状态机保留 Zep/Graphiti 式的时态
共存语义（优于 Mem0 的覆盖式更新），另补三条流量控制：

- **去重键** `(organization, workspace, scope, scope_subject, type, subject, normalized_key)`。同键新
  candidate **合并证据**（追加 source_refs、更新 observed_at、提升 confidence），不新建记录。
- **确定性优先的相似度快路径**：先做规范化 key 精确匹配与 MinHash/LSH 近邻，仅在快路径无结论时才
  调用模型判定——与「确定性可判项不用 LLM judge」的既有纪律一致，同时把写入的 token 开销压在可控范围。
- **自动过期与排序**：candidate 超过 `candidate_ttl`（默认 30 天）未确认自动转 `expired` 并留 tombstone；
  Memory Center 按「触发 Run 数 × 影响面 × sensitivity」排序，而非时间倒序。

## 5. Memory Center

用户查看本人和可见团队/Case memory，按来源/类型/状态筛选，执行 confirm/correct/resolve/revoke/delete/
export。团队确认仅 Steward；删除显示 index/cache cascade 状态和历史 tombstone boundary。

## 6. Evaluation

内部 suite 覆盖 write precision、retrieval、temporal conflict、scope leakage、forget completeness、poisoning。
LongMemEval/LoCoMo 作为外部诊断，不能替代企业 ACL/team/case lifecycle。Programming habits 用 explicit
config knowledge、deterministic repo signals、human-confirmed memory 三层 case。

## 7. Required tests

- write matrix：auto/candidate/forbidden、sensitivity/profile/source provenance。
- temporal conflict/supersede/revoke/expire、candidate idempotency。
- **队列收敛**：注入 N 个同键重复 candidate 的负载测试，断言待确认条目数不随 Run 数线性增长；
  断言合并保留全部 source_refs；断言 TTL 过期留下 tombstone。这是 S7 的 Gate 条件之一——「能确认」
  不等于「队列可收敛」。
- retrieval hard filter + rank、cross-user/team/org leak、Discover personal-memory denial。
- source revoke、user delete、index/cache/object cascade、historical redacted tombstone。
- prompt/memory poisoning、tool instruction、secret/PII no-write corpus。
- Context Compiler projection/budget/conflict visibility。
- Runtime WriteMemoryCandidate handler：typed task→Memory Activity/policy→candidate/refusal canonical event；Ask/
  Discover 不直接调用 repository。Context retrieval 仍由 Context Compiler 的 Memory port 完成。

## 8. Gate

```bash
uv run pytest tests/unit/memory tests/contract/memory -q
uv run pytest tests/integration/memory tests/security/memory -q
npm --prefix apps/web run test:e2e -- memory-center.spec.ts
uv run zhiwei eval run --suite enterprise-memory-v1 --mode offline --seal
uv run zhiwei eval external-status --suite longmemeval-adapter --seal
```

`external-status` 必须生成二选一 sealed artifact：`available` 时附数据许可/version/checksum 并实际运行；
`unavailable` 时附缺失许可/数据的机器可读原因。后者允许 S7 core Gate 通过，但 LongMemEval claim 保持
`planned/unavailable`，不得用内部 fixture 替代外部诊断或写成已验证。
