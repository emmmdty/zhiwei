"""S10 fix-A RED：agent_draft.read 矩阵缺口（R1 security D2）——真实 OPA 契约。

事实源：docs/PERMISSIONS.md §3.1 冻结矩阵 + ADR-015（本轮设计方 pre-made 裁决：
Builder 必须能读自己 workspace 的 draft——「创建/编辑/运行 draft」而不给 read
是冻结矩阵的落地缺口，不是新授权语义）。Rego 是角色→权限的唯一事实实现；本测试
用真实 PolicyInput 形状（src/zhiwei/policy/input.py 严格 schema）经真实 OPA
（compose opa 服务）判定，不用任何 mock/fake input。

矩阵语义（与本测试钉死的判定一致）：
- agent_builder（workspace 作用域绑定须匹配 input.workspace_id）→ allow；
- workspace_admin → allow（既有 cell 不变）；
- auditor → deny（冻结矩阵只给 agent_draft.read_version_gate，不给 draft read；
  扩权属新安全决策，不在本轮裁决范围）；
- member → deny（矩阵「—」）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from zhiwei.identity.domain import PrincipalKind
from zhiwei.policy.client import OPAClient
from zhiwei.policy.input import (
    Actor,
    PolicyInput,
    RequestContext,
    ResourceRef,
    binding_from_membership,
)
from zhiwei.policy.roles import (
    Action,
    Purpose,
    ResourceType,
    Role,
    RoleScope,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.test.yaml"
OPA_URL = "http://127.0.0.1:8181"
ORG_ID = uuid4()
WS_ID = uuid4()
DRAFT_ID = uuid4()

_COMPOSE_CMD = ["docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "identity"]


def _run_compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_COMPOSE_CMD, *args], capture_output=True, text=True, timeout=300, env=os.environ
    )


def _wait_healthy(deadline: float = 120.0) -> None:
    import time

    from httpx2 import HTTPError, get

    start = time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            if get(f"{OPA_URL}/health?bundles", timeout=2.0).status_code == 200:
                return
        except HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("opa 服务未在期限内通过 /health?bundles")


@pytest.fixture(scope="class")
def opa_bundle_current() -> Iterator[None]:
    """bundle 在容器启动期从仓库 policies 构建——regox 改动后必须重建再判定。

    与 test_opa_sidecar_slow.py 同款纪律：只还原 opa 服务，不动 postgres；
    无 docker 时跳过（slow 显式运行，ADR-012 §5）。
    """
    if shutil.which("docker") is None:
        pytest.skip("环境守卫：无 docker 无法执行真实 OPA 纵切（slow 显式运行）")
    rebuild = subprocess.run(
        [*_COMPOSE_CMD, "up", "-d", "--force-recreate", "--wait", "opa"],
        capture_output=True,
        text=True,
        timeout=300,
        env=os.environ,
    )
    assert rebuild.returncode == 0, f"opa 重建失败:\n{rebuild.stdout}\n{rebuild.stderr}"
    _wait_healthy()
    yield
    restore = _run_compose("up", "-d", "--wait", "opa")
    assert restore.returncode == 0, f"opa 还原失败:\n{restore.stdout}\n{restore.stderr}"
    _wait_healthy()


def _draft_read_input(role: Role) -> PolicyInput:
    scope = RoleScope.WORKSPACE if role in {
        Role.WORKSPACE_ADMIN,
        Role.AGENT_BUILDER,
    } else RoleScope.ORG
    return PolicyInput(
        actor=Actor(
            principal_id=uuid4(),
            kind=PrincipalKind.USER,
            roles=(
                binding_from_membership(
                    role.value,
                    scope=scope,
                    organization_id=ORG_ID,
                    workspace_id=WS_ID if scope == RoleScope.WORKSPACE else None,
                ),
            ),
        ),
        organization_id=ORG_ID,
        workspace_id=WS_ID,
        resource=ResourceRef(type=ResourceType.AGENT_DRAFT, id=DRAFT_ID, version="v1"),
        action=Action.READ,
        purpose=Purpose.GENERAL,
        context=RequestContext(now=datetime.now(tz=UTC)),
    )


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.usefixtures("opa_bundle_current")
class TestAgentDraftReadMatrix:
    async def _decide(self, role: Role) -> bool:
        client = OPAClient(OPA_URL)
        try:
            decision = await client.evaluate(_draft_read_input(role).model_dump(mode="json"))
        finally:
            await client.aclose()
        assert decision.decision_id is not None, f"OPA 未真实求值: {decision.reason}"
        return decision.allow

    async def test_agent_builder_reads_own_workspace_draft(self) -> None:
        # R1 security D2 的核心缺口：builder 创建/编辑/运行 draft 却读不了 draft。
        assert await self._decide(Role.AGENT_BUILDER) is True

    async def test_workspace_admin_keeps_draft_read(self) -> None:
        assert await self._decide(Role.WORKSPACE_ADMIN) is True

    async def test_auditor_has_no_draft_read_cell(self) -> None:
        # 冻结矩阵：auditor 只读 version/gate。此处钉死「未扩权」，
        # 防止实现悄悄把 auditor 放进 read cell。
        assert await self._decide(Role.AUDITOR) is False

    async def test_member_cannot_read_draft(self) -> None:
        assert await self._decide(Role.MEMBER) is False
