"""S2-T7：Redis Streams 增量事件通道（disposable UI channel）。

事实源：specs/s2-agent-runtime.md §4（「Redis/SSE 丢失只影响增量；REST projection +
cursor 可恢复」）、总设计 §4.3。

设计：
- 通知粒度是 canonical event 元数据（event_id/run_id/seq/digest），不是事件正文——
  正文始终从 PG 读（唯一真相）；Redis 只加速「有新事件」的发现。
- 每 run 一个 Redis Stream key（`zhiwei:run:{run_id}`）；XADD 由 dispatcher 的
  EventSink 路径触发（canonical.event.committed outbox 消息 → publish）。
- SSE 消费：XREAD 从 last-delivered-id 增量读；Redis 不可用/丢失 → SSE 回退到
  PG 轮询（增量零丢失，只增加延迟）。
- 游标语义：cursor = canonical sequence_no（十进制字符串）。REST 重放与 Redis
  增量共用同一游标空间，按 seq 去重（Redis 通知可能滞后/重复）。
"""

from __future__ import annotations

import redis.asyncio as aioredis

from zhiwei.persistence.outbox import OutboxDelivery

RUN_STREAM_PREFIX = "zhiwei:run:"


def run_stream_key(run_id: str) -> str:
    return f"{RUN_STREAM_PREFIX}{run_id}"


class RedisEventStream:
    """OutboxSink + run-scoped 通知流的 Redis 实现（async）。"""

    def __init__(self, client: aioredis.Redis) -> None:
        self._redis = client

    @classmethod
    async def connect(cls, url: str) -> RedisEventStream:
        return cls(aioredis.from_url(url, decode_responses=True))

    @classmethod
    def connect_lazy(cls, url: str) -> RedisEventStream:
        """组装期工厂：redis-py 的 from_url 惰性建连（首条命令时才连）。"""
        return cls(aioredis.from_url(url, decode_responses=True))

    async def close(self) -> None:
        await self._redis.aclose()

    # ------------------------------------------------------------------ OutboxSink

    async def publish(self, message: OutboxDelivery) -> None:
        """dispatcher 的 canonical.event.committed 消息 → run 流通知。

        幂等：event_id 进流前按 seq 去重由消费端处理（Redis 侧不做 CAS——
        通知重复无害，通知丢失由 PG 轮询兜底）。
        """
        run_id = str(message.payload.get("run_id", ""))
        if not run_id:
            return
        await self._redis.xadd(
            run_stream_key(run_id),
            {
                "event_id": str(message.payload.get("event_id", message.id)),
                "run_id": run_id,
                "sequence_no": str(message.payload.get("sequence_no", "")),
                "event_digest": str(message.payload.get("event_digest", "")),
            },
        )

    # ------------------------------------------------------------------ 消费端

    async def read_since(
        self,
        run_id: str,
        last_id: str,
        *,
        count: int = 100,
        block_ms: int | None = None,
    ) -> list[dict[str, str]]:
        """读取 run 流中 last_id 之后的通知（Redis stream id 空间）。"""
        key = run_stream_key(run_id)
        if block_ms is None:
            # XRANGE 的 min 是闭区间；"(" 前缀（Redis ≥6.2）取独占下界
            exclusive_min = last_id if last_id.startswith("(") else f"({last_id}"
            entries = await self._redis.xrange(key, min=exclusive_min, max="+", count=count)
        else:
            if last_id == "0":
                last_id = "0-0"
            response = await self._redis.xread(
                {key: last_id}, count=count, block=block_ms
            )
            entries = response[0][1] if response else []
        return [
            {"id": entry_id, **fields}
            for entry_id, fields in entries
        ]

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False
