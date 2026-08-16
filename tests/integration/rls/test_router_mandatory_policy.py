"""S1-T4 二轮修复 RED：router 组合期强制注入 policy_enforcer（无策略直通路径删除）。

当前实现 policy_enforcer 是可选注入（默认 None），缺省时 mutation 不经过 PEP——生产
app 之外存在无策略直通路径。二轮修复将其改为组合期必需关键字参数：缺失 → TypeError。

RED 失败原因：今日不抛 TypeError（可选参数有默认值），本文件全部用例失败。
GREEN 后：三个 router factory 缺少 policy_enforcer 一律 TypeError。
"""

from __future__ import annotations

import pytest

from zhiwei.api.memberships import create_memberships_router
from zhiwei.api.organizations import create_organizations_router
from zhiwei.api.workspaces import create_workspaces_router


def _test_policy_enforcer() -> object:
    from fixtures.policy_fake import FakePolicyEnforcer
    return FakePolicyEnforcer()


def test_organizations_router_requires_policy_enforcer() -> None:
    with pytest.raises(TypeError):
        create_organizations_router(
            actor_dependency=lambda: None,  # type: ignore[arg-type]
            sessions=object(),  # type: ignore[arg-type]
            identity_sessions=object(),  # type: ignore[arg-type]
        )


def test_workspaces_router_requires_policy_enforcer() -> None:
    with pytest.raises(TypeError):
        create_workspaces_router(
            actor_dependency=lambda: None,  # type: ignore[arg-type]
            sessions=object(),  # type: ignore[arg-type]
        )


def test_memberships_router_requires_policy_enforcer() -> None:
    with pytest.raises(TypeError):
        create_memberships_router(
            actor_dependency=lambda: None,  # type: ignore[arg-type]
            sessions=object(),  # type: ignore[arg-type]
        )


def test_router_factory_accepts_explicit_policy_enforcer() -> None:
    """显式注入合法 fake 必须组合成功（GREEN 后的正面对照）。"""
    enforcer = _test_policy_enforcer()
    router = create_organizations_router(
        actor_dependency=lambda: None,  # type: ignore[arg-type]
        sessions=object(),  # type: ignore[arg-type]
        identity_sessions=object(),  # type: ignore[arg-type]
        policy_enforcer=enforcer,  # type: ignore[arg-type]
    )
    assert router.prefix == "/api/v1/organizations"
