"""R2-A（NEW-1）：claim register 端点的 422 字符集拒绝面。

拒绝路径按构造不触数据库（service 在任何会话 I/O 之前以 ClaimIdInvalid 拒绝），
因此端点层测试用最小会话替身走真实 router（FakePolicyEnforcer 放行 authorize），
断言 422 + {"reason","message"} 机器可读形状；调用方按 reason 分支而不是解析
消息文本。PG 集成路径（201/升级/列表）由 test_releases_api.py 覆盖。
"""

from __future__ import annotations

from typing import Any, cast
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.policy_fake import FakePolicyEnforcer
from zhiwei.api.claims import create_claims_router
from zhiwei.identity.domain import ActorContext, ActorRoleBinding
from zhiwei.object_store.posix import PosixObjectStore

SCOPE: dict[str, Any] = {
    "mode": "offline",
    "model": "reference-fixture",
    "version": "1",
    "date": "2026-09-05",
    "corpus": "factqa-v1",
    "environment": "offline-fixture",
}


class _RefusalSession:
    """拒绝路径的 session 替身：tenant_session 需要的事务面 + 空操作 execute。"""

    async def __aenter__(self) -> _RefusalSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def begin(self) -> _RefusalSession:
        return self

    async def __enter__(self) -> _RefusalSession:
        return self

    async def __exit__(self, *_exc: object) -> None:
        return None

    def in_transaction(self) -> bool:
        return True

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


class _RefusalSessionFactory:
    def __call__(self) -> _RefusalSession:
        return _RefusalSession()


def _app() -> FastAPI:
    organization_id, workspace_id = uuid4(), uuid4()
    actor = ActorContext(
        principal_id=uuid4(),
        organization_id=organization_id,
        workspace_id=workspace_id,
        role_bindings=(
            ActorRoleBinding(
                name="agent_builder",
                scope="workspace",
                organization_id=organization_id,
                workspace_id=workspace_id,
            ),
        ),
    )
    app = FastAPI()
    app.include_router(
        create_claims_router(
            actor_dependency=lambda: actor,
            sessions=cast(async_sessionmaker[AsyncSession], _RefusalSessionFactory()),
            policy_enforcer=FakePolicyEnforcer(allow=True),
            object_store=PosixObjectStore(Path(f"/tmp/r2a-claims-{uuid4().hex}")),
        )
    )
    return app


@pytest.mark.asyncio
class TestRegisterCharsetRefusal:
    async def test_space_bearing_claim_id_is_422_machine_readable(self) -> None:
        app = _app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/claims",
                json={
                    "claim_id": "fact qa v1",
                    "statement": "FactQA accuracy {{accuracy}}",
                    "scope": SCOPE,
                },
            )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["reason"] == "invalid_claim_id"
        assert detail["message"]
