"""S11-T1 批 2 启动 Gate：`up -d --wait` 真实拉起全栈（specs/s11 §7 Gate 命令）。

`docker compose ... config --quiet` 只证明 YAML 可解析，不是启动 Gate
（compose.test.yaml 既有批判沿袭）——本文件做真实拉起 + 健康断言 + fixture 面探活：

1. `zhiwei dev up`（生产包装器路径，等价 `docker compose -p zhiwei-local -f … up -d --wait`）
   全栈拉起；garage/otel-collector 是 binary-only 镜像（ADR-012 登记例外），--wait 按
   running 判定，其余服务必须 healthy 或 completed_successfully；
2. proxy 是唯一发布面：/healthz（api 元数据端点）与 /（web 静态）经 127.0.0.1:8090 可达；
3. reference MCP/OpenAPI/source 在 internal 网络可达（经 api 容器内探针，不做宿主发布）；
4. `dev doctor --strict` 对 compose 暴露的 loopback PG 返回真实 schema revision。

slow + docker 守卫（无 docker 跳过并给出理由，不算断言放宽）。
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yaml"
PROXY_ENTRY = "http://127.0.0.1:8090"

pytestmark = pytest.mark.slow


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def local_product() -> Generator[None]:
    if not _docker_available():
        pytest.skip("docker 不可用：启动 Gate 跳过（环境守卫，非断言放宽）")
    proc = _run(
        ["uv", "run", "zhiwei", "dev", "up"],
        cwd=REPO_ROOT,
        timeout=1500,
    )
    assert proc.returncode == 0, f"dev up 失败:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    yield


def test_full_stack_wait_reaches_terminal_health(local_product: None) -> None:
    """up --wait 成功后逐服务核对终态：healthy / completed / binary-only 例外。"""
    proc = _run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-p",
            "zhiwei-local",
            "ps",
            "--format",
            "json",
        ],
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    import json

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    states = {}
    for line in lines:
        row = json.loads(line)
        states[row["Service"]] = row.get("Status", "")
    assert states, "docker compose ps 未返回任何服务"
    # binary-only 镜像例外（ADR-012）：garage / otel-collector 只要求 Up
    for service, status in states.items():
        if service in {"garage", "otel-collector"}:
            assert status.startswith("Up"), f"{service}: {status}"
        elif service == "migrate":
            assert "Exited (0)" in status, f"migrate 未成功完成: {status}"
        else:
            assert "(healthy)" in status, f"{service}: 未达 healthy: {status}"


def test_proxy_serves_api_healthz_and_web_static(local_product: None) -> None:
    with urllib.request.urlopen(f"{PROXY_ENTRY}/healthz", timeout=5) as resp:
        assert resp.status == 200
        body = resp.read().decode()
        assert "fixture_only" in body and "local_product" in body
    with urllib.request.urlopen(PROXY_ENTRY, timeout=5) as resp:
        assert resp.status == 200
        assert b"<" in resp.read(200), "web 静态首页应返回 HTML"


def test_proxy_routes_auth_to_api_not_spa(local_product: None) -> None:
    """浏览器 OIDC 旅程经 proxy 可达（产品化窗口 2026-09-08 发现的缺陷修复锚）：
    ① GET /auth/login 必须命中 API 的 OIDC 入口（302 → Keycloak authorize），不得被
    web 静态 fallback 承接（compose 侧 redirect_uri 走 8090 回环，/auth/callback 同理）；
    ② API 侧 login 依赖 identity 库（oidc_login_attempts 等）——compose 的 migrate
    必须对业务与 identity 两个 database 都走到 head，缺一会 500。"""
    request = urllib.request.Request(
        f"{PROXY_ENTRY}/auth/login", method="GET"
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=5) as resp:
            assert resp.status in {302, 303}, f"/auth/login 应 302 到 Keycloak，得到 {resp.status}"
            location = resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        if e.code in {302, 303}:
            location = e.headers.get("Location", "")
        else:
            raise
    assert "keycloak.local" in location and "/realms/zhiwei" in location, (
        f"302 目标必须是 zhiwei realm 的 authorize URL: {location}"
    )


def test_proxy_tls_listener_for_browser_oidc(local_product: None) -> None:
    """浏览器 OIDC 旅程的最后一公里（产品化窗口 2026-09-08 发现）：session cookie 是
    __Host- 前缀 + Secure（冻结契约，见 src/zhiwei/api/auth.py _cookie_flags）——
    浏览器在纯 HTTP 源上拒绝该 cookie，登录永远无法完成。proxy 必须提供 TLS 监听
    （127.0.0.1:8443，自签名 dev 证书，CN/SAN=keycloak.local），浏览器旅程走
    https://keycloak.local:8443；HTTP :8090 保留给非浏览器探活与 e2e。"""
    request = urllib.request.Request("https://127.0.0.1:8443/healthz", method="GET")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # dev 自签名证书；真实部署由 operator 换发
    # 空 ProxyHandler：operator 终端的 http(s)_proxy（clash 类）会把 127.0.0.1 的
    # https 请求转发到代理并返回 502——本地栈访问必须直连（CI 无代理不受影响）；
    # NoRedirect：302 本身是被测契约（default opener 会跟随到 Keycloak 登录页）。
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    direct = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ctx),
    )
    with direct.open(request, timeout=5) as resp:
        assert resp.status == 200
        assert "fixture_only" in resp.read().decode()
    try:
        with direct.open(
            urllib.request.Request("https://127.0.0.1:8443/auth/login", method="GET"),
            timeout=5,
        ) as resp:
            assert resp.status == 302
            location = resp.headers.get("Location") or ""
    except urllib.error.HTTPError as e:
        assert e.code == 302, f"expect 302, got {e.code}"
        location = e.headers.get("Location") or ""
    assert "keycloak.local" in location


def test_reference_services_reachable_on_internal_network(local_product: None) -> None:
    for service, port in (
        ("reference-mcp", 9101),
        ("reference-openapi", 9102),
        ("reference-source", 9103),
    ):
        proc = _run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                "zhiwei-local",
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "zhiwei.healthcheck",
                f"http://{service}:{port}/healthz",
            ],
            timeout=60,
        )
        assert proc.returncode == 0, f"{service} 内网不可达: {proc.stderr}"


def test_doctor_strict_reports_schema_revision(local_product: None) -> None:
    import os

    env = dict(os.environ)
    env.update(
        {
            "ZHIWEI_PROFILE": "local_product",
            "ZHIWEI_RELEASE_MODE": "fixture_only",
            "ZHIWEI_DATABASE_URL": "postgresql+asyncpg://zhiwei_app:zhiwei-dev-pg-only@127.0.0.1:55433/zhiwei",
            "ZHIWEI_IDENTITY_DATABASE_URL": "postgresql+asyncpg://zhiwei_app:zhiwei-dev-pg-only@127.0.0.1:55433/zhiwei_identity",
            "ZHIWEI_OBJECT_STORE_ROOT": str(REPO_ROOT / ".zhiwei-local-objects"),
            "ZHIWEI_IDENTITY_MASTER_KEY_FILE": str(
                REPO_ROOT / "deploy" / "compose" / "secrets.example" / "zhiwei_identity_master_key"
            ),
            "ZHIWEI_OPA_BASE_URL": "http://127.0.0.1:8182",
            "ZHIWEI_OIDC_ISSUER": "http://keycloak.local:8081/realms/zhiwei",
            "ZHIWEI_OIDC_CLIENT_ID": "zhiwei-bff",
            "ZHIWEI_OIDC_CLIENT_SECRET": "s1-dev-client-secret-only",
            "ZHIWEI_OIDC_REDIRECT_URI": "http://keycloak.local:8090/auth/callback",
        }
    )
    Path(env["ZHIWEI_OBJECT_STORE_ROOT"]).mkdir(exist_ok=True)
    proc = _run(
        ["uv", "run", "zhiwei", "dev", "doctor", "--strict", "--format", "json"],
        cwd=REPO_ROOT,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, f"doctor --strict 失败: {proc.stdout} {proc.stderr}"
    import json

    payload = json.loads(proc.stdout)
    revision = payload["checks"]["schema_revision"]
    assert revision["status"] == "ok", revision
    assert "schema revision:" in revision["detail"]
    assert payload["live_model_calls_allowed"] is False
