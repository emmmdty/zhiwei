// S10-T1：live 订阅 hook——SSE 增量合入 REST 快照（server/live 状态边界）。
//
// 纪律：SSE 帧只含事件元数据（api/events.py _event_frame），不含任务状态——
// 合入动作只能追加「最后收到的事件」元数据；任务状态的事实源始终是 REST PG
// 投影，断线 resync 时整体重取，不从事件名推断状态。
// 回调进 ref：调用方每次渲染的新函数引用不得触发重连（effect 只依赖
// runId/enabled）。

import { useEffect, useRef, useState } from "react";
import { createSseStream, type SseLiveEvent } from "./sse";

export interface RunLiveState {
  connected: boolean;
  lastEvent: SseLiveEvent | null;
  eventCount: number;
  error: string | null;
}

export interface RunLiveOptions {
  // 断线重连前的 REST 快照重建（零丢失基线：投影为准，增量只作提示）
  onResync: () => void;
  // 流端点 401 → 会话过期路径与 REST 一致
  onUnauthorized?: () => void;
}

export function useRunLive(
  runId: string,
  enabled: boolean,
  options: RunLiveOptions
): RunLiveState {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<SseLiveEvent | null>(null);
  const [eventCount, setEventCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const resyncRef = useRef(options.onResync);
  const unauthorizedRef = useRef(options.onUnauthorized);
  resyncRef.current = options.onResync;
  unauthorizedRef.current = options.onUnauthorized;

  useEffect(() => {
    if (!enabled) return;
    let closed = false;
    setConnected(false);
    setEventCount(0);
    setLastEvent(null);
    setError(null);
    const handle = createSseStream({
      runId,
      onConnect: () => {
        if (!closed) setConnected(true);
      },
      onEvent: (event) => {
        if (closed) return;
        setLastEvent(event);
        setEventCount((count) => count + 1);
      },
      onDisconnect: () => {
        if (closed) return;
        setConnected(false);
        resyncRef.current();
      },
      onError: (status) => {
        if (closed) return;
        setConnected(false);
        setError(`live stream refused (${status})`);
        if (status === 401) unauthorizedRef.current?.();
      },
    });
    return () => {
      closed = true;
      handle.close();
    };
  }, [runId, enabled]);

  return { connected, lastEvent, eventCount, error };
}
