# 安全基线与残留风险（specs/s11 §5；Task 7）

> 状态：production-topology 套件口径冻结（tests/security/production_topology/）。
> 本文档记录**已验证的拓扑面结论、精确测试版本与残留风险**；失败不允许用
> allowlist 掩盖（plan Task 7 checkbox 3）。

## 1. 测试版本（spec §8：固定版本才可写 verified）

| 组件 | 版本（tag@digest，deploy/compose/ 为准） |
| --- | --- |
| postgres | 17.6-alpine@sha256:ef257d85… |
| keycloak | 26.1@sha256:be6a8621… |
| opa | 1.19.0-debug@sha256:ec3c7a29… |
| otel-collector | 0.128.0@sha256:1ab0baba… |
| temporal | 1.31.2（server@sha256:b5ecdb82… + admin-tools@sha256:dbc5fcd6…，E-R1 方案 b） |
| opensearch | 2.19.2@sha256:69588c66… |
| garage | v1.1.0@sha256:fdb8272f… |
| redis | 7.2-alpine@sha256:dfa18828… |
| nginx | 1.29-alpine@sha256:56168782… |
| python 基础镜像 | 3.11-slim@sha256:9534e5a8… |
| node 构建镜像 | 22-slim@sha256:83f487e0… |
| uv | 0.11.8@sha256:3b7b60a8… |

## 2. production topology 已验证面（tests/security/production_topology/）

- 未认证 API 请求经 proxy → 401/403（认证面前置；healthz 是唯一公开端点，
  响应体仅 release_mode/profile 两个部署声明）；
- 应用容器**实测**非 root（docker exec id，不只看声明）；管理端口**实测**仅
  loopback 发布（docker compose ps Publishers）；
- production-reference 渲染产物：NetworkPolicy Ingress+Egress 双向、应用容器
  runAsNonRoot + readOnlyRootFilesystem、ingress TLS 声明；
- sentinel 扫描（pg dump / 备份导出 / 前端 bundle / otel 导出配置）：已知
  secret 值与私钥标记零出现；keyring 材料只允许出现在备份 secrets/ 内。

## 3. 与既有安全套件的关系

十域安全套件（tests/security/{tenancy,identity,knowledge_acl,model_egress,
telemetry_redaction,runtime_isolation,capabilities,discover_identity,
evidence_access,memory}）继续作为**域级**恶意语料回归面运行；production-topology
套件只覆盖**部署拓扑级**性质（fail-closed 边界、导出面、加固实测），两者互补，
不互相替代。

**十域套件 compose 重跑（2026-09-08，例外 #4 解除）**：`uv run pytest tests/security -q`
以 `ZHIWEI_TEST_{ADMIN,APP,IDENTITY}_DSN` 指向 compose postgres（127.0.0.1:55433/zhiwei_test）、
`ZHIWEI_OPA_BASE_URL` 指向 compose OPA（127.0.0.1:8182）——**451 passed / 9 deselected**。
环境语义记录（非代码改动，跑前临时授予、跑后逐项恢复并核对快照）：
CI 测试栈（compose.test.yaml）的 `POSTGRES_USER=zhiwei_migrator` 是引导超级用户，
而产品栈 init 脚本的 migrator 是普通角色——套件 fixture 的种子插入（FORCE RLS 表）与
SECURITY DEFINER 函数依赖 CI 语义，因此临时 `ALTER ROLE zhiwei_migrator BYPASSRLS`、
`ALTER ROLE zhiwei_app/zhiwei_identity NOINHERIT`、`REVOKE zhiwei_migrator FROM
zhiwei_app/zhiwei_identity`，跑完即恢复 init-local-product.sh 声明的姿态。
**发现（登记为产品栈安全姿态问题，待批次消化）**：init-local-product.sh 把
`zhiwei_migrator` 成员资格授予 `zhiwei_app`/`zhiwei_identity` 且二者默认 INHERIT——
应用/identity 角色在产品栈中默认持有迁移特权并可 `SET ROLE` 提升到 migrator；
CI 测试栈的 init-test-roles.sql 无此授权（NOINHERIT、无成员关系）。

## 4. 依赖漏洞扫描（复验轮 2026-09-08）

- `pip-audit`（uvx 运行，仓库零依赖改动）对全依赖树（148 包，uv export 口径）
  扫描：**零已知漏洞**；
- **trivy 镜像层扫描（2026-09-08，docker 恢复后执行）**：
  `docker run aquasec/trivy:latest image zhiwei/app:local-product`——
  LOW 60 / MEDIUM 68 / HIGH 55 / CRITICAL 3 / UNKNOWN 5；CRITICAL 全部为
  perl-base（CVE-2026-13221 / CVE-2026-42496 / CVE-2026-8376，基础镜像 Debian
  层，当前无修复版本）。应用依赖树以 pip-audit 为准（零已知漏洞）；
  镜像层 CVE 属基础镜像升级范畴，不作本仓库代码缺陷表述。

## 5. 残留风险（诚实登记，不含糊）

1. **OpenSearch/Garage 容器内探活缺失**（binary-only 镜像，ADR-012 在册）：
   --wait 按 running 判定；补偿 = loopback 发布 + doctor --strict TCP 探活。
2. **local-product 的 OIDC 全旅程**依赖浏览器 hosts 条目（keycloak.local，
   install.md 记录）；SCIM/OIDC 域级攻击面由 identity 域套件覆盖（MockTransport
   层），compose 内真实 IdP 旅程由 identity e2e profile 承载——两者都未在
   production-topology 套件内重复。
3. **RDS/备份通道**：备份目录含 keyring 原文（恢复材料，§1 事实源边界）——
   备份目录本身必须按 secret 处置（operator 纪律，机制上无法强制）。
4. **deny 面的 OPA 依赖**：OPA 完全停机时 PEP fail closed（deny），但**只读 GET**
   面在策略引擎不可用时的行为由域级策略决定（R1-T4 冻结语义），不在本套件重复。
5. **otel-collector 用户为镜像默认 10001**（file exporter 卷属主依赖首挂继承），
   未额外声明 compose user——镜像内目录属主是 vendor 契约。

## 5. 明确不做

- 不承诺渗透测试/外部红队等效性（本套件是回归面，不是审计结论）；
- 不对 K8s 集群做运行时验证（production-reference 是渲染级 reference，
  spec §8：部署 manifest 存在不等于生产上线）。
