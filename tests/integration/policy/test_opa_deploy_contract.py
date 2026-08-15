"""S1-T3 RED：OPA 部署契约（compose 服务 + entrypoint，宿主层静态校验）。

冻结（A 档）：
- opa 服务必须 tag + digest 双重 pin（禁 :latest），digest 为 64 位十六进制；
- opa 属于 identity profile（与 keycloak 同一 profile，Gate 的
  `--profile identity config --quiet` 与 `up --profile identity` 会包含它）；
- entrypoint 必须在启动 server 前用固定 revision 构建 bundle（fail closed：
  bundle 构建失败则容器退出，绝不带病启动）；
- OPA_BUNDLE_REVISION 字符契约与 keycloak 同款：控制字符/危险值 fail closed
  （revision 会进 bundle manifest 与 decision log），消息不回显值；
- opa 服务绝不挂载 master key（沿用验收阻断 4 的规则）；
- 固定镜像无 shell/wget/curl：entrypoint 只能依赖镜像内有的工具（busybox sh
  + opa 自身），健康检查由集成测试轮询 /health?bundles 完成，不在 compose
  里声明依赖镜像不存在的工具。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.test.yaml"
OPA_ENTRYPOINT = REPO_ROOT / "deploy" / "compose" / "opa" / "entrypoint.sh"
MASTER_KEY_SECRET = "zhiwei_identity_master_key"

# 与 deploy/compose/opa/entrypoint.sh 的字符契约保持一致的常量（测试即契约）
_ALLOWED_REVISION_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def test_opa_service_exists_in_identity_profile() -> None:
    compose = _compose()
    services = compose["services"]
    assert "opa" in services, "compose.test.yaml 必须提供 opa 服务"
    assert "identity" in services["opa"].get("profiles", []), (
        "opa 必须属于 identity profile（Gate 命令与启动流程依赖同一 profile）"
    )


def test_opa_image_pinned_with_tag_and_digest() -> None:
    image = _compose()["services"]["opa"]["image"]
    assert "@sha256:" in image, "opa 镜像必须用 digest 固定"
    tag, _, digest_with_algo = image.partition("@")
    assert tag.startswith("openpolicyagent/opa:"), f"镜像必须是官方 openpolicyagent/opa: {tag}"
    digest = digest_with_algo.removeprefix("sha256:")
    assert len(digest) == 64, "digest 必须是 64 位十六进制"


def test_opa_never_mounts_master_key() -> None:
    opa = _compose()["services"]["opa"]
    mounts = [m.get("source") for m in opa.get("secrets", [])]
    assert MASTER_KEY_SECRET not in mounts, "opa 服务绝不挂载 master key"
    assert mounts == [], "opa 服务不应声明任何 docker secret"


def test_opa_mounts_policies_read_only_and_entrypoint() -> None:
    opa = _compose()["services"]["opa"]
    volumes = opa.get("volumes", [])
    policies_mount = [v for v in volumes if "policies" in v]
    entrypoint_mount = [v for v in volumes if "entrypoint.sh" in v]
    assert policies_mount and all(v.endswith(":ro") or ":ro" in v for v in policies_mount), (
        "policies 必须只读挂载（容器不得改动策略源码）"
    )
    assert entrypoint_mount, "entrypoint.sh 必须挂载进容器"


def test_opa_listens_on_loopback_only() -> None:
    ports = _compose()["services"]["opa"].get("ports", [])
    assert any(str(p).startswith("127.0.0.1:8181:") for p in ports), (
        "opa 必须只暴露到 loopback 127.0.0.1:8181"
    )


class TestOpaEntrypoint:
    def test_entrypoint_builds_bundle_before_server(self) -> None:
        script = OPA_ENTRYPOINT.read_text(encoding="utf-8")
        assert "opa build" in script, "entrypoint 必须在启动 server 前用 opa build 构建 bundle"
        assert "--revision" in script, "bundle 必须携带固定 revision（client 依赖 revision 判新鲜）"
        assert "opa run --server" in script or "exec opa run" in script
        build_index = script.index("opa build")
        run_index = script.index("opa run --server")
        assert build_index < run_index, "bundle 构建必须先于 server 启动"

    def test_entrypoint_does_not_depend_on_absent_tools(self) -> None:
        # 固定镜像（1.19.0-debug，busybox）没有 wget/curl/envsubst；只检查可执行
        # 行（注释里可以解释为什么不能用这些工具）
        script_lines = [
            line
            for line in OPA_ENTRYPOINT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        script = "\n".join(script_lines)
        for tool in ("wget", "curl", "envsubst"):
            assert tool not in script, f"entrypoint 不得调用镜像内不存在的工具: {tool}"

    def test_entrypoint_uses_posix_sh_only(self) -> None:
        first_line = OPA_ENTRYPOINT.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == "#!/bin/sh", "entrypoint 必须是 POSIX sh"

    def test_entrypoint_rejects_control_characters_in_revision(self) -> None:
        """对抗（宿主层）：revision 中的控制字符/危险值必须 fail closed。

        - 非零退出（在调用 opa 之前）；
        - stderr 出现字符契约消息，但绝不回显注入值；
        - 消息只报变量名。
        """
        for dangerous in ("", "\n", "rev\n2", "rev\r2", "rev\t2", 'rev"2', "rev\\2", "rev 2"):
            printable = "".join(c for c in dangerous if c.isprintable())
            with tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(
                    ["sh", str(OPA_ENTRYPOINT)],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "OPA_BUNDLE_REVISION": dangerous,
                        "OPA_POLICY_SRC": tmp,
                    },
                )
                assert result.returncode != 0, f"危险 revision 必须 fail closed: {dangerous!r}"
                if printable:
                    assert printable not in result.stderr, "fail closed 消息不得回显注入值"
                assert "OPA_BUNDLE_REVISION" in result.stderr, "消息必须指明变量名"
                assert "fail closed" in result.stderr or "allowed" in result.stderr

    @pytest.mark.parametrize(
        "legal",
        ["s1-t3-local", "rev.1:2026-08-15", "A_2-b.3", "abc123"],
        ids=["default-style", "dotted-colon", "mixed", "plain"],
    )
    def test_entrypoint_accepts_legal_revisions(self, legal: str) -> None:
        # 合法 revision 必须通过字符契约（宿主无 opa 二进制，只能验证字符关卡本身：
        # 不出现字符契约 fail closed 消息即可证明通过）
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["sh", str(OPA_ENTRYPOINT)],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "OPA_BUNDLE_REVISION": legal,
                    "OPA_POLICY_SRC": tmp,
                },
            )
            assert "fail closed" not in result.stderr, (
                f"合法 revision 不得触发字符契约: {legal!r}\n{result.stderr}"
            )
