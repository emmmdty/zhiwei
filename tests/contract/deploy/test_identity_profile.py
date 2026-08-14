"""S1-T2 部署契约：identity profile 必须可实际启动（Keycloak）。

设计/验收方冻结（A 档，验收阻断 3）：
- compose.test.yaml identity profile 的 keycloak 镜像必须 tag + digest 双重 pin
  （禁 :latest），且 digest 必须通过 registry manifest 校验（可拉取）；
- entrypoint.sh 必须在写 realm 前创建 /opt/keycloak/data/import（镜像内该目录
  不存在），且不得依赖镜像内没有的 envsubst（exit 127 的反例已由验收复核确认）；
- compose 必须提供计划要求的 Docker-secret master-key 挂载
  （ZHIWEI_IDENTITY_MASTER_KEY_FILE 对应 /run/secrets/...）；
- `docker compose ... config --quiet` 只证明 YAML 可解析，不是启动 Gate；
  启动 Gate 必须真正拉起 profile 并通过 healthcheck（slow，需 docker + 镜像拉取，
  环境守卫：无 docker 时跳过并给出明确理由，不视为断言放宽）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.test.yaml"
ENTRYPOINT = REPO_ROOT / "deploy" / "compose" / "keycloak" / "entrypoint.sh"
MASTER_KEY_SECRET = "zhiwei_identity_master_key"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_identity_profile_keycloak_image_pinned_with_tag_and_digest() -> None:
    """镜像必须 tag + digest 固定；禁 :latest / 裸 tag（防漂移）。"""
    compose = _compose()
    image = compose["services"]["keycloak"]["image"]
    assert "@sha256:" in image, "keycloak 镜像必须用 digest 固定"
    tag, _, digest_with_algo = image.partition("@")
    assert tag.startswith("quay.io/keycloak/keycloak:")
    digest = digest_with_algo.removeprefix("sha256:")
    assert len(digest) == 64, "digest 必须是 64 位十六进制"


def test_identity_profile_declares_master_key_secret_and_mount() -> None:
    """冻结（验收阻断 3）：Docker-secret master-key 必须声明并挂载到服务。"""
    compose = _compose()
    secrets = compose.get("secrets") or {}
    assert MASTER_KEY_SECRET in secrets, "缺少 Docker-secret master-key 声明"
    mounts = [
        mount
        for service in compose["services"].values()
        for mount in (service.get("secrets") or [])
    ]
    assert any(
        m.get("source") == MASTER_KEY_SECRET
        and m.get("target") == f"/run/secrets/{MASTER_KEY_SECRET}"
        for m in mounts
    ), "master-key secret 必须挂载到服务（target=/run/secrets/zhiwei_identity_master_key）"


def test_identity_profile_keycloak_has_healthcheck() -> None:
    compose = _compose()
    keycloak = compose["services"]["keycloak"]
    assert "healthcheck" in keycloak, "keycloak 必须定义 healthcheck（启动 Gate 依赖它）"


def test_entrypoint_creates_import_dir_before_writing_realm() -> None:
    """冻结（验收阻断 3）：写 realm 前必须创建 /opt/keycloak/data/import。"""
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert "mkdir" in script and "/opt/keycloak/data/import" in script, (
        "entrypoint 必须创建 /opt/keycloak/data/import（镜像内该目录不存在）"
    )


def test_entrypoint_does_not_depend_on_envsubst() -> None:
    """冻结（验收阻断 3）：镜像内无 envsubst（gettext），entrypoint 不得依赖它。

    模板替换必须用 POSIX sh 内建（${VAR:-default} 等）或镜像内存在的工具实现。
    """
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert "envsubst" not in script, (
        "entrypoint 依赖 envsubst 会在固定镜像内 exit 127（验收复核已确认）"
    )


@pytest.mark.slow
def test_identity_profile_starts_under_docker() -> None:
    """启动 Gate（slow）：真正拉起 identity profile 并通过 healthcheck。

    `docker compose ... config --quiet` 只能证明 YAML 可解析；本测试以真实启动
    为判据：镜像 digest 可拉取、entrypoint 可执行、realm 导入成功、健康检查通过。
    """
    if shutil.which("docker") is None:
        pytest.skip("环境守卫：无 docker 无法执行启动 Gate（slow 显式运行）")
    compose_cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    try:
        # 只拉起 keycloak，避免触碰同一 compose 文件里共享的测试 postgres 服务
        up = subprocess.run(
            [*compose_cmd, "--profile", "identity", "up", "-d", "--wait", "keycloak"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert up.returncode == 0, f"identity profile 启动失败:\n{up.stdout}\n{up.stderr}"
        ps = subprocess.run(
            [*compose_cmd, "--profile", "identity", "ps", "--format", "json", "keycloak"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "healthy" in ps.stdout, f"keycloak 未达 healthy:\n{ps.stdout}\n{ps.stderr}"
    finally:
        # 只移除 keycloak 容器，不动 postgres（测试数据库复用同一 compose）
        subprocess.run(
            [*compose_cmd, "--profile", "identity", "rm", "-sf", "keycloak"],
            capture_output=True,
            text=True,
            timeout=120,
        )
