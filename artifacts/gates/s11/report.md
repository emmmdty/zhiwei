# S11 Gate Report（specs/s11 §7 Gate + §8 边界，Task 8 执行记录）

执行日期：2026-09-08　执行者：S11 executor（autonomous）　性质：fixture/offline only（零 live 模型请求）

> 证据原则：每条命令 verbatim 产物落盘于本目录；登记例外（ADR-012）显式列出；
> 未登记的缺失一律按未通过处理（AGENTS.md）。

## 0. 环境

```text
分支：main（工作区干净，提交见 git log）
Python: 3.11.15（uv）　Node: 22（web 镜像构建）
PostgreSQL: 17.6 @127.0.0.1:55433（compose zhiwei-local，全 18 服务）
CLI 环境（release 链路）：ZHIWEI_DATABASE_URL=…postgres@127.0.0.1:55433/zhiwei_s11_gate（maintenance 口径）
                          ZHIWEI_OBJECT_STORE_ROOT=/tmp/opencode/s11-gate/objects
未读取 .env；全程 fixture_only / offline 密封。
```

## 1. Gate 命令逐项（specs/s11 §7）

| # | 命令 | rc | 证据 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `docker compose -f deploy/compose/compose.yaml config --quiet` | 0 | — | 18 服务（含 include 图）可解析；**非启动 Gate**（既有纪律） |
| 2 | `docker compose … up -d --wait` | 0 | tests/e2e/local_product/test_compose_launch.py | 全栈 healthy/completed（garage/otel 按 running 判定，ADR-012） |
| 3 | `uv run zhiwei dev doctor --strict` | 0 | 会话记录 | DB revision 0026、object store、compose 配置全 ok |
| 4 | `uv run pytest tests/e2e/local_product tests/security/production_topology -q` | 0 | 19 passed | 启动 Gate e2e + 拓扑安全套件（slow marker 显式启用） |
| 5 | `uv run zhiwei ops fault-run --profile local-product --sealed` | 0 | ops/fault-run.json（JSON）+ fault-seal/ | 13 compose 场景全过（含 api_restart；区别性终态：fail_closed/degrade/recover）+ fixture 场景（崩溃窗口 #2/#11）由 tests/fault 承载 |
| 6 | `uv run zhiwei ops load-run --profile local_product --sealed` | 0 | ops/load-run.json（JSON）+ load-seal/ | 四类 workload 生产路径；**排队时延只对 discover 真实测量**（submit→claim；其余 workload 不声称排队样本，见 capacity.md §2 修订）；CPU/mem/IO 为 runner 侧 OS 采样 |
| 7 | `uv run zhiwei backup create --profile local-product` | 0 | ops/backup.json（备份目录在会话 tmp） | manifest + pg×3 + objects + keyring + claims |
| 8 | `uv run zhiwei restore verify --isolated` | 0 | ops/restore.json | 八项校验全过（RLS/伪造探针用 zhiwei_app 角色） |
| 9 | `uv run zhiwei release check --strict` | 0 | release-check.json | 74 文件 / findings=[]（11 条 claim 绑定：claim-seed.log 逐条 offline_verified 记录） |
| 10 | `uv run zhiwei release attest --dry-run` | 0 | attest-dryrun.json | signed:false；dry-run 未写任何文件 |

另：`make evals`、`make determinism`、全量 `pytest -q`（4320 passed）、`ruff check .`、
`pyright`（0 errors）在交叉检验轮全绿（见移交文档）。

## 2. Claim Registry 重放（专用库 zhiwei_s11_gate）

S9 runbook 在专用新库上重放（S9 共享测试库此后被 test fixture 清库，S9 报告 §0
已如实登记该清库行为）：`make evals` → 11 个 suite 密封（10 offline + legacy
fixture）→ `eval verify --all-sealed` → `deploy/seed_s9_gate_claims.py`（11 条：
10 offline_verified + longmemeval planned）→ release check。密封件 verbatim JSON
在本目录（sealed-runs.json 为聚合）。

## 3. 登记例外（ADR-012 口径，operator 可见）

| 例外 | 状态 | 复执行时点 |
| --- | --- | --- |
| E-R1 Temporal dev server 运行期下载 | compose Temporal digest pin 已交付（1.28.1@sha256:607d…）；eval/integration 的 SDK 下载版本**对齐**仍属复执行时点事项（SDK pin 随依赖链） | 下次依赖升级窗口 |
| garage/otel-collector 容器内探活缺失 | binary-only 镜像物理不可行；补偿=loopback 发布 + doctor TCP 探活 + 启动 e2e 断言 | 镜像提供探针工具时 |
| 三分钟演示的第 2 段（context wire/tamper）以测试锚点呈现 | verify context CLI 存在但演示脚本不构造实时篡改载荷（保持 fixture 边界） | 需要实时演示时由 operator 显式注入 |
| load-run 并发 >1 下多 dev server 资源竞争 | Gate 记录采用有界形态（concurrency=1）；ask/eval/sync-index 的并发档位经信号量真实生效（ramp），discover 串行（如实口径，capacity.md §2） | 专用容量演练环境 |
| Integration/Index/Eval workers 无 compose 服务 | spec §2 名单中的三个 worker 无仓内实现可承载（部署文件只承载已实现能力，spec §1）——不制造假容器；独立验收 F-P0 补登 | 对应域功能（webhook HTTP 端点批/检索索引批/eval worker 化）落地后补 compose 服务并冻结进契约测试 |
| tenant/security 十域套件未经 Compose 全量重跑 | 十域套件按域级恶意语料在 CI/test DB 面运行；production-topology 套件覆盖部署拓扑级 fail-closed 性质（security.md §3 互补声明） | operator 定期在 compose 栈复跑十域套件 |
| stuck approval 故障场景缺失 | spec §5 场景面未含独立场景；审批超时语义由 S2 approval 契约测试承载（check_approval expired 路径） | fault runner 扩展窗口 |
| 镜像层 CVE/policy 扫描未建 | plan Task 7「scan policies/images」当前只交付 secret sentinel 扫描（导出面）+ 加固契约；CVE 扫描属供应链工具链 | 引入扫描工具链时（须批准 dev 依赖） |
| Capability Runner 独立 node/pod policy 部分交付 | K8s 侧已给独立 NetworkPolicy（egress 收窄至 OPA/Temporal）；nodeSelector/affinity 属部署环境特化，以注释声明为 operator 假设 | 部署环境确定后由 operator 补 nodeSelector |
| clean-machine 裸 VM 复现 | 本 Gate 在开发容器以 down -v 全新栈复现；裸 VM 引导（OS 层）不在仓库交付面 | CI runner / operator 裸机 |

## 4. spec §8 边界声明（不做的事）

- 只有固定版本/环境的测试结果可写 "production reference verified"——本文档
  §1 的 10 条命令即该固定环境；
- 不写 HA（无跨节点证据）、不承诺 SLO（capacity.md 只记录观测）、
  部署 manifest 存在不等于生产上线。
