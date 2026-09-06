// S2-T7 Workbench：run 列表 + 创建（经 Planner port 的模板）+ 详情下钻。
// 数据源 = REST PG 投影（GET /api/v1/runs），刷新/断网后可恢复（spec §5）。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { listCreatableTemplates } from "../../state/appTemplates";
import { RunDetailView } from "../runs/RunDetailView";

interface RunRecord {
  run_id: string;
  status: string;
  organization_id: string;
}

interface WorkbenchProps {
  workspaceId: string;
  onSessionExpired: () => Promise<void>;
}

// 模板集 = S2 通用 fixture 模板（运行时语义，非 App 名称）+ 可创建的 App
// pack 模板。App 模板 id 字面量只住在 renderer 注册表（creatable 标志，与后端
// pack_templates.py 同构的注册数据面）；通用层经 state 桥接访问器消费。注册但
// 未标 creatable 的模板（创建期会 422）不进选择器——无后端行为的控件不得
// 出现（spec §5；S10 gate 例外 E3）。
const GENERIC_TEMPLATES = ["single-fixture", "approval-chain"] as const;
const PACK_TEMPLATES = listCreatableTemplates();

export function Workbench({ workspaceId, onSessionExpired }: WorkbenchProps) {
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [template, setTemplate] = useState<string>("single-fixture");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setRuns(await api.get<RunRecord[]>("/api/v1/runs"));
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

  const createRun = async () => {
    setCreating(true);
    setError(null);
    try {
      const created = await api.post<{ run_id: string }>("/api/v1/runs", {
        template,
        workspace_id: workspaceId,
      });
      await load();
      setSelected(created.run_id);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  };

  if (selected) {
    return (
      <RunDetailView
        runId={selected}
        onBack={() => {
          setSelected(null);
          load();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }

  return (
    <section aria-label="Workbench">
      <h2>Workbench</h2>
      <div>
        <label htmlFor="run-template">Template</label>
        <select
          id="run-template"
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
        >
          {[...GENERIC_TEMPLATES, ...PACK_TEMPLATES].map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <button onClick={createRun} disabled={creating}>
          {creating ? "Creating…" : "New run"}
        </button>
      </div>
      {loading ? (
        <StateBanner tone="loading" text="Loading runs…" />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : runs.length === 0 ? (
        <StateBanner tone="empty" text="No active runs" />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Run ID</th>
              <th>Status</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td>{run.run_id}</td>
                <td>{run.status}</td>
                <td>
                  <button onClick={() => setSelected(run.run_id)}>Open</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
