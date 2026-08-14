"""S1-T2 部署契约：identity profile 必须可实际启动（Keycloak）。

设计/验收方冻结（A 档，验收阻断 3/4）：
- compose.test.yaml identity profile 的 keycloak 镜像必须 tag + digest 双重 pin
  （禁 :latest），且 digest 必须通过 registry manifest 校验（可拉取）；
- entrypoint.sh 必须在写 realm 前创建 /opt/keycloak/data/import（镜像内该目录
  不存在），且不得依赖镜像内没有的 envsubst（exit 127 的反例已由验收复核确认）；
- Docker-secret master key：顶层声明保留（S11 应用容器挂载计划实现），但当前
  compose 没有任何 ZhiWei 服务可挂载它——尤其 Keycloak 绝不挂载 master key，
  也不得为挂载制造 dummy service（修订：原「任意服务挂载即可」断言已删除）；
- realm 注入的字符契约：`/` 与 `&` 等 sed 危险字符必须正确转义处理；`"`、反斜杠、
  控制字符（换行/CR/TAB）、空值必须在写文件前 fail closed（容器退出，日志不泄露 secret）；
- 验收修订 5（本 RED 冻结）：控制字符检测必须覆盖整个 shell value（不得依赖逐行 sed
  正则——sed 按行处理会把换行当行分隔符漏检）；渲染先写受控临时文件，sed 成功后再
  原子移动到 realm.json，失败清理临时文件；危险值不得创建/截断最终输出文件；
- 固定镜像内的对抗测试：危险字符 secret → 容器在写文件前退出且日志不泄露；
  合法含 `/`/`&` 的 secret → realm.json 可解析、Keycloak 启动并通过 healthcheck；
- `docker compose ... config --quiet` 只证明 YAML 可解析，不是启动 Gate；
  启动 Gate 必须真正拉起 profile 并通过 healthcheck（slow，需 docker + 镜像拉取，
  环境守卫：无 docker 时跳过并给出明确理由，不视为断言放宽）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.test.yaml"
ENTRYPOINT = REPO_ROOT / "deploy" / "compose" / "keycloak" / "entrypoint.sh"
REALM_TEMPLATE = REPO_ROOT / "deploy" / "compose" / "keycloak" / "realm-template.json"
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


def test_identity_profile_declares_master_key_secret_without_service_mount() -> None:
    """修订（验收阻断 4）：Docker-secret master-key 顶层声明保留，但当前 compose 没有
    ZhiWei 应用服务——任何服务（尤其 Keycloak）都不得挂载 master key。

    原「任意服务挂载即可」断言已删除：为挂载制造 dummy service 会扩大攻击面，
    实际应用容器挂载标记为 S11 计划实现。
    """
    compose = _compose()
    secrets = compose.get("secrets") or {}
    assert MASTER_KEY_SECRET in secrets, "缺少顶层 Docker-secret master-key 声明"
    mounts = [
        (service_name, mount)
        for service_name, service in compose["services"].items()
        for mount in (service.get("secrets") or [])
    ]
    assert not mounts, (
        f"当前 compose 没有任何服务应挂载 master key（S11 才挂载应用容器）: {mounts}"
    )


def test_identity_profile_keycloak_never_mounts_master_key() -> None:
    """master key 绝不能挂载给 Keycloak（Keycloak 不需要任何 docker secret）。"""
    compose = _compose()
    keycloak = compose["services"]["keycloak"]
    service_secrets = keycloak.get("secrets") or []
    sources = [mount.get("source") for mount in service_secrets]
    assert MASTER_KEY_SECRET not in sources, "master key 绝不能挂载给 Keycloak"
    assert service_secrets == [], "keycloak 服务不应声明任何 docker secret"


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


def _render_env(out_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "ZHIWEI_KC_IMPORT_DIR": str(out_dir),
        "ZHIWEI_KC_REALM_TEMPLATE": str(REALM_TEMPLATE),
        "ZHIWEI_KC_REALM_OUTPUT": str(out_dir / "realm.json"),
        "ZHIWEI_KC_RENDER_ONLY": "1",
    }


@pytest.mark.parametrize(
    "dangerous",
    [
        "",  # 空值
        "\n",  # 单独换行
        "dev\nsecret",  # 嵌入换行
        "dev\rsecret",  # CR
        "dev\tsecret",  # TAB
        'dev"quote"secret',  # 双引号
        "dev\\backslash",  # 反斜杠
    ],
    ids=["empty", "standalone-newline", "embedded-newline", "cr", "tab", "double-quote", "backslash"],
)
def test_entrypoint_render_fails_closed_on_control_characters(dangerous: str) -> None:
    """对抗（宿主层，验收修订 5）：realm 注入的危险值必须 fail closed。

    - 非零退出；
    - stderr 出现明确字符契约消息，但绝不回显注入值；
    - 最终 realm.json 不存在——不得先创建/截断最终输出文件；
    - 渲染目录不留任何残留（无 realm.json、无临时文件）。

    换行/CR 检测不得依赖逐行 sed 正则（sed 按行处理会把换行当行分隔符漏检）；
    渲染必须先写受控临时文件，sed 成功后再原子移动到 realm.json，失败清理临时文件。
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "import"
        out_dir.mkdir()
        result = subprocess.run(
            ["sh", str(ENTRYPOINT)],
            capture_output=True,
            text=True,
            env={
                **_render_env(out_dir),
                "KEYCLOAK_TEST_CLIENT_SECRET": dangerous,
                "KEYCLOAK_TEST_USER_PASSWORD": "s1-dev-user-password-only",
            },
        )
        assert result.returncode != 0, "危险值必须在写文件前 fail closed"
        # 控制字符无法用子串断言（消息本身以换行结尾），断言注入值的可打印部分
        # 不得被回显；空值必须被拒绝并说明原因
        printable = "".join(ch for ch in dangerous if ch.isprintable())
        if printable:
            assert printable not in result.stderr, "fail closed 消息不得回显注入值"
        if not dangerous:
            assert "empty" in result.stderr, "空值必须被拒绝并说明原因"
        assert "allowed" in result.stderr or "fail closed" in result.stderr, (
            f"stderr 必须出现明确字符契约消息:\n{result.stderr}"
        )
        assert not (out_dir / "realm.json").exists(), "fail closed 不得写出 realm 文件"
        assert list(out_dir.iterdir()) == [], (
            f"fail closed 不得残留临时文件:\n{sorted(p.name for p in out_dir.iterdir())}"
        )


@pytest.mark.parametrize(
    "legal",
    [
        "dev/s1&prod+secret@x",
        "a+b%c@d/e:f_g-h.i",
        "plainsecret",
    ],
    ids=["slash-amp-plus", "url-safe-set", "plain"],
)
def test_entrypoint_render_succeeds_with_legal_special_characters(legal: str) -> None:
    """对抗（宿主层，验收修订 5）：合法但 sed 危险的字符（/ & + % @）必须正确渲染。

    渲染成功：退出码 0、realm.json 可解析、值正确注入、目录无残留临时文件。
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "import"
        out_dir.mkdir()
        result = subprocess.run(
            ["sh", str(ENTRYPOINT)],
            capture_output=True,
            text=True,
            env={
                **_render_env(out_dir),
                "KEYCLOAK_TEST_CLIENT_SECRET": legal,
                "KEYCLOAK_TEST_USER_PASSWORD": "s1-dev-user-password-only",
            },
        )
        assert result.returncode == 0, f"合法值应渲染成功:\n{result.stderr}"
        rendered = json.loads((out_dir / "realm.json").read_text(encoding="utf-8"))
        assert rendered["clients"][0]["secret"] == legal
        assert rendered["users"][0]["credentials"][0]["value"] == "s1-dev-user-password-only"
        leftover = [p.name for p in out_dir.iterdir() if p.name != "realm.json"]
        assert leftover == [], f"渲染成功后不得残留临时文件: {leftover}"


@pytest.mark.slow
def test_keycloak_realm_injection_adversarial_in_fixed_image() -> None:
    """对抗（固定镜像，验收阻断 4）：realm 注入对 JSON/sed 危险字符 fail closed。

    - 双引号 secret：容器在写 realm 文件前退出（字符契约），docker logs 不泄露 secret；
    - 含 `/` 与 `&` 的合法 secret：realm.json 可解析、Keycloak 启动且 healthcheck
      通过、docker logs 不泄露 secret。
    """
    if shutil.which("docker") is None:
        pytest.skip("环境守卫：无 docker 无法执行固定镜像对抗测试（slow 显式运行）")
    compose_cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "identity"]
    try:
        subprocess.run(
            [*compose_cmd, "rm", "-sf", "keycloak"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        # ---- fail closed：双引号 secret ----
        up = subprocess.run(
            [*compose_cmd, "up", "-d", "keycloak"],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "KEYCLOAK_TEST_CLIENT_SECRET": 'dev"quote"secret'},
        )
        assert up.returncode == 0, up.stderr
        deadline = time.monotonic() + 60
        exited = False
        while time.monotonic() < deadline:
            cid = subprocess.run(
                [*compose_cmd, "ps", "-q", "keycloak"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if cid:
                state = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Status}}", cid],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if state == "exited":
                    exited = True
                    break
            time.sleep(1)
        logs = subprocess.run(
            [*compose_cmd, "logs", "keycloak"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        assert exited, f"危险 secret 必须导致容器退出而非带病启动:\n{logs}"
        assert 'dev"quote"secret' not in logs and 'quote"secret' not in logs, (
            f"fail closed 不得在日志泄露 secret:\n{logs}"
        )
        assert "allowed" in logs or "fail closed" in logs, (
            f"缺少字符契约消息:\n{logs}"
        )
        subprocess.run(
            [*compose_cmd, "rm", "-sf", "keycloak"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        # ---- 合法但 sed 危险字符（/ 与 &）：必须正确渲染并启动 ----
        volumes = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", "name=zhiwei_s0_keycloak"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.splitlines()
        for volume in volumes:
            subprocess.run(
                ["docker", "volume", "rm", volume],
                capture_output=True,
                text=True,
                timeout=60,
            )
        safe_secret = "dev/s1&prod+secret@x"
        up = subprocess.run(
            [*compose_cmd, "up", "-d", "--wait", "keycloak"],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "KEYCLOAK_TEST_CLIENT_SECRET": safe_secret},
        )
        assert up.returncode == 0, f"含 / 与 & 的 secret 必须能启动:\n{up.stdout}\n{up.stderr}"
        cat = subprocess.run(
            [*compose_cmd, "exec", "-T", "keycloak", "cat", "/opt/keycloak/data/import/realm.json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert cat.returncode == 0, f"无法读取容器内 realm.json:\n{cat.stderr}"
        realm = json.loads(cat.stdout)
        assert realm["clients"][0]["secret"] == safe_secret, "secret 必须被正确注入"
        logs = subprocess.run(
            [*compose_cmd, "logs", "keycloak"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        assert safe_secret not in logs, f"日志不得泄露 client secret:\n{logs}"
    finally:
        subprocess.run(
            [*compose_cmd, "rm", "-sf", "keycloak"],
            capture_output=True,
            text=True,
            timeout=120,
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
