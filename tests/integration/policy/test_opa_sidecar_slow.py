"""S1-T3 RED：真实 OPA 容器生命周期场景（slow，需要 docker）。

真实覆盖（非 mock）三个冻结场景（PERMISSIONS.md §15 / specs/s1 §5）：
- OPA unavailable：容器停止后任何需要求值的请求 fail closed，不回落到缓存 allow；
- stale bundle：bundle revision 更新后，旧 revision 的缓存条目立即失效；
- policy update during request：bundle 策略收紧后，后续请求按新策略判定，
  已入缓存的旧策略 allow 不得再被服务。

与 test_identity_profile.py 同款纪律：只在 finally 里还原 opa 服务，不动 postgres；
无 docker 时跳过并给出明确理由（slow 显式运行，不算断言放宽）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from zhiwei.policy.client import OPAClient
from zhiwei.policy.enforcement import PolicyEnforcer

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.test.yaml"
POLICIES_DIR = REPO_ROOT / "policies"
OPA_URL = "http://127.0.0.1:8181"
ORG = "00000000-0000-0000-0000-000000000001"

COMPOSE_CMD = ["docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "identity"]


def _wait_healthy(deadline: float = 120.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            resp = httpx.get(f"{OPA_URL}/health?bundles", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("opa 服务未在期限内通过 /health?bundles")


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*COMPOSE_CMD, *args], capture_output=True, text=True, timeout=300, env=env or os.environ
    )


U1 = "00000000-0000-0000-0000-0000000000a1"
R1 = "00000000-0000-0000-0000-0000000000b1"


def _input_doc(*, purpose: str = "general") -> dict:
    return {
        "organization_id": ORG,
        "workspace_id": None,
        "actor": {"principal_id": U1, "kind": "user", "roles": [
            {"name": "org_owner", "scope": "org", "organization_id": ORG, "workspace_id": None},
        ]},
        "resource": {"type": "org", "id": R1, "version": "v1"},
        "action": "manage",
        "purpose": purpose,
        "classification": None,
        "risk": None,
        "delegation": [],
        "resource_context": {},
        "context": {"now": "2026-08-15T00:00:00Z", "classification_ceiling": None,
                    "requires_delegation": False},
    }


def _new_enforcer(cache_ttl: float) -> PolicyEnforcer:
    return PolicyEnforcer(OPAClient(OPA_URL, cache_maxsize=64, cache_ttl_seconds=cache_ttl))


@pytest.mark.slow
@pytest.mark.asyncio
class TestOpaLifecycle:
    @pytest.fixture()
    def opa_service(self) -> Iterator[None]:
        """确保 opa 服务处于 compose 定义的正常状态（还原任何测试残留）。"""
        if shutil.which("docker") is None:
            pytest.skip("环境守卫：无 docker 无法执行真实容器生命周期场景（slow 显式运行）")
        _run("rm", "-sf", "opa")
        up = _run("up", "-d", "--force-recreate", "--wait", "opa")
        assert up.returncode == 0, f"opa 启动失败:\n{up.stdout}\n{up.stderr}"
        _wait_healthy()
        yield
        _run("rm", "-sf", "opa")
        up = _run("up", "-d", "--wait", "opa")
        assert up.returncode == 0, f"opa 还原失败:\n{up.stdout}\n{up.stderr}"
        _wait_healthy()

    async def test_opa_unavailable_fails_closed_without_cache_fallback(
        self, opa_service: None
    ) -> None:
        enforcer = _new_enforcer(cache_ttl=3600)  # 长 TTL：证明不是 TTL 而是不可用性在拒绝

        # 预热缓存：allow 决策入缓存
        d1 = await enforcer.authorize(_input_doc())
        assert d1.allow is True and d1.revision

        # 停止真实 OPA 容器
        stopped = _run("stop", "opa")
        assert stopped.returncode == 0, stopped.stderr

        try:
            # 有界缓存契约（PERMISSIONS.md:85）：同 input 的 allow 只在 TTL+revision
            # 界内复用（不联系 OPA）——这是缓存存在的意义；任何需要求值的请求
            # （TTL 过期 / revision 变化 / 其他 input）在 OPA 不可用时必须拒绝。
            d_same = await enforcer.authorize(_input_doc())
            assert d_same.allow is True and d_same.decision_id == d1.decision_id, (
                "TTL+revision 界内的同 input 复用是契约允许的有界缓存行为"
            )
            d2 = await enforcer.authorize(_input_doc(purpose="compliance"))
            assert d2.allow is False
            assert d2.reason == "opa_unavailable"
            assert d2.decision_id is None and d2.revision is None, (
                "fail closed 决策不得伪造 decision_id/revision"
            )
        finally:
            _run("start", "opa")
            _wait_healthy()

    async def test_stale_bundle_invalidates_cached_allow(self, opa_service: None) -> None:
        # revision R1：缓存 allow
        _run("rm", "-sf", "opa")
        up = _run("up", "-d", "--force-recreate", "--wait", "opa",
                  env={**os.environ, "OPA_BUNDLE_REVISION": "rev-stale-1"})
        assert up.returncode == 0, up.stderr
        _wait_healthy()

        enforcer = _new_enforcer(cache_ttl=3600)
        d1 = await enforcer.authorize(_input_doc())
        assert d1.allow is True and d1.revision == "rev-stale-1"

        # bundle 更新到 R2（同一策略，revision 变更）
        _run("rm", "-sf", "opa")
        up = _run("up", "-d", "--force-recreate", "--wait", "opa",
                  env={**os.environ, "OPA_BUNDLE_REVISION": "rev-stale-2"})
        assert up.returncode == 0, up.stderr
        _wait_healthy()

        # 新请求感知 R2 → 旧 R1 缓存条目失效
        d2 = await enforcer.authorize(_input_doc(purpose="compliance"))
        assert d2.revision == "rev-stale-2", "新请求必须看到新 revision"
        d3 = await enforcer.authorize(_input_doc())
        assert d3.revision == "rev-stale-2", "R1 的缓存 allow 不得在 R2 继续服务"
        assert d3.allow is True

    async def test_policy_update_during_request_denies_under_new_policy(
        self, opa_service: None
    ) -> None:
        # 阶段 1：真实 bundle（R1），org_owner/manage 被允许
        _run("rm", "-sf", "opa")
        up = _run("up", "-d", "--force-recreate", "--wait", "opa",
                  env={**os.environ, "OPA_BUNDLE_REVISION": "rev-update-1"})
        assert up.returncode == 0, up.stderr
        _wait_healthy()

        enforcer = _new_enforcer(cache_ttl=3600)
        d1 = await enforcer.authorize(_input_doc())
        assert d1.allow is True and d1.revision == "rev-update-1"

        # 阶段 2：收紧策略（临时 rego 副本 + 硬拒绝规则）+ 新 revision，重建容器
        tightened = (
            (POLICIES_DIR / "zhiwei" / "authz.rego").read_text(encoding="utf-8")
            + "\n# slow test: tightened policy during request\n"
            + 'sod_deny contains "slow_test_tightened_org_manage" if {\n'
            + '    input.resource.type == "org"\n'
            + '    input.action == "manage"\n'
            + "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_policies = Path(tmp) / "policies" / "zhiwei"
            tmp_policies.mkdir(parents=True)
            (tmp_policies / "authz.rego").write_text(tightened, encoding="utf-8")
            override = Path(tmp) / "override.yaml"
            # volumes 列表在 compose 中是拼接而非替换：收紧策略挂到独立目标，
            # 通过 OPA_POLICY_SRC 指向它，避免与原 /policies 挂载冲突。
            override.write_text(
                "services:\n"
                "  opa:\n"
                "    environment:\n"
                f"      OPA_POLICY_SRC: /tightened-policies/zhiwei\n"
                "    volumes:\n"
                f"      - {tmp_policies.parent}:/tightened-policies:ro\n",
                encoding="utf-8",
            )
            up = subprocess.run(
                [*COMPOSE_CMD, "-f", str(override), "up", "-d", "--force-recreate", "--wait", "opa"],
                capture_output=True, text=True, timeout=300,
                env={**os.environ, "OPA_BUNDLE_REVISION": "rev-update-2"},
            )
            assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"
            _wait_healthy()

            # 收紧策略生效：新请求看到 R2，org_owner/manage 被拒
            d2 = await enforcer.authorize(_input_doc(purpose="compliance"))
            assert d2.revision == "rev-update-2"
            assert d2.allow is False, "收紧后的策略必须拒绝 org/manage"
            assert "sod_deny" in d2.reason or "slow_test_tightened" in d2.reason

            # 旧缓存中的 R1 allow 不得再被服务：同一 input 必须按新策略判定
            d3 = await enforcer.authorize(_input_doc())
            assert d3.revision == "rev-update-2" and d3.allow is False, (
                "policy update during request: 缓存中的旧 allow 不得越过 bundle 更新"
            )
