# 第三方数据、模型、服务与能力清单

> 初始 provenance 记录，不是法律意见。Capability admission 与 S9/S11 release check 必须对实际版本、
> license、source digest、SBOM、服务条款和再分发边界复核。

| 资产/服务 | 用途 | 仓库/部署策略 | 当前状态 |
| --- | --- | --- | --- |
| `BAAI/bge-small-zh-v1.5@7999e1d...` | 文档 dense CPU 检索 | 不提交权重；固定 revision、model card、files digest/license | 计划；下载时复核 |
| `BAAI/bge-reranker-base@2cfc18c...` | CPU rerank | 同上 | 计划；下载时复核 |
| SCIP/tree-sitter | 代码索引/解析 | 固定工具与 grammar 版本、记录 license/SBOM | 计划 |
| OpenSearch | hybrid search/ACL filter | 容器版本 pin；索引可重建 | 计划 |
| Garage | local-product S3-compatible store | 容器 digest pin；不将其等同生产托管 S3 | 计划；替代已 archive 的 MinIO 默认项 |
| Temporal | durable execution | local 使用 dev server；production external service | 计划；需 crash/replay 验证 |
| Keycloak / OPA | local identity/policy | reference config，生产可替换标准 IdP/PDP | 计划 |
| MCP Registry/server/Agent Skills | 能力发现与导入 | 不因目录存在自动执行；逐版本 license/SBOM/admission | 计划 |
| BIRD Mini-Dev | SQL 外部诊断 | 不提交原始数据；只提交获取说明、checksum、adapter/report | 计划；运行前复核许可/scorer |
| LongMemEval / LoCoMo | memory 外部诊断 | 遵守原数据许可，和内部 suite 分开报告 | 计划 |
| Promptfoo / Inspect AI adapters | security/eval 外部诊断与日志互操作 | adapter/version pin，不替代 ZhiWei runtime truth | 计划 |
| OpenCode Go 模型输出 | 有界 live attestation/eval | 请求/输出默认私有；仅 release checker 生成脱敏 artifact | 配置声明；用户接受已记录使用风险，不声称专项授权 |
| `evals/novels` / `evals/risk` | 自建历史 FactQA/Risk 资产 | 由脚本生成并 asset lock | 已有资产；发布前复核素材/声明 |
| GitHub repositories/API payloads | code knowledge reference | 默认使用自有/明确授权 synthetic repo；私有 token/content 不发布 | 计划 |

导入的每个 Tool/MCP/Skill/OpenAPI/Agent provider 都会生成独立 `AdmissionRecord`，至少包含来源、digest、
publisher、license、SBOM/vulnerability、network、data class、effect/risk、contract/security result。目录列表
不是本清单的替代品。

Python/Node 依赖不复制进源码许可证。OCI/Release 产生分发物时生成 dependency/license/SBOM 清单；
Apache-2.0 只覆盖本项目原创代码和文档，不覆盖第三方模型、数据、服务或 Capability 条款。
