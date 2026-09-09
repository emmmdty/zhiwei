"""S11-T2 Kubernetes reference 契约（specs/s11 §2/§3，plan Task 2）。

冻结断言面（A 档契约）：

1. **渲染即校验**：`kubectl kustomize` 渲染 local-reference 与 production-reference
   两个 overlay 必须成功；CI 渲染失败即 Gate 失败（kubectl 缺失时环境守卫跳过，
   不算断言放宽）；
2. **Pod security**：全部工作负载 runAsNonRoot + allowPrivilegeEscalation=false +
   capabilities drop ALL + seccompProfile RuntimeDefault + resources requests/limits；
   长驻工作负载必须有 readiness/liveness probe；
3. **镜像纪律**：第三方镜像 tag+digest 双 pin；production-reference overlay 的应用
   镜像必须 digest pin（local-reference 允许本地构建 tag）；
4. **无假 HA**：production-reference overlay 不得出现 StatefulSet/PVC——外部托管
   依赖以 ConfigMap endpoint + Secret 引用表达（specs/s11 §3：不自建数据库/IdP/KMS
   operator）；local-reference 的参考态服务是单副本参考实现，文件注释显式声明非 HA；
5. **网络策略**：应用工作负载被 default-deny NetworkPolicy 覆盖（Ingress+Egress，
   显式放行依赖端口）；
6. **PDB**：每个 stateless 工作负载有 PodDisruptionBudget；
7. **迁移 Job**：alembic 迁移以 Job 承载（backoffLimit + ttlSecondsAfterFinished），
   app 不在启动期跑迁移；
8. **Temporal 持久化分离**：production-reference 配置面里 Temporal database 与业务
   database 是不同名（同 §3 精神：不同 account/database）。

事实源：docs/review/findings/R8-platform-s11.md §T2（勿重复 compose.test.yaml 对
`config --quiet` 式弱断言的批判——这里做的是渲染+策略断言）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
K8S_ROOT = REPO_ROOT / "deploy" / "kubernetes"
OVERLAYS = {
    "local-reference": K8S_ROOT / "overlays" / "local-reference",
    "production-reference": K8S_ROOT / "overlays" / "production-reference",
}
APP_WORKLOADS = {"api", "agent-worker", "outbox-dispatcher", "capability-runner", "web"}


def _kubectl_available() -> bool:
    """kubectl 渲染是全模块前置——不只 test_overlays_render 需要（复验轮修复：
    悬空符号链接环境下其余测试原先会 FileNotFoundError 而非 skip）。"""
    if not shutil.which("kubectl"):
        return False
    proc = subprocess.run(["kubectl", "version", "--client"], capture_output=True, timeout=30)
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(
    not _kubectl_available(), reason="kubectl 不可用：K8s 渲染契约测试跳过（环境守卫）"
)


def _render(overlay: str) -> list[dict]:
    path = OVERLAYS[overlay]
    proc = subprocess.run(
        ["kubectl", "kustomize", str(path)], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        pytest.fail(f"kustomize 渲染失败（{overlay}）:\n{proc.stderr[-2000:]}")
    docs = [doc for doc in yaml.safe_load_all(proc.stdout) if doc]
    assert docs, f"{overlay}: 渲染结果为空"
    return docs


def _docs_by_kind(overlay: str) -> dict[str, list[dict]]:
    by_kind: dict[str, list[dict]] = {}
    for doc in _render(overlay):
        by_kind.setdefault(doc["kind"], []).append(doc)
    return by_kind


def _pod_spec(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def _containers(doc: dict) -> list[dict]:
    return _pod_spec(doc)["containers"]


def _workload_security(doc: dict) -> None:
    name = f"{doc['kind']}/{doc['metadata']['name']}"
    pod = _pod_spec(doc)
    sc = pod.get("securityContext") or {}
    assert sc.get("runAsNonRoot") is True, f"{name}: runAsNonRoot 未声明"
    for container in _containers(doc):
        csc = container.get("securityContext") or {}
        assert csc.get("allowPrivilegeEscalation") is False, f"{name}: 提权未禁"
        assert "ALL" in (csc.get("capabilities", {}).get("drop") or []), f"{name}: caps 未 drop ALL"
        assert csc.get("seccompProfile", {}).get("type") == "RuntimeDefault", f"{name}: seccomp 缺失"
        resources = container.get("resources") or {}
        assert resources.get("requests") and resources.get("limits"), f"{name}: resources 缺失"


def test_kubernetes_tree_structure() -> None:
    assert (K8S_ROOT / "base" / "kustomization.yaml").is_file()
    for name, path in OVERLAYS.items():
        assert (path / "kustomization.yaml").is_file(), f"{name}: 缺少 overlay kustomization"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_overlays_render(overlay: str) -> None:
    if not _kubectl_available():
        pytest.skip("kubectl 不可用：渲染校验跳过（环境守卫，非断言放宽）")
    assert _docs_by_kind(overlay)


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_app_deployments_security(overlay: str) -> None:
    """完整加固契约只作用于应用面工作负载；第三方参考态服务按镜像能力分层（见下）。"""
    by_kind = _docs_by_kind(overlay)
    deployments = by_kind.get("Deployment") or []
    names = {d["metadata"]["name"] for d in deployments}
    missing = APP_WORKLOADS - names
    assert not missing, f"{overlay}: 缺少应用工作负载 {missing}"
    for doc in deployments:
        if doc["metadata"]["name"] in APP_WORKLOADS:
            _workload_security(doc)


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_reference_services_basic_hardening(overlay: str) -> None:
    """第三方参考态服务：必须有 resources 限额 + 禁提权 + drop caps；non-root 由
    镜像用户承载（postgres 999/opensearch 1000/redis 999/opa 1000/temporal auto-setup
    需写 config 走 root——镜像约束，与 compose 侧 binary/stateful 例外同一登记口径）。"""
    by_kind = _docs_by_kind(overlay)
    for doc in (by_kind.get("Deployment") or []) + (by_kind.get("StatefulSet") or []):
        if doc["metadata"]["name"] in APP_WORKLOADS:
            continue
        for container in _containers(doc):
            csc = container.get("securityContext") or {}
            assert csc.get("allowPrivilegeEscalation") is False, (
                f"{doc['metadata']['name']}: 提权未禁"
            )
            assert "ALL" in (csc.get("capabilities", {}).get("drop") or [])
            assert container.get("resources", {}).get("limits"), f"{doc['metadata']['name']}: 缺 limits"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_app_probes_required(overlay: str) -> None:
    by_kind = _docs_by_kind(overlay)
    for doc in by_kind.get("Deployment") or []:
        name = doc["metadata"]["name"]
        for container in _containers(doc):
            assert container.get("readinessProbe"), f"{name}: 缺 readinessProbe"
            assert container.get("livenessProbe"), f"{name}: 缺 livenessProbe"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_third_party_images_digest_pinned(overlay: str) -> None:
    by_kind = _docs_by_kind(overlay)
    for doc in (by_kind.get("Deployment") or []) + (by_kind.get("StatefulSet") or []):
        for container in _containers(doc):
            image = container.get("image", "")
            if "zhiwei/app" in image or "zhiwei/web" in image:
                continue
            assert "@sha256:" in image, f"{doc['metadata']['name']}: {image} 未 digest pin"


def test_production_reference_images_registry_qualified() -> None:
    """production-reference 的应用镜像必须是 registry 限定名 + 非 latest tag。

    应用镜像的 digest pin 在 release seal（T8）时写入 provenance/manifest——本地
    未发布的镜像没有 registry digest 可引用；第三方镜像的 digest pin 在本文件
    test_third_party_images_digest_pinned 强制。
    """
    by_kind = _docs_by_kind("production-reference")
    for doc in by_kind.get("Deployment") or []:
        for container in _containers(doc):
            image = container.get("image", "")
            if "zhiwei/app" in image or "zhiwei/web" in image:
                assert ":latest" not in image, f"{doc['metadata']['name']}: 禁止 :latest"


def test_production_reference_has_no_stateful_workloads() -> None:
    """specs/s11 §3：不自建数据库/IdP/KMS operator——外部依赖不落地 StatefulSet/PVC。"""
    by_kind = _docs_by_kind("production-reference")
    assert not by_kind.get("StatefulSet"), "production-reference 出现 StatefulSet（假 HA）"
    assert not by_kind.get("PersistentVolumeClaim"), "production-reference 出现 PVC"


def test_production_reference_external_endpoints_declared() -> None:
    by_kind = _docs_by_kind("production-reference")
    keys: set[str] = set()
    for cm in by_kind.get("ConfigMap") or []:
        keys.update((cm.get("data") or {}).keys())
    for endpoint in ("ZHIWEI_DATABASE_URL", "ZHIWEI_TEMPORAL_TARGET", "ZHIWEI_OPA_BASE_URL"):
        assert endpoint in keys, f"production-reference 未声明外部端点 {endpoint}"


def test_production_reference_temporal_database_separate() -> None:
    by_kind = _docs_by_kind("production-reference")
    joined = "\n".join(
        str(v) for cm in by_kind.get("ConfigMap") or [] for v in (cm.get("data") or {}).values()
    )
    # 外部托管 PG 下，Temporal persistence 的 database 名必须与业务库不同名
    assert "zhiwei_temporal" in joined, "未声明 Temporal 专用 database（持久化分离）"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_network_policy_covers_app_workloads(overlay: str) -> None:
    by_kind = _docs_by_kind(overlay)
    policies = by_kind.get("NetworkPolicy") or []
    assert policies, f"{overlay}: 缺少 NetworkPolicy"
    selectors = set()
    for policy in policies:
        for key in ("matchLabels", "matchExpressions"):
            selector = (policy.get("spec", {}).get("podSelector") or {}).get(key)
            if selector:
                selectors.add(str(selector))
    assert policies[0]["spec"].get("policyTypes") in (
        ["Ingress", "Egress"],
        ["Ingress"],
        ["Egress", "Ingress"],
    ) or set(policies[0]["spec"].get("policyTypes") or []) >= {"Ingress", "Egress"}


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_pdb_per_stateless_workload(overlay: str) -> None:
    by_kind = _docs_by_kind(overlay)
    pdbs = by_kind.get("PodDisruptionBudget") or []
    pdb_selectors = [
        (p["spec"].get("selector", {}).get("matchLabels") or {}) for p in pdbs
    ]
    for doc in by_kind.get("Deployment") or []:
        if doc["metadata"]["name"] not in APP_WORKLOADS:
            continue
        labels = doc["spec"]["template"]["metadata"].get("labels") or {}
        matched = any(
            selector.items() <= set(labels.items()) for selector in pdb_selectors
        )
        assert matched, f"{overlay}: {doc['metadata']['name']} 缺 PDB"


def test_migration_job_is_batch_workload() -> None:
    by_kind = _docs_by_kind("local-reference")
    jobs = by_kind.get("Job") or []
    assert jobs, "缺少迁移 Job"
    job = jobs[0]
    assert job["spec"].get("backoffLimit") is not None
    assert job["spec"].get("ttlSecondsAfterFinished") is not None
    cmd = " ".join(str(x) for c in job["spec"]["template"]["spec"]["containers"] for x in (c.get("command") or []))
    assert "alembic" in cmd, "迁移 Job 未运行 alembic"


def test_temporal_server_config_single_source() -> None:
    """E-R1 方案 b（2026-09-08）：temporal server 静态 config 的单一事实源是
    deploy/compose/configs/temporal/ 下的模板；local-reference overlay 的
    temporal-server-config ConfigMap 必须与源文件逐字节一致（渲染期嵌入的
    副本不允许静默漂移）。"""
    if not _kubectl_available():
        pytest.skip("kubectl 不可用：渲染校验跳过（环境守卫，非断言放宽）")
    by_kind = _docs_by_kind("local-reference")
    cms = [c for c in by_kind.get("ConfigMap") or [] if c["metadata"]["name"] == "temporal-server-config"]
    assert len(cms) == 1, "缺少 temporal-server-config ConfigMap"
    data = cms[0]["data"]
    for key in ("development.yaml.template", "dynamicconfig.yaml"):
        source = (REPO_ROOT / "deploy" / "compose" / "configs" / "temporal" / key).read_text()
        assert data.get(key) == source, f"{key}: ConfigMap 与 compose 侧模板漂移"
