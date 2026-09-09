// S9 R2-B（plan Task 7）：run 的 canonical event 时间线。
// 数据源 = 既有 GET /api/v1/runs/{id}/events（api/runs.py get_run_events 投影，
// 从 PG canonical events 读取）——不发明端点，不从字符串日志猜状态。
// 元数据纪律（specs/s9 §6）：只渲染 sequence / 机器事件名（逐字）/ event_id 前缀 /
// task ref；事件正文永不渲染（生产投影不含正文；mock 若超量供给正文，canary
// 必须全程不可见——e2e trace journey 断言）。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";

interface RunEventRow {
  sequence_no: number;
  event_type: string;
  event_id: string;
  task_id: string | null;
}

interface RunEventTimelineProps {
  runId: string;
  onSessionExpired: () => Promise<void>;
}

// 事实源：src/zhiwei/telemetry/traces.py SpanNames（冻结 span 名常量，specs/s9 §6
// 全部域）。此映射仅作显示标签（view concern），镜像 span 域常量；不得据此推导
// 状态或业务逻辑（「不从字符串日志猜状态」）。未知事件类型显示 unknown domain。
const SPAN_LABELS: Readonly<Record<string, string>> = {
  RunCreated: "zhiwei.run",
  RunStarted: "zhiwei.run",
  RunCompleted: "zhiwei.run",
  RunFailed: "zhiwei.run",
  RunCancelled: "zhiwei.run",
  RunPaused: "zhiwei.run",
  RunResumed: "zhiwei.run",
  TaskScheduled: "zhiwei.task",
  TaskStarted: "zhiwei.task",
  TaskCompleted: "zhiwei.task",
  TaskFailed: "zhiwei.task",
  TaskSkipped: "zhiwei.task",
  AttemptCreated: "zhiwei.task",
  AttemptCommitted: "zhiwei.task",
  AttemptAborted: "zhiwei.task",
  ConflictDetected: "zhiwei.run",
};

// event_id 是事件的 canonical 内容寻址标识；时间线只展示前缀（digest prefix）。
function digestPrefix(eventId: string): string {
  return eventId.slice(0, 8);
}

export function RunEventTimeline({ runId, onSessionExpired }: RunEventTimelineProps) {
  const [events, setEvents] = useState<RunEventRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setEvents(await api.get<RunEventRow[]>(`/api/v1/runs/${runId}/events`));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    load();
  }, [runId]);

  if (error) return <StateBanner tone="error" text={`Error: ${error}`} />;
  if (!events) return <StateBanner tone="loading" text="Loading events…" />;

  return (
    <section aria-label="Run events">
      <h3>Canonical events</h3>
      {events.length === 0 ? (
        <StateBanner tone="empty" text="No canonical events" />
      ) : (
        <table aria-label="Run event timeline">
          <thead>
            <tr>
              <th>Seq</th>
              <th>Event type</th>
              <th>Digest</th>
              <th>Task</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={`${event.sequence_no}-${event.event_id}`}>
                <td>{event.sequence_no}</td>
                <td>
                  <span>{event.event_type}</span>{" "}
                  <span>
                    ({SPAN_LABELS[event.event_type] ?? "unknown domain"})
                  </span>
                </td>
                <td>{digestPrefix(event.event_id)}</td>
                <td>{event.task_id ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
