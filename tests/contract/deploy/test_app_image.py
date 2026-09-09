"""S11-T1 应用镜像契约（specs/s11 §2：所有 image 非 root、health/readiness、无源码密钥）。

冻结断言面（A 档契约）：

1. Dockerfile 构建出的运行镜像可以 `id -u` 非 0 运行——compose 的 user 声明不是唯一
   防线，镜像内文件属主与可写目录布局必须支持非 root；
2. 镜像内不携带源码密钥面：`.env`、`tests/`、`evals/`、`docs/` 不进镜像
   （.dockerignore 承载，测试用真实构建产物验证）；
3. 健康检查入口存在且可执行：镜像内 healthcheck 用 Python 标准库探测 /healthz，
   slim 基础镜像无 curl/wget；
4. web 镜像由 Node 22 构建静态产物 + nginx 只读运行，同样非 root。

slow + docker 守卫：无 docker 环境跳过并给出理由（沿袭 tests/contract/deploy 先例，
不算断言放宽）。
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_DOCKERFILE = REPO_ROOT / "Dockerfile"
WEB_DOCKERFILE = REPO_ROOT / "apps" / "web" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

pytestmark = pytest.mark.slow


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # type: ignore[arg-type]


def _require_docker() -> None:
    if not _docker_available():
        pytest.skip("docker 不可用：镜像契约测试跳过（环境守卫，非断言放宽）")


@functools.cache
def _build_app_image() -> str:
    tag = "zhiwei-local-product:test-build"
    proc = _run(
        [
            "docker",
            "build",
            "-f",
            str(APP_DOCKERFILE),
            "-t",
            tag,
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, f"应用镜像构建失败:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    return tag


def test_dockerignore_excludes_secrets_and_source_trees() -> None:
    assert DOCKERIGNORE.is_file(), "缺少 .dockerignore"
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    for entry in (".env", "tests", "evals", "docs", "deploy/compose/secrets"):
        assert entry in text, f".dockerignore 缺少 {entry}"


def test_app_dockerfile_exists_with_pinned_base() -> None:
    assert APP_DOCKERFILE.is_file()
    text = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM python:3.11-slim@sha256:" in text, "基础镜像必须 digest pin"
    assert "USER " in text, "Dockerfile 必须以非 root USER 收尾"
    assert "HEALTHCHECK" in text, "Dockerfile 必须定义 HEALTHCHECK"


def test_app_image_runs_non_root() -> None:
    _require_docker()
    tag = _build_app_image()
    proc = _run(["docker", "run", "--rm", tag, "id", "-u"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != "0", "应用镜像默认用户必须是非 root"


def test_app_image_excludes_source_secret_surface() -> None:
    _require_docker()
    tag = _build_app_image()
    for forbidden in ("/app/.env", "/app/tests", "/app/evals", "/app/docs"):
        proc = _run(["docker", "run", "--rm", tag, "test", "-e", forbidden])
        assert proc.returncode != 0, f"镜像内不应存在 {forbidden}"


def test_app_image_healthcheck_probe_exists() -> None:
    _require_docker()
    tag = _build_app_image()
    proc = _run(["docker", "run", "--rm", tag, "python", "-m", "zhiwei.healthcheck", "--help"])
    assert proc.returncode == 0, f"healthcheck 探针不可执行: {proc.stderr[-500:]}"


def test_web_dockerfile_exists_with_pinned_stages() -> None:
    assert WEB_DOCKERFILE.is_file()
    text = WEB_DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM node:22-slim@sha256:" in text, "Node 构建阶段必须 digest pin"
    assert "@sha256:" in text.split("FROM", 2)[-1], "nginx 运行阶段必须 digest pin"
    assert "USER " in text, "web 镜像必须以非 root 收尾"
