# P1 · 内部数据接入 Spec：SQL / 报表 / 文档 / API + ACL + 语料包

## 1. Goal / Non-Goals
**Goal**：四种内部数据形态接入，全部产物可溯源（QueryReplay/CellRef/DocRef/ApiCallRef）；text-to-SQL 安全+回查；**embedding 密集检索（BM25 兜底）**；**查询结果/LLM 响应缓存**；**数据源级 ACL 从本阶段内置**；四大名著语料包 v1（西游记）。
**Non-Goals**：不做跨库联邦；不做多租户（单实例）。

## 2. 契约（详见 docs/DATA_MODEL.md / docs/PERMISSIONS.md）
- `datasources/sql/`：schema_provider（表/列/注释/样例注入）、validator（仅 SELECT/禁 DDL/DML/多语句/危险函数/LIMIT<=500/超时30s）、connector（只读账号 + 执行 + 回查）、rewrite（错误重写 <=2 次 -> 无法查询）。
- `datasources/reports/`：Excel/CSV 直读（openpyxl/pandas）行列定位；PDF 报表（anydoc 锁版本）转结构；表头映射（LLM 辅助 + 人工确认缓存）。
- `datasources/documents/`：anydoc 预处理 + 分块 + **embedding 索引（P1 核心；OpenAI 兼容 embedding 端点或 GPU 服务）** + BM25 兜底；检索协议统一（Retriever Protocol），rerank 为 P2。
- `core/cache.py`：查询结果缓存（SQL+参数指纹+TTL）+ LLM 响应缓存（请求指纹+TTL，语义命中默认关闭）；缓存命中记录 trace。
- `datasources/apis/`：OpenAPI -> tool 注册（复用 tool_gateway 幂等/预算/角色）。
- `datasources/acl.py::enforce()`：组授权/表白名单/列白名单/脱敏/行过滤（查询执行前强制）。
- `core/trace.py`：四类 TraceRef 模型 + schema + 契约测试。
- 语料包：`evals/novels/xiyouji/`（81 难 SQLite + CSV + 改编文档 + perturbation_manifest.json）。

## 3. 测试计划
1. validator：合法 SELECT 通过；恶意输入 100% 拦截（DDL/DML/多语句/危险函数/超长）。
2. connector：schema 注入 prompt 断言；重写循环；回查行哈希稳定；只读账号参数断言。
3. reports：Excel 多 sheet/合并单元格/中文表头 -> CellRef 定位正确；PDF 报表表格保留；损坏文件分类处理。
4. documents：分块 + BM25 命中；DocRef 引用正确。
6. apis：OpenAPI -> tool；ApiCallRef；凭据加密。
7. acl：组授权拒绝；表/列白名单；脱敏改写；行过滤注入；拒绝记录审计。
8. trace：四类 schema 正反向样例。
9. 集成（FakeLLM + 内存库）：问题 -> schema 感知 SQL -> 执行 -> 溯源答案；"无法查询"不编造。
10. 西游记语料包：81 难表查询（"第 80 难是什么"）E2E（真实 DeepSeek）。

## 4. 验收标准
- [ ] sql_safety_block_rate=1.0（安全 fixture 全拦截）。
- [ ] 四种形态接入端到端 + 溯源契约通过。
- [ ] ACL 演示：analyst 可查、viewer 拒绝；敏感列脱敏。
- [ ] 验收集 A1/A2 通过（第 80 难 + 水难占比）。

## 5. 风险
- 复杂业务语义误写 SQL -> schema 感知 + 重写 + 回查 + 复杂查询降级"请用户确认"。
- 报表结构多样 -> 归一化 + 复杂表整表引用 + 表头映射缓存。

## 6. 工作量
约 12 个工作日（含 embedding 检索与缓存）。
