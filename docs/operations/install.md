# 安装与启动（specs/s11 §2/§7；Task 8）

> 边界（specs/s11 §8）：本文档描述的是**单机 CPU-only local product**。
> Docker startup 只允许 fixture/replay；live 模型调用是 operator 显式动作
> （specs/s11 §6），不在安装序列内。

## 1. 前置条件

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Docker + Compose | ≥ 27 / v2.20+（include 语法） | 全组件容器栈 |
| uv | 0.11+（Python 3.11） | 应用依赖与 CLI |
| Node | 22（仅构建 web 镜像时需要；compose build 自包含） | 前端 |
| 内核 | `vm.max_map_count ≥ 262144`（OpenSearch） | Linux/WSL2 默认满足 |

## 2. 一条命令序列安装（clean machine）

```bash
git clone <repo> zhiwei && cd zhiwei
make evals                                  # 重建并校验冻结基准资产（1205 项 / 32 资产）
uv run zhiwei dev up                        # 全栈拉起（= docker compose -p zhiwei-local -f deploy/compose/compose.yaml up -d --wait --build）
uv run zhiwei dev doctor --strict           # 就绪诊断（compose 配置 + DB revision + object store）
```

首次 `dev up` 需要拉取镜像与构建应用镜像（约 5–15 分钟，取决于网络与机器）；
`--wait` 会等到全部长驻服务 healthy（garage/otel-collector 为 binary-only 镜像，
按 running 判定——ADR-012 在册例外，补偿控制见 docs/operations/security.md §4）。

## 3. 启动后的固定检查

```bash
curl -s http://127.0.0.1:8090/healthz       # {"release_mode":"fixture_only","profile":"local_product"}
uv run zhiwei dev doctor --strict --format json
uv run pytest tests/e2e/local_product -m slow -q    # 启动 Gate e2e（18 服务健康 + fixture 面探活）
```

## 4. OIDC 浏览器旅程（可选，交互式 UI 登录需要）

issuer 固定为 `http://keycloak.local:8081/realms/zhiwei`（compose 内 API 经
host-gateway 到达宿主 loopback 发布的 8081）。浏览器侧需要一条 hosts 条目：

```bash
echo "127.0.0.1 keycloak.local" | sudo tee -a /etc/hosts
```

产品入口（浏览器旅程走 TLS——session cookie 是 `__Host-` 前缀 + Secure 冻结契约，
纯 HTTP 源浏览器拒绝该 cookie）：

```text
https://keycloak.local:8443/
```

- TLS 证书是 proxy-tls-certgen 一次性生成的 dev 自签名（CN/SAN=keycloak.local），
  浏览器会告警，接受即可；真实部署由 operator 换发正式证书（k8s reference 走
  Ingress TLS）。
- journey principal（owner-oidc 等）经种子脚本预供给（JIT 未实现）：

```bash
ZHIWEI_E2E_ADMIN_DSN="postgresql://zhiwei_migrator:zhiwei-dev-pg-only@127.0.0.1:55433/zhiwei_identity" \
  ZHIWEI_OIDC_ISSUER="http://keycloak.local:8081/realms/zhiwei" \
  uv run python deploy/seed_identity_e2e.py
```

- HTTP `127.0.0.1:8090` 保留给非浏览器探活与 e2e 套件。
- 已知缺陷（2026-09-08 登记，修复待设计方裁决）：登录后的组织/工作区列表依赖
  SECURITY DEFINER 函数 `zhiwei_principal_memberships`——FORCE RLS 对非超级用户
  definer 同样生效（CI 的 migrator 是引导超级用户故测试不可见），登录后
  `/api/v1/me` 的 organizations 恒为空、UI 停留在 Create organization 引导页。

Keycloak 管理台：http://keycloak.local:8081（zhiwei-admin / 本地开发占位口令，
operator 覆盖见 deploy/compose/compose.yaml 环境段）。

## 5. 停止与清理

```bash
uv run zhiwei dev down              # 移除本项目容器（保留数据卷）
uv run zhiwei dev down --volumes    # 连数据卷一起删除（显式 opt-in）
```

## 6. 已验证边界（诚实口径）

- 本安装序列在开发容器（Ubuntu + Docker + 4 核）上以「干净 compose 状态
  （down -v 后重来）+ 全量启动 Gate e2e」复现通过（S11 Gate 记录，见
  docs/handoffs/s11-production-reference.md）；
- 全新 VM（无 docker/uv）的引导属于 OS 层（apt/docker 安装），不在本仓库
  交付面内——未在裸 VM 复现的部分不宣称 verified；
- `make evals` 与 `make determinism` 是冻结资产承诺（AGENTS.md），任何环境
  必须全绿。
