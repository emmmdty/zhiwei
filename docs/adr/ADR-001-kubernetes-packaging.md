# ADR-001: Kubernetes 参考实现采用 Kustomize

> 状态: accepted（S11-T2 spike 裁决，2026-09-08）
> 事实源: specs/s11-production-reference.md §2/§3、docs/superpowers/plans/2026-08-12-s11-production-reference.md Task 2

## 背景

S11 Task 2 要求在 Kustomize 与 Helm 之间择一并记录决策（不做两套）。选型必须满足：
overlays 分档（local-reference / production-reference）、版本 pin、secret 外部化、
CI 可渲染校验。

## 决策

采用 **Kustomize**。

1. **零新增工具链**：kubectl 内置 kustomize（当前 runner: kubectl 1.36.1 / Kustomize
   v5.8.1），渲染校验 `kubectl kustomize` 即可进 CI；Helm 需要额外二进制与模板渲染
   依赖，违反「不引入新的第三方依赖」约束下的最小工具链原则。
2. **overlays 与 spec 语义同构**：spec §2 的 `overlays/local-reference` 与
   `overlays/production-reference` 正是 kustomize overlay 模型——base 承载共享的
   stateless 应用面与策略，overlay 承载环境差异（镜像 tag、副本、外部端点、ingress）。
3. **replaceable deps 的表达**：production-reference 的外部托管依赖以 ConfigMap
   endpoint + Secret 引用表达，用 patch 覆盖即可替换供应商；无需 chart 模板层。

## 后果

- 部署文件全部是原样 YAML，渲染产物可 diff（对比 Helm 模板不可静态审阅）。
- base/ 的默认值即 production-reference 缺省；local-reference overlay 以 patch 收窄。
- 不引入 Helm release/skip-hooks 语义；迁移 Job 以 K8s Job 显式编排（配合 T3 升级文档）。
- 版本 pin：第三方镜像 tag+manifest digest 双 pin；应用镜像在 production-reference
  也必须 digest pin（CI 校验，tests/contract/deploy/test_kubernetes_reference.py）。
