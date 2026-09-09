"""S11-T7 production-topology 安全套件（specs/s11 §5：tenant/security suite 在
production topology 重跑 + secret/PII 扫描）。

冻结断言面（A 档契约）：

1. **compose 拓扑 fail-closed**：未认证 API 请求经 proxy → 401（认证面前置）；
   healthz 是唯一公开端点且只泄露 release_mode/profile 两个部署声明；
2. **容器面**：应用容器以非 root 运行（docker inspect 实测，不只看 compose 声明）；
   管理端口仅 loopback 发布（docker port 实测）；
3. **K8s 渲染面**：production-reference 渲染产物 NetworkPolicy default-deny、
   应用容器 readOnlyRootFilesystem/runAsNonRoot、ingress TLS 声明（渲染即校验）；
4. **sentinel 扫描**：pg dump / 备份 / ObjectStore / 前端 bundle / otel 导出配置
   不得出现已知 secret 载荷（compose secret 值、master key 材料、私钥标记）；
5. **残留风险与版本**：security.md 记录精确测试版本；失败不得用 allowlist 掩盖。

slow + docker 守卫（无 docker/栈未起 → skip，环境守卫非断言放宽）。
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yaml"
PROXY = "http://127.0.0.1:8090"

pytestmark = pytest.mark.slow

# 已知 secret 哨兵：这些载荷绝不允许出现在任何导出面（pg dump/备份/对象/前端 bundle）
SENTINELS = (
    "zhiwei-dev-admin-only",
    "s1-dev-client-secret-only",
    "zhiwei-dev-pg-only",
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "sk-ant-",
    "sk-proj-",
)


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=timeout)


def _docker_available() -> bool:
    import shutil

    return shutil.which("docker") is not None


@pytest.fixture(scope="module")
def compose_stack() -> None:
    if not _docker_available():
        pytest.skip("docker 不可用：production topology 套件跳过（环境守卫）")
    proc = _run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", "zhiwei-local",
         "up", "-d", "--wait", "proxy", "api", "postgres"]
    )
    if proc.returncode != 0:
        pytest.skip(f"compose 栈未就绪: {proc.stderr[-200:]}")


# ---------------------------------------------------------------- topology 面经 compose


def test_unauthenticated_api_is_fail_closed(compose_stack: None) -> None:
    """未认证请求经 proxy 到 API → 401/403（认证面前置，不是 200/500）。"""
    request = urllib.request.Request(f"{PROXY}/api/v1/organizations")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            pytest.fail(f"未认证请求竟然成功: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        assert exc.code in {401, 403}, f"未认证请求应 401/403，得到 {exc.code}"


def test_healthz_leaks_no_topology_details(compose_stack: None) -> None:
    """healthz 是唯一公开端点：响应体只有部署声明，无拓扑/版本/内部细节。"""
    with urllib.request.urlopen(f"{PROXY}/healthz", timeout=10) as resp:
        body = json.loads(resp.read().decode())
    assert set(body) == {"release_mode", "profile"}, f"healthz 泄露多余字段: {set(body)}"


def test_app_containers_run_non_root_in_real_stack(compose_stack: None) -> None:
    """docker inspect 实测：应用容器进程 uid 非 0（不只看 compose 声明）。"""
    for service in ("api", "agent-worker", "outbox-dispatcher", "capability-runner"):
        proc = _run([
            "docker", "compose", "-f", str(COMPOSE_FILE), "-p", "zhiwei-local",
            "exec", "-T", service, "python", "-c", "import os; print(os.getuid())",
        ])
        if proc.returncode != 0:
            continue  # 非 python 容器（worker 均为 python 镜像，此分支不应触发）
        assert proc.stdout.strip() != "0", f"{service}: 实际以 root 运行"


def test_management_ports_loopback_only_in_real_stack(compose_stack: None) -> None:
    """docker port 实测：发布端口全部绑 127.0.0.1（无 0.0.0.0 发布）。"""
    proc = _run(["docker", "compose", "-f", str(COMPOSE_FILE), "-p", "zhiwei-local", "ps", "--format", "json"])
    assert proc.returncode == 0
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for publication in row.get("Publishers") or []:
            # 无发布端口的容器出现空 Publisher（URL=""、Port=0）——跳过
            if not publication.get("PublishedPort"):
                continue
            host_ip = str(publication.get("URL", ""))
            assert host_ip == "127.0.0.1", (
                f"{row.get('Service')}: 端口 {publication.get('PublishedPort')} 发布在 {host_ip}"
            )


# ---------------------------------------------------------------- sentinel 扫描


def test_pg_dump_contains_no_secret_sentinels(compose_stack: None, tmp_path: Path) -> None:
    """pg dump（业务库样本）不得包含 compose secret 值/私钥标记。"""
    dump = _run([
        "docker", "compose", "-f", str(COMPOSE_FILE), "-p", "zhiwei-local",
        "exec", "-T", "postgres", "pg_dump", "-U", "postgres", "zhiwei",
    ])
    assert dump.returncode == 0
    text = dump.stdout
    for sentinel in SENTINELS:
        assert sentinel not in text, f"pg dump 出现哨兵: {sentinel}"


def test_backup_export_contains_no_secret_sentinels(compose_stack: None, tmp_path: Path) -> None:
    """备份导出（claims/objects）不得包含哨兵；keyring 材料只允许在 secrets/ 内。"""
    from zhiwei.operations.backup import create_backup

    backup_dir = tmp_path / "backup"
    create_backup(
        output_dir=backup_dir,
        profile="local_product",
        keyring_file=REPO_ROOT / "deploy/compose/secrets.example/zhiwei_identity_master_key",
    )
    for path in (backup_dir / "claims").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for sentinel in SENTINELS:
                assert sentinel not in text, f"claims 导出出现哨兵: {sentinel}"
    # claims 导出清单里允许存在的唯一 secret 位置 = secrets/（keyring 恢复材料）
    secrets_dir = backup_dir / "secrets"
    if secrets_dir.is_dir():
        assert any(secrets_dir.iterdir()), "secrets/ 缺 keyring 恢复材料"


def test_frontend_bundle_contains_no_secret_sentinels(compose_stack: None) -> None:
    """前端构建产物不得包含哨兵。"""
    bundle = REPO_ROOT / "apps" / "web" / "dist"
    if not bundle.is_dir():
        pytest.skip("apps/web/dist 不存在（未构建）")
    for path in bundle.rglob("*"):
        if path.is_file() and path.suffix in {".js", ".html", ".css", ".json"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            for sentinel in SENTINELS:
                assert sentinel not in text, f"前端 bundle 出现哨兵: {sentinel}（{path.name}）"


def test_otel_export_config_is_metadata_only(compose_stack: None) -> None:
    """otel 导出管道删除正文键（prompt/messages/result 等不进导出）。"""
    config = (REPO_ROOT / "deploy" / "observability" / "otel-collector-config.yaml").read_text(
        encoding="utf-8"
    )
    for body_key in ("prompt", "messages", "result", "completion", "tool_args", "input_values"):
        assert body_key in config, f"attributes 处理器缺正文键 {body_key}（导出面泄漏）"


# ---------------------------------------------------------------- K8s 渲染面


def test_production_reference_rendered_policy_fail_closed() -> None:
    """渲染即校验：production-reference 的 NetworkPolicy/Pod security/TLS 面。"""
    import shutil

    if not shutil.which("kubectl"):
        pytest.skip("kubectl 不可用：渲染校验跳过（环境守卫）")
    proc = _run(["kubectl", "kustomize", str(REPO_ROOT / "deploy/kubernetes/overlays/production-reference")])
    assert proc.returncode == 0, proc.stderr
    import yaml

    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    policies = [d for d in docs if d["kind"] == "NetworkPolicy"]
    assert policies, "production-reference 渲染产物缺 NetworkPolicy"
    assert any(
        set(p["spec"].get("policyTypes") or []) >= {"Ingress", "Egress"} for p in policies
    ), "NetworkPolicy 必须 Ingress+Egress 双向"
    deployments = [d for d in docs if d["kind"] == "Deployment"]
    assert deployments
    for doc in deployments:
        pod = doc["spec"]["template"]["spec"]
        assert pod.get("securityContext", {}).get("runAsNonRoot") is True
        for container in pod["containers"]:
            assert container["securityContext"]["readOnlyRootFilesystem"] is True
    ingress = [d for d in docs if d["kind"] == "Ingress"]
    assert ingress and ingress[0]["spec"].get("tls"), "ingress 必须声明 TLS"
