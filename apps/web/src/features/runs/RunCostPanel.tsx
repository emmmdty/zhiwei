// S10 fix-B（specs/s10 §2）：run-scoped Cost 面板。数据源 = 租户级
// GET /api/v1/observability/costs（api/observability.py CostSummary）：
// reservations 携带 run_id，reconciliations 以 reservation_id 关联。
//
// 取数纪律（不发明 API）：端点没有 run 过滤参数——租户投影即契约面，客户端
// 按 run_id 过滤是对真实响应的诚实派生（租户数据量是投影契约决定的，前端
// 不伪造成「run 端点」）。金额是字符串化 Decimal，原样渲染；汇总只做加法，
// 非数字分量 → unknown（不造 placeholder 0）。403（org.read_audit 缺失）
// 按 server 返回如实呈现，前端不硬判角色。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";

interface CostReservationView {
  reservation_id: string;
  run_id: string;
  amount_usd: string;
  price_source: string;
  price_confidence: string;
  created_at: string;
}

interface CostReconciliationView {
  reservation_id: string;
  variance_usd: string;
}

interface CostSummary {
  reservations: CostReservationView[];
  reconciliations: CostReconciliationView[];
}

// 逐行累加；任一分量不是有限数字 → 汇总为 unknown（incomplete 原样上浮，
// 与 CostsView 同纪律）
function sumOrUnknown(values: string[]): string {
  let total = 0;
  for (const value of values) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "unknown";
    total += n;
  }
  return total.toFixed(7);
}

interface RunCostPanelProps {
  runId: string;
  onSessionExpired: () => Promise<void>;
}

export function RunCostPanel({ runId, onSessionExpired }: RunCostPanelProps) {
  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setSummary(await api.get<CostSummary>("/api/v1/observability/costs"));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    load();
  }, [runId]);

  const reservations = summary?.reservations.filter((r) => r.run_id === runId) ?? [];
  const varianceByReservation = new Map(
    (summary?.reconciliations ?? []).map((r) => [r.reservation_id, r.variance_usd])
  );

  return (
    <section aria-label="Run cost" data-panel-state="data">
      <h3>Cost</h3>
      {error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : !summary ? (
        <StateBanner tone="loading" text="Loading costs…" />
      ) : reservations.length === 0 ? (
        <StateBanner tone="empty" text="No cost reservations for this run" />
      ) : (
        <>
          <table aria-label="Run cost reservations">
            <thead>
              <tr>
                <th>Reservation</th>
                <th>Amount (USD)</th>
                <th>Price source</th>
                <th>Confidence</th>
                <th>Variance (USD)</th>
                <th>Created at</th>
              </tr>
            </thead>
            <tbody>
              {reservations.map((r) => (
                <tr key={r.reservation_id}>
                  <td>{r.reservation_id}</td>
                  <td>{r.amount_usd}</td>
                  <td>{r.price_source}</td>
                  <td>{r.price_confidence}</td>
                  <td>{varianceByReservation.get(r.reservation_id) ?? "—"}</td>
                  <td>{r.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>Run total (USD): {sumOrUnknown(reservations.map((r) => r.amount_usd))}</p>
        </>
      )}
    </section>
  );
}
