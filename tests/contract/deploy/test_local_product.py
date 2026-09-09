"""S11-T1 部署契约：local-product compose 结构（设计/验收方冻结口径，A 档契约面）。

事实源：specs/s11-production-reference.md §2/§7、docs/review/findings/R8-platform-s11.md
（F-R8-01 拆批、F-R8-02 Temporal compose 化）、docs/review/s11-handoff-input.md §2。

本文件是契约，不是可调整的障碍（AGENTS.md）。冻结的断言面：

1. **镜像双 pin**：compose 内每个 service 的 image 必须 `tag@sha256:` 双重固定，禁
   `:latest` 与裸 tag。keycloak/OPA 的 compose.test.yaml 是范本；postgres/otel 的
   tag-only 漂移面（R9 F-R9-10/11）在本文件不允许复发。
2. **health/readiness**：每个长驻 service 必须有 healthcheck。`up -d --wait` 是
   S11 Gate 命令，缺 healthcheck 等于没有 Gate。
3. **非 root + 只读 rootfs + no-new-privileges + 资源限额**：全部 ZhiWei 应用容器
   （api/worker/dispatcher/capability-runner/reference/migrate）必须 `user`（数字 uid）、
   `read_only`、`security_opt no-new-privileges`、`deploy.resources.limits`。有真实
   先例的加固容器 OPA 已达标；本契约把同等要求扩到应用面。
4. **管理端口仅 loopback/internal**：发布到宿主机的端口必须绑 127.0.0.1；未发布端口
   走 internal 网络。缺省 network 语义（全 bridge + 随意发布）不允许。
5. **fixture 保证**：应用容器 `ZHIWEI_RELEASE_MODE=fixture_only`、
   `ZHIWEI_PROFILE=local_product`；compose 内不出现 `OPENAI_API_KEY` 等 live 凭据——
   Docker startup 不做 live（spec §6）。
6. **secrets 卫生**：master key 走 Docker secrets + secrets.example 占位；仓库不提交
   真实凭据；Keycloak 绝不挂载 master key（验收阻断 4 沿袭）。
7. **组件完备**：spec §2 组件清单逐项在 services 里登记（Temporal dev/OpenSearch/
   Garage/Redis 首集成批 2 交付；断言按批分组，批 1 只冻结结构面）。
8. `docker compose ... config --quiet` 只证明 YAML 可解析，不是启动 Gate
   （compose.test.yaml:18-24 既有批判沿袭）——启动 Gate 在 tests/e2e/local_product。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
COMPOSE_FILE = COMPOSE_DIR / "compose.yaml"

# spec §2 组件 → compose service 名。批 2（首集成 5 件）与批 1（有先例 4 件 + 应用面）
# 的分组来自 plan 2026-09-08 修订（F-R8-01）；批 1 只断言批 1 名单，批 2 名单在
# 批 2 提交后由 test_batch2_components 冻结。
#
# **登记例外（ADR-012，S11 独立验收 F-P0 补登）**：spec §2 名单中的
# Integration/Index/Eval workers 无仓内实现可承载（deployment 文件只承载已实现
# 能力，specs/s11 §1）——不制造假 worker 容器。解锁条件：对应域功能（webhook
# HTTP 端点批/检索索引批/eval worker 化）落地后补 compose 服务；届时把组件名
# 加入 BATCH1_SERVICES 并由本契约冻结。
BATCH1_SERVICES = (
    "postgres",
    "keycloak",
    "opa",
    "otel-collector",
    "migrate",
    "api",
    "web",
    "proxy",
    "agent-worker",
    "outbox-dispatcher",
    "capability-runner",
)
BATCH2_SERVICES = (
    "temporal",
    "opensearch",
    "garage",
    "redis",
    "reference-mcp",
    "reference-openapi",
    "reference-source",
)
APP_SERVICES = (
    "migrate",
    "api",
    "agent-worker",
    "outbox-dispatcher",
    "capability-runner",
    "reference-mcp",
    "reference-openapi",
    "reference-source",
)
LIVE_CREDENTIAL_ENVS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")


# 长 驻 service 必须有 healthcheck（--wait Gate 的就绪语义）。两类例外在契约内
# 显式冻结：
# - migrate：一次性任务，完成即成功（compose --wait 按 completed_successfully 判定），
#   健康语义不适用；
# - garage / otel-collector：vendor 镜像是 binary-only（无 shell/curl），容器内探测
#   物理不可行——按 ADR-012 登记例外，补偿控制：loopback 发布端口 + doctor --strict
#   宿主侧 TCP 探活 + 启动 e2e 显式断言。未登记的缺口一律按未通过处理。
HEALTHCHECK_EXEMPT = ("migrate", "garage", "otel-collector")


def _landed(names: tuple[str, ...]) -> tuple[str, ...]:
    """按批交付进度过滤断言名单（批 2 守卫见 _batch2_landed）。"""
    return tuple(name for name in names if name in _services())


def _load_compose_model(path: Path) -> dict:
    """合并 compose include 图（本地实现，快速契约测试不依赖 docker）。

    include 的 fragment 内相对路径（volumes/build context）以 fragment 所在目录为基准——
    与 docker compose v2 的 include 语义一致；本测试只做 service 级断言，不解析路径。
    """
    model = yaml.safe_load(path.read_text(encoding="utf-8"))
    merged: dict = dict(model)
    merged_services: dict = dict(model.get("services") or {})
    merged_volumes: dict = dict(model.get("volumes") or {})
    merged_secrets: dict = dict(model.get("secrets") or {})
    merged_networks: dict = dict(model.get("networks") or {})
    for include in model.get("include") or []:
        fragment_path = (path.parent / include).resolve()
        fragment = _load_compose_model(fragment_path)
        merged_services.update(fragment.get("services") or {})
        merged_volumes.update(fragment.get("volumes") or {})
        merged_secrets.update(fragment.get("secrets") or {})
        merged_networks.update(fragment.get("networks") or {})
    merged["services"] = merged_services
    merged["volumes"] = merged_volumes
    merged["secrets"] = merged_secrets
    merged["networks"] = merged_networks
    return merged


_CACHE_COMPOSE: dict | None = None


def _compose() -> dict:
    assert COMPOSE_FILE.exists(), f"缺少 S11 compose 文件: {COMPOSE_FILE}"
    global _CACHE_COMPOSE
    if _CACHE_COMPOSE is None:
        _CACHE_COMPOSE = _load_compose_model(COMPOSE_FILE)
    return _CACHE_COMPOSE


def _services() -> dict:
    compose = _compose()
    services = compose.get("services") or {}
    assert services, "compose.yaml 未定义任何 service"
    return services


def _service_image(service: dict) -> str:
    image = service.get("image")
    assert image, "service 缺少 image 字段（build-only 镜像不受 pin 契约保护）"
    return image


def test_compose_spec_structure_directories() -> None:
    """spec §2 部署目录结构：compose.yaml + profiles/ + configs/ + secrets.example/。"""
    assert (COMPOSE_DIR / "compose.yaml").is_file()
    assert (COMPOSE_DIR / "profiles").is_dir()
    assert (COMPOSE_DIR / "configs").is_dir()
    assert (COMPOSE_DIR / "secrets.example").is_dir()
    assert any((COMPOSE_DIR / "secrets.example").iterdir()), "secrets.example 不能为空目录"


def _batch2_landed() -> bool:
    """批 2（首集成 5 件，plan F-R8-01）是否已交付。

    批 1 提交时批 2 断言显式 skip（原因登记：F-R8-01 分批纪律，批 2 的
    `up -d --wait` 失败面首测在批 2 提交验收）；批 2 提交时此守卫删除，
    断言无条件生效。
    """
    return "temporal" in _services()


@pytest.mark.parametrize("name", BATCH1_SERVICES)
def test_batch1_components_exist(name: str) -> None:
    assert name in _services(), f"批 1 组件 {name} 不在 compose services 中"


@pytest.mark.parametrize("name", BATCH2_SERVICES)
def test_batch2_components_exist(name: str) -> None:
    assert name in _services(), f"批 2 组件 {name} 不在 compose services 中"


def test_all_images_tag_and_digest_pinned() -> None:
    """第三方基础/服务镜像 tag+digest 双 pin。

    仓内构建的应用镜像（build: 声明）除外——它们的 pin 由 Dockerfile FROM 的
    digest 承载（tests/contract/deploy/test_app_image.py 验证），没有 registry
    digest 可引用；spec §2「所有 image digest pin」的真实意图是第三方镜像防漂移。
    """
    for name, service in _services().items():
        if "image" not in service:
            assert "build" in service, f"{name}: 既无 image 也无 build，无法部署"
            continue
        if "build" in service:
            continue
        image = _service_image(service)
        assert "@sha256:" in image, f"{name}: 镜像未做 digest pin: {image}"
        tag = image.split("@", 1)[0]
        assert ":" in tag, f"{name}: 镜像缺少 tag（裸 digest 不可读）: {image}"
        assert not tag.endswith(":latest"), f"{name}: 禁止 :latest"


def test_app_build_context_is_repo_root_with_pinned_dockerfile() -> None:
    """应用镜像从仓库根构建，Dockerfile 基础镜像 digest pin 由镜像契约测试验证。"""
    for name in ("api", "agent-worker", "outbox-dispatcher", "capability-runner", "migrate"):
        build = _services()[name].get("build") or {}
        context = build.get("context", "")
        assert build.get("dockerfile") == "Dockerfile", f"{name}: 应用镜像必须用根 Dockerfile"
        assert context.endswith("../..") or context == "..", f"{name}: build context 必须是仓库根"


@pytest.mark.parametrize("name", _landed(tuple(n for n in sorted(set(BATCH1_SERVICES + BATCH2_SERVICES)) if n not in HEALTHCHECK_EXEMPT)))
def test_every_service_has_healthcheck(name: str) -> None:
    service = _services()[name]
    healthcheck = service.get("healthcheck")
    assert healthcheck and healthcheck.get("test"), f"{name}: 缺少 healthcheck（--wait Gate 失效）"


@pytest.mark.parametrize("name", _landed(HEALTHCHECK_EXEMPT))
def test_healthcheck_exempt_services_are_declared(name: str) -> None:
    """例外面只能收窄不能扩大：_exempt 名单里的服务必须真实存在于 compose。"""
    assert name in _services()


def test_app_services_hardened() -> None:
    for name in _landed(APP_SERVICES):
        service = _services()[name]
        assert service.get("user"), f"{name}: 应用容器必须声明非 root user"
        user = str(service["user"])
        assert user.split(":")[0].isdigit(), f"{name}: user 必须是数字 uid（可审计）"
        assert service.get("read_only") is True, f"{name}: 应用容器必须只读 rootfs"
        assert "no-new-privileges:true" in (service.get("security_opt") or []), (
            f"{name}: 缺少 no-new-privileges"
        )
        limits = (service.get("deploy") or {}).get("resources", {}).get("limits")
        assert limits and limits.get("cpus") and limits.get("memory"), (
            f"{name}: 缺少 deploy.resources.limits"
        )


def test_published_ports_loopback_only() -> None:
    for name, service in _services().items():
        for entry in service.get("ports") or []:
            if isinstance(entry, str):
                # 长格式字符串 "127.0.0.1:8090:8080"——loopback 绑定必须是显式前缀
                assert entry.startswith("127.0.0.1:") or entry.startswith("127.0.0.1]:"), (
                    f"{name}: 端口 {entry} 未绑 loopback（管理端口仅 loopback/internal）"
                )
            else:
                published = str(entry.get("published", ""))
                if not published:
                    continue
                assert (entry.get("host_ip") or "") == "127.0.0.1", (
                    f"{name}: 端口 {published} 未绑 loopback（管理端口仅 loopback/internal）"
                )


def test_fixture_only_guarantee() -> None:
    """Docker startup 不做 live：应用容器 fixture 档、无 live 凭据进入 compose。"""
    for name in _landed(APP_SERVICES):
        service = _services()[name]
        env = service.get("environment") or {}
        env_map = dict(env) if not isinstance(env, dict) else env
        mode = env_map.get("ZHIWEI_RELEASE_MODE")
        profile = env_map.get("ZHIWEI_PROFILE")
        if name == "migrate":
            # 一次性迁移任务不承载 API/模型面，但同样不得带 live 凭据
            pass
        else:
            assert mode == "fixture_only", f"{name}: ZHIWEI_RELEASE_MODE 必须是 fixture_only"
            assert profile == "local_product", f"{name}: ZHIWEI_PROFILE 必须是 local_product"
        for credential in LIVE_CREDENTIAL_ENVS:
            assert credential not in env_map, f"{name}: compose 不允许出现 live 凭据 {credential}"


def test_master_key_via_docker_secret_not_keycloak() -> None:
    """master key 走 Docker secrets；Keycloak 绝不挂载（S1 验收阻断 4 沿袭）。"""
    compose = _compose()
    secrets_decl = compose.get("secrets") or {}
    assert "zhiwei_identity_master_key" in secrets_decl
    api_service = _services()["api"]
    secrets_mounted = list(api_service.get("secrets") or [])
    names = [
        s if isinstance(s, str) else s.get("target") or s.get("source") for s in secrets_mounted
    ]
    assert "zhiwei_identity_master_key" in names, "api 必须挂载 master key secret"
    keycloak = _services()["keycloak"]
    kc_mounts = keycloak.get("secrets") or []
    kc_names = [
        s if isinstance(s, str) else s.get("target") or s.get("source") for s in kc_mounts
    ]
    assert "zhiwei_identity_master_key" not in kc_names


def test_secrets_example_is_placeholder_material() -> None:
    """secrets.example 的 master key 是 keyring 文件格式（key_id=base64），
    解码后 32 字节——是格式合法的开发占位，不是真实凭据（load_keyring 契约）。"""
    key_file = COMPOSE_DIR / "secrets.example" / "zhiwei_identity_master_key"
    assert key_file.is_file()
    for line in key_file.read_text(encoding="utf-8").strip().splitlines():
        key_id, _, encoded = line.partition("=")
        assert key_id, "keyring 行缺少 key_id"
        assert encoded, "keyring 行缺少 key material"
        decoded = base64.b64decode(encoded, validate=True)
        assert len(decoded) == 32, "master key 占位必须是 32 字节 base64（keyring 载荷要求）"


def test_reverse_proxy_and_web_topology() -> None:
    """proxy 是唯一发布面；web 静态与 api 经 proxy 对外；应用端口不直接发布。"""
    services = _services()
    assert services["proxy"].get("ports"), "proxy 必须发布入口端口"
    for name in ("api", "web", "agent-worker", "outbox-dispatcher", "capability-runner"):
        assert not (services[name].get("ports") or []), (
            f"{name}: 应用端口不得直接发布（统一经 proxy）"
        )


def test_temporal_persistence_separate_database() -> None:
    """spec §3：Temporal persistence 与业务 PG 使用不同 database（同实例不同 DB 足够
    local-product；production reference 由 K8s overlay 指向外部托管 PG）。

    修订（E-R1 方案 b，2026-09-08）：temporal 服务不再是 env 驱动的 auto-setup
    （DBNAME env 随之消失），持久化 database 的事实源改为静态 config 模板
    （deploy/compose/configs/temporal/development.yaml.template）。原契约的不变量
    不变，探测面从 env 扩为「env 存在则校验 + 静态 config 双 store 必须独立于业务
    database」——比原断言更强（补上了 visibility store）。"""
    services = _services()
    temporal = services["temporal"]
    env_map = dict(temporal.get("environment") or {})
    db_env = str(env_map.get("DBNAME") or env_map.get("DB_NAME") or "")
    assert db_env not in {"zhiwei", "zhiwei_app"}, (
        "temporal 不得复用业务 database"
    )
    business_databases = {"zhiwei", "zhiwei_identity", "zhiwei_test"}
    config_text = (COMPOSE_DIR / "configs" / "temporal" / "development.yaml.template").read_text(
        encoding="utf-8"
    )
    if db_env:
        assert db_env == "zhiwei_local_temporal", "temporal persistence database 与 init 脚本对齐"
    else:
        database_names = re.findall(r'databaseName:\s*"([^"]+)"', config_text)
        assert set(database_names) == {"zhiwei_local_temporal", "temporal_visibility"}, (
            "temporal 静态 config 的 persistence store 必须与既有部署对齐"
        )
        assert not (set(database_names) & business_databases), (
            "temporal 不得复用业务 database"
        )
    postgres = services["postgres"]
    init_text = ""
    for volume in postgres.get("volumes") or []:
        source = volume if isinstance(volume, str) else volume.get("source", "")
        suffix = Path(source.split(":")[0]).suffix
        if suffix in {".sql", ".sh"}:
            # include fragment 内相对路径以 fragment 目录为基准（profiles/）
            relative = Path(source.split(":")[0])
            for base in (COMPOSE_DIR, COMPOSE_DIR / "profiles"):
                candidate = (base / relative).resolve()
                if candidate.is_file():
                    init_text += candidate.read_text(encoding="utf-8")
                    break
    assert "zhiwei_local_temporal" in init_text, "init 脚本必须创建 temporal 独立 database"
    assert "BYPASSRLS" in init_text, "dispatcher bypass 角色必须在 init 脚本显式声明"


def test_opa_bundle_wired_from_policies() -> None:
    """OPA 侧车沿用 S1-T3 字符契约：bundle 从仓内 policies/ 构建且 fail closed。"""
    opa = _services()["opa"]
    volumes = opa.get("volumes") or []
    sources = [v if isinstance(v, str) else v.get("source", "") for v in volumes]
    assert any("policies" in s for s in sources), "OPA 必须挂载仓内 policies/ 构建 bundle"
    assert opa.get("read_only") is True and "no-new-privileges:true" in (
        opa.get("security_opt") or []
    )
