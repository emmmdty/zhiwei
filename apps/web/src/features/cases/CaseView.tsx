// S10-T4b：Case 分区视图——case 列表 + 详情（S6 §4 通用 Case surface 的消费面，
// 非 App 专属）。数据源 = /api/v1/cases（api/cases.py 投影），状态与关联 run
// 从 server projection 逐字渲染；详情在挂载时重取——刷新/断网后可恢复（与
// Workbench 同款恢复语义）。创建入口在 RunDetailView（run 终态 gate），本视图
// 只读呈现生命周期（转移 API 未落地，不提供状态操作控件）。

import { useCallback, useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";

interface CaseRecord {
  id: string;
  run_id: string | null;
  title: string;
  description: string;
  status: string;
  created_at: string;
}

interface CaseViewProps {
  onSessionExpired: () => Promise<void>;
}

export function CaseView({ onSessionExpired }: CaseViewProps) {
  const [items, setItems] = useState<CaseRecord[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<CaseRecord | null>(null);
  const [detailRequests, setDetailRequests] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await api.get<CaseRecord[]>("/api/v1/cases"));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [onSessionExpired]);

  // 详情挂载即重取：server projection 是唯一事实源（刷新恢复语义）
  const loadDetail = useCallback(
    async (caseId: string) => {
      try {
        setDetail(await api.get<CaseRecord>(`/api/v1/cases/${caseId}`));
        setDetailRequests((count) => count + 1);
      } catch (e) {
        if (e instanceof SessionExpiredError) return onSessionExpired();
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [onSessionExpired]
  );

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (selected) loadDetail(selected);
  }, [selected, loadDetail]);

  if (error) return <StateBanner tone="error" text={`Error: ${error}`} />;

  if (selected) {
    return (
      <section aria-label="Case">
        <button
          onClick={() => {
            setSelected(null);
            setDetail(null);
          }}
        >
          Back
        </button>
        <h2>Case</h2>
        {detail ? (
          <div>
            <p>Title: {detail.title}</p>
            <p>Status: {detail.status}</p>
            <p>Description: {detail.description || "—"}</p>
            {detail.run_id && <p>Run: {detail.run_id}</p>}
            <p>Created: {detail.created_at}</p>
            {/* 详情每次挂载都重取 projection；请求计数只用于恢复语义的自证 */}
            <p data-testid="case-detail-fetches">{detailRequests}</p>
          </div>
        ) : (
          <StateBanner tone="loading" text="Loading case…" />
        )}
      </section>
    );
  }

  return (
    <section aria-label="Cases">
      <h2>Cases</h2>
      {items === null ? (
        <StateBanner tone="loading" text="Loading cases…" />
      ) : items.length === 0 ? (
        <StateBanner tone="empty" text="No cases yet" />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Status</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>{item.title}</td>
                <td>{item.status}</td>
                <td>
                  <button onClick={() => setSelected(item.id)}>Open</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
