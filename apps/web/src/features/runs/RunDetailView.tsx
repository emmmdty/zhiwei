// S2-T7 Run 详情：任务投影 + 审批队列（REST PG 投影，轮询恢复——spec §5）。
// S10-T1：opt-in live 订阅（默认关——默认 journey 行为不变）经 state/runLive
// 合入 SSE 增量；App 归属经通用 AppRendererSlot 槽位渲染（本文件不含任何
// App 名称条件，features 也不直接 import renderers/——架构契约）。

import { useCallback, useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { AppRendererSlot } from "../../components/AppRendererSlot";
import { StateBanner } from "../../components/StateBanner";
import { useRunLive } from "../../state/runLive";
import { RunEventTimeline } from "../observability/RunEventTimeline";
import { ApprovalsView } from "../approvals/ApprovalsView";

interface TaskState {
  status: string;
  error: string | null;
}

interface RunDetail {
  run_id: string;
  status: string;
  organization_id: string;
  tasks: Record<string, TaskState>;
  // 后端 RunDetail（extra=forbid）暂不投影 run 的规划意图（template/pack）；
  // 字段到位前通用 App 槽位如实渲染 "No app binding"。机制先冻结（S10-T1），
  // 数据由后续任务的 pack registry 供给；e2e mock 显式供给以证明解析路径。
  template?: string;
}

interface RunDetailViewProps {
  runId: string;
  onBack: () => void;
  onSessionExpired: () => Promise<void>;
}

export function RunDetailView({ runId, onBack, onSessionExpired }: RunDetailViewProps) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);

  const load = async () => {
    try {
      setRun(await api.get<RunDetail>(`/api/v1/runs/${runId}`));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  // live 断线 resync：快照整体重取但不触发整页 loading（首载 load 语义不变）
  const resync = useCallback(async () => {
    try {
      setRun(await api.get<RunDetail>(`/api/v1/runs/${runId}`));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
    }
  }, [runId, onSessionExpired]);

  const liveState = useRunLive(runId, live, {
    onResync: resync,
    onUnauthorized: onSessionExpired,
  });

  useEffect(() => {
    load();
  }, [runId]);

  if (loading) return <StateBanner tone="loading" text="Loading run…" />;
  if (error) return <StateBanner tone="error" text={`Error: ${error}`} />;
  if (!run) return <div>Run not found</div>;

  return (
    <section aria-label={`Run ${runId}`}>
      <button onClick={onBack}>Back</button>
      <h2>Run</h2>
      <p>
        Status: {run.status} (ID: {run.run_id})
      </p>
      <div>
        <button onClick={() => setLive((value) => !value)} aria-pressed={live}>
          {live ? "Leave live" : "Go live"}
        </button>
        {/* badge = live 模式开启（订阅 + 自动续传在途）；连接瞬时态单独呈现 */}
        {live && <span>live</span>}
        {live && !liveState.connected && !liveState.error && <span>reconnecting…</span>}
        {liveState.error && <span role="alert">{liveState.error}</span>}
        {/* SSE 帧只含事件元数据（api/events.py）：这里只显示元数据，任务状态
            仍以 REST 投影为准，不从事件名推断状态 */}
        {liveState.lastEvent && (
          <p>
            Last event: #{liveState.lastEvent.sequence} {liveState.lastEvent.eventType}
          </p>
        )}
      </div>
      <h3>Tasks</h3>
      <ul>
        {Object.entries(run.tasks).map(([tid, state]) => (
          <li key={tid}>
            {tid}: {state.status}
            {state.error ? ` — ${state.error}` : ""}
          </li>
        ))}
      </ul>
      <AppRendererSlot
        run={{
          runId: run.run_id,
          status: run.status,
          template: run.template,
          tasks: run.tasks,
        }}
      />
      <ApprovalsView runId={runId} onSessionExpired={onSessionExpired} />
      {/* S9 R2-B trace journey（plan Task 7）：canonical event 时间线（元数据 only） */}
      <RunEventTimeline runId={runId} onSessionExpired={onSessionExpired} />
    </section>
  );
}
