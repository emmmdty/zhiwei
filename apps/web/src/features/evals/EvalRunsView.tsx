// S9-T7 Eval runs 视图：列表（mode/status/sealed/units）+ 详情下钻。
//
// 后端缺口（显式登记，不静默）：src/zhiwei/api/evals.py 尚不存在。本视图的
// 路径按 specs/s9 §2 的资源名 /api/v1/evals 推导，字段名逐一对齐
// evals/runs.py（RunPhase/SampleStatus/EvalMode）与 persistence/models.py
// EvalRunRow；router 补齐前真实后端 404 → 错误显性呈现（fail loud），不造假数据。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { EvalRunDetailView } from "./EvalRunDetailView";

interface EvalRunListItem {
  eval_run_id: string;
  run_id: string | null;
  mode: string;
  status: string;
  sealed_at: string | null;
  registered_units: number;
}

interface EvalRunsViewProps {
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}

export function EvalRunsView({ readOnly, onSessionExpired }: EvalRunsViewProps) {
  const [runs, setRuns] = useState<EvalRunListItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setRuns(await api.get<EvalRunListItem[]>("/api/v1/evals"));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  if (selected) {
    return (
      <EvalRunDetailView
        evalRunId={selected}
        readOnly={readOnly}
        onBack={() => {
          setSelected(null);
          load();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }

  return (
    <section aria-label="Evals">
      <h2>Evals</h2>
      {loading ? (
        <StateBanner tone="loading" text="Loading eval runs…" />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : runs.length === 0 ? (
        <StateBanner tone="empty" text="No eval runs" />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Eval Run</th>
              <th>Mode</th>
              <th>Status</th>
              <th>Sealed</th>
              <th>Units</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.eval_run_id}>
                <td>{run.eval_run_id}</td>
                <td>{run.mode}</td>
                <td>{run.status}</td>
                {/* 未密封的 run 没有 sealed 事实——unknown 原样展示，不造时间戳 */}
                <td>{run.sealed_at ?? "unknown"}</td>
                <td>{run.registered_units}</td>
                <td>
                  <button onClick={() => setSelected(run.eval_run_id)}>Open</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
