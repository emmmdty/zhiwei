// S10-T1：SSE 客户端（fetch-stream + cursor 续传）——server/live/draft 状态
// 边界的 live 腿。
//
// 事实源：src/zhiwei/api/events.py——帧形状 `id: <sequence_no>` + data:
// {event_type,event_id,run_id,task_id}；keepalive 是 ": keepalive" 注释帧；
// 游标 = canonical sequence_no（十进制），断线重连带 Last cursor 即可精确续传。
//
// 为什么不用 EventSource：其自动重连固定复用首连 URL，无法在重连时改写
// ?cursor——游标只能前进的续传语义做不到。自管重连循环：流结束/网络错误 →
// 记住最后收到的 sequence → 带 ?cursor 重连。fail-closed：非 200 一律停止并
// 上报（403/404 重试无意义，500 重试同样可能hammering），由调用方显式呈现。

export interface SseLiveEvent {
  sequence: number;
  eventType: string;
  eventId: string;
  runId: string;
  taskId: string | null;
}

export interface SseStreamOptions {
  runId: string;
  // 首连游标（重连场景由内部 lastSequence 接管，调用方只管首连）
  cursor?: number;
  onConnect?: () => void;
  onEvent: (event: SseLiveEvent) => void;
  // 连接断开（流结束/网络错误）——重连前回调；调用方借此重取 REST 快照
  onDisconnect?: () => void;
  // 不可恢复的响应状态（非 200）——循环终止
  onError?: (status: number) => void;
  reconnectDelayMs?: number;
}

export interface SseStreamHandle {
  close: () => void;
}

const DEFAULT_RECONNECT_DELAY_MS = 250;

export function createSseStream(options: SseStreamOptions): SseStreamHandle {
  let closed = false;
  let lastSequence = options.cursor ?? 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const controller = new AbortController();

  const scheduleReconnect = () => {
    if (closed) return;
    timer = setTimeout(() => {
      void connect();
    }, options.reconnectDelayMs ?? DEFAULT_RECONNECT_DELAY_MS);
  };

  const connect = async (): Promise<void> => {
    if (closed) return;
    const query = lastSequence > 0 ? `?cursor=${lastSequence}` : "";
    let response: Response;
    try {
      response = await fetch(`/api/v1/runs/${options.runId}/stream${query}`, {
        headers: { Accept: "text/event-stream" },
        credentials: "include",
        signal: controller.signal,
      });
    } catch {
      // 网络错误/中断：原地重连（最后 sequence 不变 → 零丢失续传）
      if (!closed) scheduleReconnect();
      return;
    }
    if (response.status !== 200 || !response.body) {
      closed = true;
      options.onError?.(response.status);
      return;
    }
    options.onConnect?.();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        for (;;) {
          const boundary = buffer.indexOf("\n\n");
          if (boundary === -1) break;
          const event = parseFrame(buffer.slice(0, boundary));
          if (event) {
            lastSequence = Math.max(lastSequence, event.sequence);
            options.onEvent(event);
          }
          buffer = buffer.slice(boundary + 2);
        }
      }
    } catch {
      // reader 中断（close/abort/网络）——落到统一断线处理
    }
    if (!closed) {
      options.onDisconnect?.();
      scheduleReconnect();
    }
  };

  void connect();

  return {
    close: () => {
      closed = true;
      if (timer !== null) clearTimeout(timer);
      controller.abort();
    },
  };
}

// SSE 帧解析（lite）：注释行（keepalive）跳过；id/data 各取其一；帧不完整或
// data 非法 JSON → 丢弃该帧，不猜测、不抛错（消费方状态不被污染）。
function parseFrame(block: string): SseLiveEvent | null {
  let sequence: number | null = null;
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("id:")) {
      const parsed = Number(line.slice(3).trim());
      if (Number.isFinite(parsed)) sequence = parsed;
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (sequence === null || dataLines.length === 0) return null;
  try {
    const payload = JSON.parse(dataLines.join("\n")) as {
      event_type?: unknown;
      event_id?: unknown;
      run_id?: unknown;
      task_id?: unknown;
    };
    if (
      typeof payload.event_type !== "string" ||
      typeof payload.event_id !== "string" ||
      typeof payload.run_id !== "string"
    ) {
      return null;
    }
    return {
      sequence,
      eventType: payload.event_type,
      eventId: payload.event_id,
      runId: payload.run_id,
      taskId: typeof payload.task_id === "string" ? payload.task_id : null,
    };
  } catch {
    return null;
  }
}
