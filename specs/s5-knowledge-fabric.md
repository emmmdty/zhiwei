# S5 - Source-native Knowledge Fabric

> Status: frozen implementation specification  
> Depends on: S4  
> Unlocks: S6

## 1. Goal

交付文档/表格、代码/GitHub、PostgreSQL 和 API/MCP resource 的 Source Ledger、源原生索引、Context Graph、
Knowledge Planner、ACL 与 freshness，使后续 Ask/Discover 获取可追溯 Evidence 候选，而不是匿名 chunks。

## 2. Required modules

```text
src/zhiwei/knowledge/
  {ledger,contracts,sync,watermarks,acl,planner,query,freshness,graph}.py
  connectors/{files,github,postgres,api_resource}.py
  parsers/{documents,tables,scip,treesitter}.py
  indexes/{opensearch,lexical,dense,fusion,rerank}.py
apps/web/src/features/knowledge/
src/zhiwei/runtime/handlers/retrieve.py
src/zhiwei/workflows/activities/knowledge.py
tests/{fixtures/knowledge,unit/knowledge,contract/knowledge,integration/knowledge}/
```

## 3. Source Ledger

SourceObject/Version/Locator 保存 immutable original、digest、source-native identity、ACL/version、classification、
observed/valid time、sync watermark、connector/parser/index versions、parent/tombstone。ObjectStore manifest 是
内容事实，OpenSearch/Context Graph 可重建。

webhook 触发增量 sync，reconciliation 修复丢事件；delete/revoke 优先。更新创建新 version，旧 Evidence
标 stale 但历史 Run 不重写。

## 4. Native indexing

- docs/tables：document→section→paragraph/table→row/cell/code-block，保留 page/title path/span。
- code：Repository@Commit、File、Symbol、definition/reference/implementation、imports/dependencies/tests、
  commit/diff/blame；SCIP first，tree-sitter/exact search fallback。
- GitHub：GitHub App permissions、webhook+reconcile；PR/issue/review/check stable locator。
- PostgreSQL：schema snapshot、read-only AST/typed query、timeout/row/byte limit；query result 可冻结。
  数据源在接入时必须声明可达的 `reproducibility_level`（[ADR-003](../docs/DECISIONS.md#adr-003)）：支持
  时间点快照的标 `replayable`；只读副本等无稳定 snapshot id 的走 `copy_frozen`（结果集副本经 canonical
  编码后写 Object Store 并 digest）；两者都不可得的标 `reference_only`，且该源产出的 Evidence 不得支撑
  Fact 类 claim。**不允许用「若支持」留白**——接入时不声明即视为 `reference_only`。
- API/MCP resource：observation 必须进入 Source Ledger 才可作为 Evidence。

## 5. Retrieval

KnowledgeQuery 包含 source/entity/time/exact identifier/filter/top-k/evidence requirement。文档 BM25+dense+RRF+
rerank；代码 exact path/symbol/ref/commit 优先；DB 优先 schema-grounded query。每个 result 返回 score breakdown、
SourceVersion/Locator、ACL/freshness/classification。

ACL 在 candidate generation 前过滤，hydration 后 re-check；unknown/stale ACL fail closed。Context Graph 用 PG
typed temporal edges并要求 source refs，不能直接出 Evidence。

## 6. Reference corpus and suites

新增自有/synthetic enterprise corpus：产品规范文档、表格/DB、至少两个 commit/PR/issue/review 的代码仓库、
跨源冲突/变化、两个 Organization/多个 Workspace ACL。不得借 120 题声称覆盖这些能力。

Suite：doc/table、code/GitHub、cross-source、ACL/freshness。每个包含 exact locator targets、blind holdout、
metamorphic rename/move/update/revoke 和 latency/memory report；CPU BGE revision 固定。

## 7. Required tests

- parser/index determinism、locator replay、SCIP unavailable fallback、binary/large file limits。
- webhook duplicate/out-of-order/missing + reconcile；force push/delete/permission revoke。
- ACL pre/post、index stale、cross-org query、source disabled、classification mismatch。
- BM25/dense/RRF/rerank assembly spy 与 source-native score priority。
- OpenSearch loss/rebuild/alias switch；PG graph rebuild；source version Evidence stale transition。
- Runtime Retrieve handler：Task input→Knowledge Activity→typed candidates/artifact→canonical task events；cancel/
  ACL/freshness/failure 走正式 Run 状态，不由 Ask 直接访问 Knowledge service。

## 8. Gate

```bash
uv run pytest tests/unit/knowledge tests/contract/knowledge -q
uv run pytest tests/integration/knowledge tests/security/knowledge_acl -q
uv run zhiwei source sync --all-reference --reconcile
uv run zhiwei eval run --suite knowledge-doc-v1 --mode offline --seal
uv run zhiwei eval run --suite knowledge-code-github-v1 --mode offline --seal
uv run zhiwei eval run --suite knowledge-cross-source-v1 --mode offline --seal
uv run zhiwei eval run --suite knowledge-acl-freshness-v1 --mode offline --seal
```

至少一个 suite 通过 `AgentRuntime → Retrieve TaskHandler → Temporal Activity → Knowledge Planner → canonical
event` 执行，不允许只测 planner function。

## 9. Claim boundary

只声明固定 corpus/suite 的 retrieval、locator、ACL/freshness 结果。不声明任意代码库/文档格式理解，Context
Graph 也不能被写成企业知识真相。
