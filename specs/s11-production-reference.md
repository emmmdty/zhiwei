# S11 - Local Product and Production Reference

> Status: frozen implementation specification  
> Depends on: S10  
> Unlocks: evidence-backed release

## 1. Goal

交付 clean-machine 可启动的完整 local-product、可替换外部依赖的 Kubernetes production reference、升级/
备份恢复/故障/负载/安全报告和最终 sealed release。部署文件只承载已实现能力，不制造 HA/SLO。

## 2. Deployment artifacts

```text
deploy/compose/{compose.yaml,profiles,configs,secrets.example}/
deploy/kubernetes/{base,overlays/local-reference,overlays/production-reference}/
deploy/observability/{otel,alerts,dashboards}/
docs/operations/{install,upgrade,backup-restore,incident-response,capacity,security}.md
```

local-product 包含 reverse proxy、Web、API、Agent/Integration/Index/Eval workers、Outbox Dispatcher、Capability
Runner、PostgreSQL、Temporal dev、OpenSearch、Garage、Redis、Keycloak、OPA、OTel backend、reference MCP/
OpenAPI/source。所有 image digest pin、非 root、health/readiness、资源限额；管理端口仅 loopback/internal。

## 3. Production reference

- stateless application replicas，worker 按 queue/trust/workload 扩展。
- external managed/HA PostgreSQL、Temporal、OpenSearch、S3、OIDC/SCIM、Vault/KMS、Redis、OTel。
- ingress/WAF/TLS、NetworkPolicy、Pod security、PDB、resource request/limit、migration job、rollout/rollback。
- Temporal persistence 与业务 PG 使用不同 account/database；Capability Runner 独立 node/pod policy。
- 配置检查 external version/feature matrix；不自建数据库/IdP/KMS operator。

## 4. Upgrade and recovery

- Alembic expand/migrate/contract；event/manifest backward reader；Temporal version marker；OpenSearch rebuild+
  alias switch；Agent/Tool/Skill version pin。
- backup PG、ObjectStore、SecretBackend recovery material/procedure、Temporal config/persistence scope、version/
  release manifests；Redis/search 可重建。
- restore 到隔离环境，校验 tenant/RLS、artifact digest、canonical projection、workflow reconciliation、search
  rebuild、secret rotation 和 release claims。

## 5. Fault/load/security matrix

- kill/restart API/worker/Temporal/PG failover/Redis/OpenSearch/Object/OPA/OTel/reference tool。
- network partition、slow provider、duplicate webhook/outbox/signal、stuck approval、artifact corruption、effect_unknown。
- fixed workload：concurrent interactive Ask、scheduled Discover、sync/index、eval；测 queue/p50/p95/error/recovery/
  tokens/cost/resource，先报告再提出 SLO。
- tenant/security suite 在 production topology 重跑；backup/trace/log/object 扫 secret/PII。

## 6. Demo/release

默认 fixture/replay 且全 UI 标识；Docker startup 不做 live。显式 live run 经 OpenCode Go preflight，产出 sealed
model/source/usage/cost/latency/failure/Evidence artifact。三分钟演示走真实 local-product runtime：Ask cross-
source Evidence、context wire manifest/tamper、Discover→Case→approval→ActionReceipt、Studio/Capability binding、
ChangeBrief third App。

## 7. Gate

```bash
docker compose -f deploy/compose/compose.yaml config --quiet
docker compose -f deploy/compose/compose.yaml up -d --wait
uv run zhiwei dev doctor --strict
uv run pytest tests/e2e/local_product tests/security/production_topology -q
uv run zhiwei ops fault-run --profile local-product --sealed
uv run zhiwei ops load-run --profile local-product --sealed
uv run zhiwei backup create --profile local-product
uv run zhiwei restore verify --isolated
uv run zhiwei release check --strict
```

clean Ubuntu/WSL2 runner 复现 install；CPU-only、无真实 key 跑完整 fixture journey。live 和 destructive fault 由
操作者显式运行，不在普通 CI 自动执行。

## 8. Release boundary

只有固定版本/环境的测试结果可写“production reference verified”。没有跨节点 HA/长期运行证据就不写 HA；
没有测量就不承诺 SLO。部署 manifest 存在不等于生产上线。
