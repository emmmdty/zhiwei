"""S2-T7：SSE run 事件流——REST 重放（骨架）+ Redis 增量（加速）+ PG 兜底。

事实源：specs/s2-agent-runtime.md §4/§5（SSE cursor/reconnect、Redis 丢失只影响
增量）、ADR-006。

游标 = canonical sequence_no（十进制字符串）。每轮循环：PG 重放 seq > 游标的
事件（零丢失基线）→ Redis XREAD 短阻塞等新通知（仅作唤醒信号，正文始终来自
PG）→ 无 Redis 或 Redis 异常时退化为 PG 轮询。断线重连带 Last cursor 即可
精确续传。

租户纪律（ARCHITECTURE §9：SSE 是 PEP）：连接时校验 run 归属 actor 的
tenant scope，403 先于任何字节下发。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.runtime_events import RuntimeEventStore
from zhiwei.telemetry.redis_streams import RedisEventStream

logger = logging.getLogger(__name__)

_PG_FALLBACK_POLL_SECONDS = 1.0
_REDIS_BLOCK_MS = 750
_SSE_KEEPALIVE_SECONDS = 15.0


def create_events_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    session_factory_factory: Callable[[], Any],
    tenant_context_factory: Callable[[ActorContext], Any],
    run_exists: Callable[[Any, UUID], Awaitable[bool]],
    redis_stream: RedisEventStream | None = None,
) -> APIRouter:
    """SSE router。

    session_factory_factory：每个连接取自己的 session factory（连接级生命周期）；
    tenant_context_factory：actor → (org, ws) 显式 tenant 上下文；
    run_exists：在 tenant scope 内校验 run 归属（PEP 判定）。
    """
    router = APIRouter(tags=["events"])

    @router.get("/api/v1/runs/{run_id}/stream")
    async def stream_events(
        run_id: UUID,
        request: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
        cursor: str | None = Query(default=None, description="从该 sequence_no 之后续传"),
    ) -> StreamingResponse:
        context = tenant_context_factory(actor)
        if context.workspace_id is None:
            raise HTTPException(status_code=403, detail="workspace context required")
        last_sequence = 0
        if cursor:
            try:
                last_sequence = int(cursor)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid cursor") from exc
            if last_sequence < 0:
                raise HTTPException(status_code=422, detail="invalid cursor")

        sessions = session_factory_factory()
        if not await run_exists(context, run_id):
            # 归属校验先于任何字节：跨租户/不存在的 run 一律 404 语义不区分
            raise HTTPException(status_code=404, detail="run not found in tenant scope")

        async def event_generator() -> AsyncIterator[str]:
            last = last_sequence
            redis_healthy = redis_stream is not None and await redis_stream.ping()
            redis_cursor = "0-0"
            while not await request.is_disconnected():
                # 1. PG 重放：零丢失基线（首连/重连/Redis 丢失统一走这里）。
                # 读路径同样走 tenant 事务（RLS 对无 GUC 会话过滤全部行）。
                from zhiwei.persistence.tenant import tenant_session

                async with tenant_session(sessions, context) as session:
                    store = RuntimeEventStore(session, context)
                    pairs = await store.load_events_with_sequences(run_id)
                for seq, event in pairs:
                    if seq > last:
                        last = seq
                        yield _sse(_event_frame(event), seq)

                # 2. 增量唤醒：Redis 健康时短阻塞等通知；否则退化为 PG 轮询
                woke = False
                if redis_healthy and redis_stream is not None:
                    try:
                        notices = await redis_stream.read_since(
                            str(run_id), redis_cursor, block_ms=_REDIS_BLOCK_MS
                        )
                        for notice in notices:
                            redis_cursor = notice["id"]
                            if int(notice.get("sequence_no") or 0) > last:
                                woke = True
                    except Exception:
                        redis_healthy = False
                        logger.warning("redis unhealthy; SSE falls back to pg polling")
                if redis_healthy and not woke:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(_SSE_KEEPALIVE_SECONDS)
                elif not redis_healthy:
                    await asyncio.sleep(_PG_FALLBACK_POLL_SECONDS)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _event_frame(event: object) -> dict[str, object]:
    from zhiwei.runtime.events import RuntimeEvent

    assert isinstance(event, RuntimeEvent)
    return {
        "event_type": type(event).__name__,
        "event_id": str(event.event_id),
        "run_id": str(event.run_id),
        "task_id": getattr(event, "task_id", None),
    }


def _sse(payload: dict[str, object], sequence: int) -> str:
    return f"id: {sequence}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
