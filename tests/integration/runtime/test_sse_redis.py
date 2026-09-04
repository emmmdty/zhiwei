"""S2-T7 集成：Redis 事件通道 + SSE 流契约（真实 redis-server + 真实 PG）。

事实源：specs/s2-agent-runtime.md §4/§6（cancellation/backpressure/SSE reconnect/
Redis kill）、ADR-006。

覆盖：
- RedisEventStream 作为 OutboxSink：dispatcher 消息进 run 流；
- SSE：REST 重放（首连）→ 增量（Redis 唤醒）→ cursor 续传（断线重连）；
- Redis kill：通道丢失后事件经 PG 轮询仍零丢失（spec：只影响增量延迟）；
- SSE PEP：跨租户连接 404（先于任何字节）。
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx2 import AsyncClient

from zhiwei.agents.task_graph import TaskGraph, TaskGraphNode
from zhiwei.api.events import create_events_router
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.outbox import OutboxDelivery
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.run_commands import RunCommandService
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.telemetry.redis_streams import RedisEventStream

pytestmark = pytest.mark.asyncio


def _app_url() -> str:
    import os

    dsn = os.environ.get(
        "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
    )
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest_asyncio.fixture
async def tenant(sessions) -> TenantContext:
    organization_id, workspace_id = uuid_module.uuid4(), uuid_module.uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="sse")
    assert context.organization_id is not None and context.workspace_id is not None
    return context


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    engine = create_database_engine(_app_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sessions(engine) -> AsyncIterator[Any]:
    yield create_session_factory(engine)


@pytest_asyncio.fixture
async def redis_stream(redis_server) -> AsyncIterator[RedisEventStream]:
    url, _proc, _dir = redis_server
    stream = await RedisEventStream.connect(url)
    try:
        yield stream
    finally:
        await stream.close()


def _outbox_message(
    context: TenantContext, run_id: str, sequence_no: int
) -> OutboxDelivery:
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    from zhiwei.contracts.identifiers import new_id

    return OutboxDelivery(
        id=new_id(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
        topic="canonical.event.committed",
        event_key=str(new_id()),
        payload={
            "event_id": str(new_id()),
            "run_id": run_id,
            "sequence_no": sequence_no,
            "event_digest": f"sha256:{'0' * 64}",
        },
        status="processing",
        attempts=0,
        available_at=now,
        claimed_by="test",
        claim_token=new_id(),
        lease_expires_at=now + timedelta(seconds=30),
    )


class TestRedisEventStreamAsSink:
    async def test_publish_delivers_run_notifications(self, redis_stream, tenant) -> None:
        run_id = str(uuid_module.uuid4())
        for seq in (1, 2, 3):
            await redis_stream.publish(_outbox_message(tenant, run_id, seq))
        notices = await redis_stream.read_since(run_id, "0-0")
        assert [int(n["sequence_no"]) for n in notices] == [1, 2, 3]

    async def test_read_since_is_incremental(self, redis_stream, tenant) -> None:
        run_id = str(uuid_module.uuid4())
        await redis_stream.publish(_outbox_message(tenant, run_id, 1))
        first = await redis_stream.read_since(run_id, "0-0")
        assert len(first) == 1
        await redis_stream.publish(_outbox_message(tenant, run_id, 2))
        second = await redis_stream.read_since(run_id, first[0]["id"])
        assert [int(n["sequence_no"]) for n in second] == [2]


def _actor(context: TenantContext) -> ActorContext:
    assert context.organization_id is not None and context.workspace_id is not None
    return ActorContext(
        principal_id=uuid_module.uuid4(),
        organization_id=context.organization_id,
        workspace_id=context.workspace_id,
    )


def _sse_app(
    sessions,
    context: TenantContext,
    redis_stream: RedisEventStream | None,
):
    from fastapi import FastAPI

    app = FastAPI()

    def tenant_factory(actor: ActorContext) -> TenantContext:
        # 测试约定：actor 的 org/ws 即 tenant（S1 API 同构）；fail closed
        assert actor.organization_id is not None and actor.workspace_id is not None
        return TenantContext(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    async def run_exists(ctx: TenantContext, run_id: uuid_module.UUID) -> bool:
        from sqlalchemy import select

        from zhiwei.persistence.models import Run
        from zhiwei.persistence.tenant import tenant_session

        async with tenant_session(sessions, ctx) as session:
            row = await session.scalar(
                select(Run).where(
                    Run.id == run_id,
                    Run.organization_id == ctx.organization_id,
                    Run.workspace_id == ctx.workspace_id,
                )
            )
            return row is not None

    app.include_router(
        create_events_router(
            actor_dependency=lambda: _actor(context),
            session_factory_factory=lambda: sessions,
            tenant_context_factory=tenant_factory,
            run_exists=run_exists,
            redis_stream=redis_stream,
        )
    )
    return app


async def _seed_run_with_events(
    sessions, context: TenantContext, event_count: int
) -> str:

    graph = TaskGraph(
        nodes={
            "t1": TaskGraphNode(task_id="t1", task_type="Fixture", required_capability="f")
        },
        edges={},
    )
    run_id = uuid_module.uuid4()
    async with tenant_session(sessions, context) as session:
        await RunCommandService(session, context).submit_start_run(
            run_id=run_id, graph=graph.model_dump(mode="json"), task_queue="unused"
        )
    async with tenant_session(sessions, context) as session:
        store = RuntimeEventStore(session, context)
        from zhiwei.runtime.events import RunCreated, RunStarted, TaskScheduled

        events = [
            RunCreated(run_id=run_id, timestamp=_utcnow(), graph=graph),
            RunStarted(run_id=run_id, timestamp=_utcnow()),
        ]
        for _i in range(event_count - len(events)):
            events.append(
                TaskScheduled(run_id=run_id, timestamp=_utcnow(), task_id="t1")
            )
        for event in events:
            await store.append(
                event, actor_ref="test", idempotency_key=f"sse:{uuid_module.uuid4()}"
            )
    return str(run_id)


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)


class TestSSEJourney:
    async def test_replay_then_increment_and_cursor_reconnect(
        self, sessions, tenant, redis_stream
    ) -> None:
        run_id = await _seed_run_with_events(sessions, tenant, 4)
        app = _sse_app(sessions, tenant, redis_stream)
        received: list[tuple[int, dict]] = []

        async with (
            _uvicorn_server(app) as base_url,
            AsyncClient(base_url=base_url) as client,
            client.stream("GET", f"/api/v1/runs/{run_id}/stream", timeout=5.0) as response,
        ):
            # 首连：REST 重放全部既有事件
                assert response.status_code == 200
                deadline = asyncio.get_event_loop().time() + 5
                async for line in response.aiter_lines():
                    if asyncio.get_event_loop().time() > deadline:
                        break
                    if line.startswith("id: "):
                        received.append((int(line[4:].strip()), {}))
                    if len(received) >= 4:
                        break
        assert [seq for seq, _ in received] == [1, 2, 3, 4]

        # 新事件经 Redis 通道发布 → SSE 增量收到
        async with tenant_session(sessions, tenant) as session:
            store = RuntimeEventStore(session, tenant)
            from zhiwei.contracts.identifiers import new_id
            from zhiwei.runtime.events import TaskStarted

            await store.append(
                TaskStarted(
                    run_id=uuid_module.UUID(run_id),
                    timestamp=_utcnow(),
                    task_id="t1",
                    attempt_id=new_id(),
                ),
                actor_ref="test",
                idempotency_key=f"sse:{new_id()}",
            )
        await redis_stream.publish(_outbox_message(tenant, run_id, 5))

        # 断线重连（cursor=4）：只收 seq>4 的事件
        async with (
            _uvicorn_server(app) as base_url,
            AsyncClient(base_url=base_url) as client,
            client.stream(
                "GET",
                f"/api/v1/runs/{run_id}/stream",
                params={"cursor": "4"},
                timeout=5.0,
            ) as response,
        ):
                assert response.status_code == 200
                reconnected: list[int] = []
                deadline = asyncio.get_event_loop().time() + 6
                async for line in response.aiter_lines():
                    if asyncio.get_event_loop().time() > deadline:
                        break
                    if line.startswith("id: "):
                        reconnected.append(int(line[4:].strip()))
                        if reconnected[-1] == 5:
                            break
                assert 5 in reconnected, (
                    f"cursor reconnect missed seq 5: {reconnected}"
                )

    async def test_redis_kill_falls_back_to_pg_polling(
        self, sessions, tenant, redis_server
    ) -> None:
        """Redis 进程被杀 → SSE 降级 PG 轮询，事件仍零丢失（spec §4 契约）。"""
        url, proc, _dir = redis_server
        run_id = await _seed_run_with_events(sessions, tenant, 2)
        stream = await RedisEventStream.connect(url)
        app = _sse_app(sessions, tenant, stream)

        received: list[int] = []
        async with (
            _uvicorn_server(app) as base_url,
            AsyncClient(base_url=base_url, timeout=30.0) as client,
        ):
            stream_task = asyncio.create_task(
                _drain_stream(client, f"/api/v1/runs/{run_id}/stream", received)
            )
            await asyncio.sleep(0.5)  # 首连重放完成
            proc.terminate()
            proc.wait(timeout=10)
            await asyncio.sleep(1.5)  # 触发降级窗口
            # Redis 死后写入的事件：经 PG 轮询仍必达
            async with tenant_session(sessions, tenant) as session:
                store = RuntimeEventStore(session, tenant)
                from zhiwei.contracts.identifiers import new_id
                from zhiwei.runtime.events import TaskScheduled

                await store.append(
                    TaskScheduled(
                        run_id=uuid_module.UUID(run_id),
                        timestamp=_utcnow(),
                        task_id="t1",
                    ),
                    actor_ref="test",
                    idempotency_key=f"sse:{new_id()}",
                )
            deadline = asyncio.get_event_loop().time() + 8
            while asyncio.get_event_loop().time() < deadline and 3 not in received:
                await asyncio.sleep(0.2)
            stream_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream_task
        assert 3 in received, f"event after redis kill was lost: {received}"

    async def test_cross_tenant_stream_is_404(self, sessions, tenant, redis_stream) -> None:
        run_id = await _seed_run_with_events(sessions, tenant, 1)
        other_context = TenantContext(
            organization_id=uuid_module.uuid4(), workspace_id=uuid_module.uuid4()
        )
        app = _sse_app(sessions, other_context, redis_stream)
        async with (
            _uvicorn_server(app) as base_url,
            AsyncClient(base_url=base_url) as client,
        ):
            response = await client.get(f"/api/v1/runs/{run_id}/stream")
            assert response.status_code == 404


async def _drain_stream(client: AsyncClient, path: str, received: list[int]) -> None:
    async with client.stream("GET", path, timeout=30.0) as response:
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                received.append(int(line[4:].strip()))



@asynccontextmanager
async def _uvicorn_server(app: Any) -> AsyncIterator[str]:
    """真实 uvicorn（进程内 task）：ASGITransport 对 SSE 早退的 disconnect
    投递不可靠（minimal repro 挂起），真实 HTTP 才有正确的断连语义。"""
    import socket

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="off"
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    deadline = asyncio.get_event_loop().time() + 10
    while not server.started:
        if asyncio.get_event_loop().time() > deadline or server_task.done():
            raise RuntimeError("uvicorn failed to start")
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await server_task
