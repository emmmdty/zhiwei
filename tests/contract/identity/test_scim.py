"""S1-T5 RED：SCIM router 组合契约与子集矩阵（无 DB，stub actor）。

冻结契约（docs/handoffs/s1-t5-design.md §1/§7/§8/§10，以冻结契约为准）：
- create_scim_router 组合期必需注入 actor_dependency / sessions /
  identity_sessions / policy_enforcer；显式 policy_enforcer=None → TypeError
  （fail closed，与 T4 router factory 同款）；
- SCIM 2.0 必需子集之外的一切操作显式 501 + RFC 7644 §3.12 错误体（绝不静默忽略）；
- 未知属性（Pydantic extra=forbid）→ 400 invalidSyntax；externalId 缺失 → 400；
  POST userName≠externalId → 400 invalidValue；externalId≠displayName → 400
  invalidValue；PATCH op≠replace → 501；PATCH path≠active → 400 noTarget；
  PATCH value 非布尔 → 400 invalidValue；GET filter → 400 invalidFilter；
  If-Match 头 → 400（S1 不支持版本化）；路径 UUID 非法 → 400 invalidValue
  （str 接收手工解析，避免 FastAPI 422）；
- 读与写都要求 org context（无 org → 403 organization context required）；
- 本文件用 stub actor，只覆盖在触达 DB 之前的真实拒绝路径；supported-path 行为
  全部在 integration（真实 DB + 真实登录）。

RED 状态：zhiwei.api.scim 模块尚不存在 → 本文件 import 时整文件收集失败
（ModuleNotFoundError）——契约面缺失是正确失败原因（新模块 RED 惯例，
T4 同款：组合契约缺失）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fixtures.policy_fake import FakePolicyEnforcer
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from zhiwei.identity.domain import ActorContext

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _org_actor() -> ActorContext:
    return ActorContext(principal_id=uuid4(), organization_id=uuid4())


def _ws_actor() -> ActorContext:
    organization_id = uuid4()
    return ActorContext(
        principal_id=uuid4(), organization_id=organization_id, workspace_id=uuid4()
    )


def _no_org_actor() -> ActorContext:
    return ActorContext(principal_id=uuid4())


_TEST_ISSUER = "https://idp.example.com"


def _make_app(
    *,
    actor: object = _org_actor,
    enforcer: FakePolicyEnforcer | None = None,
) -> FastAPI:
    """手工组装 SCIM router；session factories 无 bind（本文件不触达 DB）。"""
    from zhiwei.api.scim import create_scim_router

    app = FastAPI()
    app.include_router(
        create_scim_router(
            actor_dependency=actor,  # type: ignore[arg-type]
            sessions=async_sessionmaker(),
            identity_sessions=async_sessionmaker(),
            policy_enforcer=enforcer or FakePolicyEnforcer(),
            issuer=_TEST_ISSUER,
        )
    )
    return app


def _user_body(external_id: str, *, extra: dict | None = None) -> dict:
    body: dict = {
        "schemas": [USER_SCHEMA],
        "externalId": external_id,
        "userName": external_id,
    }
    body.update(extra or {})
    return body


def _group_body(name: str, *, extra: dict | None = None) -> dict:
    body: dict = {
        "schemas": [GROUP_SCHEMA],
        "externalId": name,
        "displayName": name,
    }
    body.update(extra or {})
    return body


def _patch_body(op: str, path: str, value: object) -> dict:
    return {
        "schemas": [PATCH_SCHEMA],
        "Operations": [{"op": op, "path": path, "value": value}],
    }


def _assert_scim_error(response, status: int, *, scim_type: str | None = None) -> dict:
    assert response.status_code == status
    error = response.json()
    assert error["schemas"] == [ERROR_SCHEMA]
    assert error["status"] == str(status)
    if scim_type is not None:
        assert error["scimType"] == scim_type
    assert error["detail"]
    return error


# --------------------------------------------------------------------------- 组合契约


def test_composition_requires_mandatory_injections() -> None:
    from zhiwei.api.scim import create_scim_router

    with pytest.raises(TypeError):
        create_scim_router()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        create_scim_router(
            actor_dependency=_org_actor,
            sessions=async_sessionmaker(),
            identity_sessions=async_sessionmaker(),
            policy_enforcer=None,  # type: ignore[arg-type]  # 测试 None 拒绝
            issuer=_TEST_ISSUER,
        )
    with pytest.raises(TypeError):
        create_scim_router(  # type: ignore[call-arg]
            actor_dependency=_org_actor,
            sessions=async_sessionmaker(),
        )


# --------------------------------------------------------------------------- 501 矩阵


@pytest.mark.asyncio
async def test_unsupported_operations_return_501_scim_error() -> None:
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        user_id = str(uuid4())
        group_id = str(uuid4())
        cases = [
            ("GET", "/scim/v2/Users", None),
            ("DELETE", f"/scim/v2/Users/{user_id}", None),
            ("PATCH", f"/scim/v2/Groups/{group_id}", _patch_body("add", "members", [])),
            ("DELETE", f"/scim/v2/Groups/{group_id}", None),
            ("POST", "/scim/v2/Bulk", {"schemas": [], "Operations": []}),
            ("GET", "/scim/v2/Me", None),
            ("GET", "/scim/v2/ServiceProviderConfig", None),
            ("GET", "/scim/v2/ResourceTypes", None),
            ("GET", "/scim/v2/Schemas", None),
            ("POST", "/scim/v2/.search", {"schemas": []}),
        ]
        for method, path, body in cases:
            response = await client.request(method, path, json=body)
            _assert_scim_error(response, 501)


# --------------------------------------------------------------------------- 400 校验（触达 DB 之前）


@pytest.mark.asyncio
async def test_user_create_validation_errors_are_scim_shaped() -> None:
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        _assert_scim_error(
            await client.post("/scim/v2/Users", json=_user_body("u1", extra={"bogus": 1})),
            400,
            scim_type="invalidSyntax",
        )
        missing = {"schemas": [USER_SCHEMA], "userName": "u1"}
        _assert_scim_error(
            await client.post("/scim/v2/Users", json=missing),
            400,
            scim_type="invalidSyntax",
        )
        mismatch = {"schemas": [USER_SCHEMA], "externalId": "u1", "userName": "other"}
        _assert_scim_error(
            await client.post("/scim/v2/Users", json=mismatch),
            400,
            scim_type="invalidValue",
        )
        _assert_scim_error(
            await client.post("/scim/v2/Users", json=_user_body("u1", extra={"issuer": "x"})),
            400,
            scim_type="invalidSyntax",
        )


@pytest.mark.asyncio
async def test_patch_user_subset_rejects_unsupported_ops_and_paths() -> None:
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        user_id = str(uuid4())
        _assert_scim_error(
            await client.patch(
                f"/scim/v2/Users/{user_id}",
                json=_patch_body("add", "emails", [{"value": "a@b.c"}]),
            ),
            501,
        )
        _assert_scim_error(
            await client.patch(
                f"/scim/v2/Users/{user_id}",
                json=_patch_body("replace", "displayName", "x"),
            ),
            400,
            scim_type="noTarget",
        )
        _assert_scim_error(
            await client.patch(
                f"/scim/v2/Users/{user_id}",
                json=_patch_body("replace", "active", "false"),
            ),
            400,
            scim_type="invalidValue",
        )
        empty = {"schemas": [PATCH_SCHEMA], "Operations": []}
        _assert_scim_error(
            await client.patch(f"/scim/v2/Users/{user_id}", json=empty),
            400,
            scim_type="invalidSyntax",
        )


@pytest.mark.asyncio
async def test_group_validation_and_rejections() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_make_app(actor=_ws_actor)), base_url="http://test"
    ) as client:
        _assert_scim_error(
            await client.post("/scim/v2/Groups", json=_group_body("x", extra={"displayName": "y"})),
            400,
            scim_type="invalidValue",
        )
        unknown = _group_body("g1", extra={"description": "d"})
        _assert_scim_error(
            await client.post("/scim/v2/Groups", json=unknown),
            400,
            scim_type="invalidSyntax",
        )
        _assert_scim_error(
            await client.get("/scim/v2/Groups?filter=displayName%20eq%20%22g1%22"),
            400,
            scim_type="invalidFilter",
        )


@pytest.mark.asyncio
async def test_if_match_header_rejected_when_versioning_unsupported() -> None:
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        user_id = str(uuid4())
        _assert_scim_error(
            await client.put(
                f"/scim/v2/Users/{user_id}",
                json=_user_body("u1"),
                headers={"If-Match": '"v1"'},
            ),
            400,
        )


@pytest.mark.asyncio
async def test_invalid_uuid_path_returns_400_scim_error() -> None:
    async with AsyncClient(transport=ASGITransport(app=_make_app()), base_url="http://test") as client:
        _assert_scim_error(
            await client.get("/scim/v2/Users/not-a-uuid"),
            400,
            scim_type="invalidValue",
        )
    async with AsyncClient(
        transport=ASGITransport(app=_make_app(actor=_ws_actor)), base_url="http://test"
    ) as client:
        _assert_scim_error(
            await client.get("/scim/v2/Groups/not-a-uuid"),
            400,
            scim_type="invalidValue",
        )


# --------------------------------------------------------------------------- 上下文要求


@pytest.mark.asyncio
async def test_mutations_and_reads_require_organization_context() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_make_app(actor=_no_org_actor)), base_url="http://test"
    ) as client:
        _assert_scim_error(
            await client.post("/scim/v2/Users", json=_user_body("u1")),
            403,
        )
        _assert_scim_error(
            await client.get(f"/scim/v2/Users/{uuid4()}"),
            403,
        )
        _assert_scim_error(
            await client.get("/scim/v2/Groups"),
            403,
        )