// S2-T7 Run 详情：任务投影 + 审批队列（REST PG 投影，轮询恢复——spec §5）。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
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

  useEffect(() => {
    load();
  }, [runId]);

  if (loading) return <div aria-busy="true">Loading run…</div>;
  if (error) return <div role="alert">Error: {error}</div>;
  if (!run) return <div>Run not found</div>;

  return (
    <section aria-label={`Run ${runId}`}>
      <button onClick={onBack}>Back</button>
      <h2>Run</h2>
      <p>
        Status: {run.status} (ID: {run.run_id})
      </p>
      <h3>Tasks</h3>
      <ul>
        {Object.entries(run.tasks).map(([tid, state]) => (
          <li key={tid}>
            {tid}: {state.status}
            {state.error ? ` — ${state.error}` : ""}
          </li>
        ))}
      </ul>
      <ApprovalsView runId={runId} onSessionExpired={onSessionExpired} />
      {/* S9 R2-B trace journey（plan Task 7）：canonical event 时间线（元数据 only） */}
      <RunEventTimeline runId={runId} onSessionExpired={onSessionExpired} />
    </section>
  );
}
