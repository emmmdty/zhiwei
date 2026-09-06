# 交接单 S1 tenancy e2e 修复轮 — 真实栈 bring-up 与六项根因收敛

> 日期：2026-09-05　执行方：本轮编码代理　裁决：operator 本轮逐项确认（ADR-014）
> 终态：**tenancy.spec.ts 13/13 passed（历史首次）**、runtime-approval.spec.ts 3/3、
> 全仓 Gate 全绿（pytest 3312/0、ruff 0、pyright 0、evals 822、determinism ✓）。
> S1 e2e 例外条目（Keycloak leg）解锁关闭，S1 Gate 的 e2e 项转绿。

## 1. 六项根因的处置（对照 s1-t6 §5）

| # | 根因 | 处置 | 证据 |
| --- | --- | --- | --- |
| §5-1 | T2 OIDC token exchange 缺陷 | **真实缺陷是 authlib×httpx2 类体系混用**（非 redirect_uri——该字段自 `9c43335` 即随 client 构造自动携带，冻结 FakeIdP 强校验通过即为证）。`_AuthlibTransportCompat` 双向归一 + 标量 timeout，见 ADR-014 决策 4 | RED：test_oidc_transport_compat.py（生产同构子进程 AssertionError）；GREEN：真实 Keycloak exchange 探针 OK（sub 正确返回） |
| §5-2 N-1 | bootstrap 零角色 vs journey 角色可见 | operator 裁决：**种子层解法**（裸 principal）+ journey signIn `.or()` 两态兼容；真实 OPA 双向实证不可满足（bare=True / org_owner=False） | authz.rego bootstrap_org_create；e2e 13/13 |
| §5-3 N-2 | 角色名脱节（owner vs org_owner） | session.tsx 镜像 LEGACY_ROLE_ALIASES 归一；App.tsx 改用 canonical 名（agent_builder 等） | "Signed in as: org_owner" 快照 |
| §5 N-3 | bootstrap 后 tenant header 不刷新 | App.tsx bootstrap 成功后 `await refresh()`（/me 重解析 org context）再 load | create workspace 201 |
| §5-4 N-4 | 种子不可复现 | `deploy/seed_identity_e2e.py`（幂等自纠、subject=KC user id）+ realm-template.json 固定 7 用户 + `deploy/serve_identity_e2e.py` | seed 输出 6 绑定齐备 |
| §5-5 N-5 | group 不渲染 | Members 组件加载并渲染 group 列表（GET /workspaces/{ws}/groups） | e2e core-platform 断言通过 |

## 2. 本轮新裁决（ADR-014，均由真实 OPA 双向实证后提请 operator）

1. **矩阵补 org_owner**：`workspace_policy.configure_workspace` += org_owner（组创建）。
   端点 RLS 上下文回退语义：workspace 作用域 actor 必须与路径对齐（冻结 scope 测试
   继续约束）；org 作用域 actor 落到路径 workspace（PEP 矩阵按 org 角色判定）。
2. **journey 修订**：signIn `.or().first()`；newuser-oidc（empty-state）；loading 延迟
   路由；removes-a-member 移至角色 journey 之后（原顺序状态流自毁——先删 member 再跑
   Member journey）。
3. **Keycloak realm**：journey 用户补 email（KC 26 首登强制 VerifyProfile 拦截回调）；
   `sslRequired: none`（docker 网桥来源非 localhost，external 档会下发 Secure cookie，
   明文测试链路 cookie_not_found）。

## 3. e2e 运行前置（可复现程序）

```bash
docker compose -f deploy/compose/compose.test.yaml --profile identity up -d --wait postgres opa keycloak
uv run alembic upgrade head
ZHIWEI_OIDC_ISSUER=http://localhost:8080/realms/zhiwei uv run python deploy/seed_identity_e2e.py
# 后端（env 组合见 deploy/serve_identity_e2e.py docstring）
ZHIWEI_PROFILE=test ... uv run python deploy/serve_identity_e2e.py &
npm --prefix apps/web run test:e2e -- tenancy.spec.ts
```

**注意**：全量 pytest 的 `migrated_database` fixture 会 downgrade→upgrade 重建 schema，
清掉 e2e 种子——**pytest 之后跑 e2e 必须重新 alembic（如被降级）+ seed**。另：本机
compose 容器未发布 55432 端口时，宿主机原生 postgres 若占用了 55432，tests/app 实际
连接的是它（本轮曾因此误判「schema 消失」）——bring-up 前先核对
`docker port zhiwei-s0-postgres-1`。

**§3.1 复执行补记（2026-09-06，S10 Gate E2 关闭轮）**——两条曾造成误诊的精确性缺口：

1. `ZHIWEI_IDENTITY_DATABASE_URL` 必须指向 **`zhiwei_identity` 角色**（如
   `postgresql://zhiwei_identity@127.0.0.1:55432/zhiwei_test`），不能与
   `ZHIWEI_DATABASE_URL`（zhiwei_app）同值：`oidc_login_attempts` 等身份数据面的
   GRANT 授的是 identity 角色，配成 app DSN 时 `/auth/login` 以 500
   `permission denied for table oidc_login_attempts` 失败（现象是登录页永不出现，
   e2e 全部 timeout 在 `#username`）。
2. 本机 shell 若存在 HTTP 代理（env `http_proxy/https_proxy`），Vite(5173)、
   后端(8000)、Keycloak(8080) 的 localhost 流量都会被代理劫持（curl 得 502、
   浏览器同样）——playwright 运行前必须 `export no_proxy/NO_PROXY=localhost,127.0.0.1`。
   曾据此误诊「Vite 进程死亡/陈旧服务」。

复执行证据：2026-09-06，HEAD 54fd3dd，按本节程序（identity 角色 DSN + NO_PROXY）
`tenancy.spec.ts` **13/13 passed**（18.7s）。

## 4. Gate 输出（2026-09-05，干净库）

```text
uv run pytest -q                                        3312 passed, 6 skipped, 20 deselected
uv run ruff check .                                     All checks passed
uv run pyright                                          0 errors, 0 warnings
make evals                                              822 项校验全部通过
make determinism                                        ✓ 逐字节一致
npm --prefix apps/web run test:e2e                      16 passed（tenancy 13 + runtime-approval 3）
```

## 5. 遗留（登记，不阻塞）

- S4/S6/S7/S8 e2e 例外条目维持（解锁时点最迟 S10，四要素在案）；本轮已补 operator
  确认登记。
- AuditLog 仍为占位（audit-events 端点未交付）；group 空态展示未做。
- live attestation、生产 egress 组装接线维持「计划实现」。
