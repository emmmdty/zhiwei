"""HTTP/API 层。

S1-T1：identity routers 以工厂函数提供，必须显式注入 actor dependency 与 session 工厂；
本模块没有默认 actor，也没有「默认允许」占位。OIDC 真实身份依赖在 S1-T2 由 app.py
组合时接入（S1-T1 不创建 app.py，避免挂载无身份来源的路由）。

domain 层不依赖本包（依赖方向见 docs/ARCHITECTURE.md §2）。
"""

from __future__ import annotations

from zhiwei.api.memberships import create_memberships_router
from zhiwei.api.organizations import create_organizations_router
from zhiwei.api.workspaces import create_workspaces_router

__all__ = [
    "create_memberships_router",
    "create_organizations_router",
    "create_workspaces_router",
]
