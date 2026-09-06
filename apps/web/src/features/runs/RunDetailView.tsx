// S2-T7 Run 详情：任务投影 + 审批队列（REST PG 投影，轮询恢复——spec §5）。
// S10-T1：opt-in live 订阅（默认关——默认 journey 行为不变）经 state/runLive
// 合入 SSE 增量；App 归属经通用 AppRendererSlot 槽位渲染（本文件不含任何
// App 名称条件，features 也不直接 import renderers/——架构契约）。
// S10 fix-B：§2 通用面板结构（Task Graph/Evidence/Cost + Tools/Artifacts/
// Context/Memory 诚实占位）与执行模式 provenance（§6，从 API 派生，缺席
// 如实 unknown）。Evidence 面板只对无 App 绑定的 run 渲染——绑定 App 的
// result renderer 从同一 evidence 投影渲染自己的视图，避免同页重复渲染。

import { useCallback, useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { AppRendererSlot, hasAppBinding } from "../../components/AppRendererSlot";
import { StateBanner } from "../../components/StateBanner";
import { useRunLive } from "../../state/runLive";
import { RunEventTimeline } from "../observability/RunEventTimeline";
import { ApprovalsView } from "../approvals/ApprovalsView";
import { EvidencePanel } from "./EvidencePanel";
import { RunCostPanel } from "./RunCostPanel";
import { PendingPanel } from "./PendingPanel";

interface TaskState {
  status: string;
  error: string | null;
}

interface RunDetail {
  run_id: string;
  status: string;
  organization_id: string;
  tasks: Record<string, TaskState>;
  // FIX-A 后端下发 template（string | null）与执行模式标注 mode（派生自
  // template 列的 fixture 资格事实）；字段缺席/null → 面板如实渲染 unknown
  //（spec §6：mode 从 API 派生，不猜）。
  template?: string | null;
  mode?: string | null;
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
  const [createdCaseId, setCreatedCaseId] = useState<string | null>(null);

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

  // S10-T4b：从完成的 run 创建 Case（api/cases.py；409/404 语义 server-driven，
  // 前端不硬判）。入口 gated on run 终态——非终态 run 无此控件。
  const createCase = async () => {
    setError(null);
    try {
      const created = await api.post<{ id: string }>(`/api/v1/runs/${runId}/cases`, {
        title: `Case for run ${runId.slice(0, 8)}`,
      });
      setCreatedCaseId(created.id);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
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

  const runSummary = {
    runId: run.run_id,
    status: run.status,
    template: run.template,
    tasks: run.tasks,
  };

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
      {/* Execution provenance（§6）：值逐字来自 API（FIX-A RunDetail.mode），
          缺席字段如实 unknown */}
      <section aria-label="Run execution">
        <h3>Execution</h3>
        <p>Template: {run.template ?? "not reported"}</p>
        <p>Execution mode: {run.mode ?? "unknown (not reported by API)"}</p>
      </section>
      {/* Task Graph 面板：run 详情投影只携带任务（无 edges 数据）——任务列表
          如实展示，边投影缺席如实声明，不发明 edges API 也不伪造边 */}
      <section aria-label="Task graph">
        <h3>Task Graph</h3>
        <p>
          Edges are not projected by the run detail API — tasks are shown as a
          flat list.
        </p>
        {Object.keys(run.tasks).length === 0 ? (
          <StateBanner tone="empty" text="No tasks projected" />
        ) : (
          <ul>
            {Object.entries(run.tasks).map(([tid, state]) => (
              <li key={tid}>
                {tid}: {state.status}
                {state.error ? ` — ${state.error}` : ""}
              </li>
            ))}
          </ul>
        )}
      </section>
      {!hasAppBinding(runSummary) && (
        <EvidencePanel runId={run.run_id} runStatus={run.status} onSessionExpired={onSessionExpired} />
      )}
      <RunCostPanel runId={run.run_id} onSessionExpired={onSessionExpired} />
      {/* §2 面板结构的诚实占位：后端投影未落地，只声明将来会展示什么（§5） */}
      <PendingPanel title="Tools" wouldShow="tool invocations and results for this run." />
      <PendingPanel title="Artifacts" wouldShow="artifacts emitted by this run (id, kind, schema)." />
      <PendingPanel title="Context" wouldShow="context inputs bound to this run." />
      <PendingPanel title="Memory" wouldShow="memory reads and candidates for this run." />
      <ApprovalsView runId={runId} onSessionExpired={onSessionExpired} />
      {run.status === "completed" && !createdCaseId && (
        <button onClick={createCase}>Create case</button>
      )}
      {createdCaseId && <p>Case created: {createdCaseId}</p>}
      <AppRendererSlot run={runSummary} />
      {/* S9 R2-B trace journey（plan Task 7）：canonical event 时间线（元数据 only） */}
      <RunEventTimeline runId={runId} onSessionExpired={onSessionExpired} />
    </section>
  );
}
