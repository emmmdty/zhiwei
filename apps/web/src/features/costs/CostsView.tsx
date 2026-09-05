// S9-T7 Costs 视图：reservations + reconciliations（api/observability.py
// CostSummary 投影）+ 汇总。
//
// 纪律：金额是字符串化的 Decimal，原样渲染；无法解析为数字的分量（如未对账
// 的 "unknown"）使对应汇总列以 unknown 呈现——不造 placeholder 0、不假装对账
// 已完成。汇总只做加法聚合，不产生任何「成功率」类推断值。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";

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
  reserved_usd: string;
  actual_usd: string;
  variance_usd: string;
  retry_cost_usd: string;
  child_run_cost_usd: string;
  tool_external_cost_usd: string;
  created_at: string;
}

interface CostSummary {
  reservations: CostReservationView[];
  reconciliations: CostReconciliationView[];
}

// 逐行累加；任一分量不是有限数字 → 该列汇总为 unknown（incomplete 原样上浮）
function sumOrUnknown(values: string[]): string {
  let total = 0;
  for (const value of values) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "unknown";
    total += n;
  }
  return total.toFixed(7);
}

export function CostsView({
  onSessionExpired,
}: {
  onSessionExpired: () => Promise<void>;
}) {
  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setSummary(await api.get<CostSummary>("/api/v1/observability/costs"));
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

  return (
    <section aria-label="Costs">
      <h2>Costs</h2>
      {loading ? (
        <div aria-busy="true">Loading costs…</div>
      ) : error ? (
        <div role="alert">Error: {error}</div>
      ) : summary ? (
        <>
          <h3>Reservations</h3>
          <table>
            <thead>
              <tr>
                <th>Reservation</th>
                <th>Run</th>
                <th>Amount (USD)</th>
                <th>Price source</th>
                <th>Confidence</th>
                <th>Created at</th>
              </tr>
            </thead>
            <tbody>
              {summary.reservations.map((r) => (
                <tr key={r.reservation_id}>
                  <td>{r.reservation_id}</td>
                  <td>{r.run_id}</td>
                  <td>{r.amount_usd}</td>
                  <td>{r.price_source}</td>
                  <td>{r.price_confidence}</td>
                  <td>{r.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3>Reconciliations</h3>
          <table>
            <thead>
              <tr>
                <th>Reservation</th>
                <th>Reserved (USD)</th>
                <th>Actual (USD)</th>
                <th>Variance (USD)</th>
                <th>Retry (USD)</th>
                <th>Child run (USD)</th>
                <th>Tool external (USD)</th>
                <th>Created at</th>
              </tr>
            </thead>
            <tbody>
              {summary.reconciliations.map((r) => (
                <tr key={`${r.reservation_id}-${r.created_at}`}>
                  <td>{r.reservation_id}</td>
                  <td>{r.reserved_usd}</td>
                  <td>{r.actual_usd}</td>
                  <td>{r.variance_usd}</td>
                  <td>{r.retry_cost_usd}</td>
                  <td>{r.child_run_cost_usd}</td>
                  <td>{r.tool_external_cost_usd}</td>
                  <td>{r.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            Totals — reserved:{" "}
            {sumOrUnknown(summary.reconciliations.map((r) => r.reserved_usd))}, actual:{" "}
            {sumOrUnknown(summary.reconciliations.map((r) => r.actual_usd))}, variance:{" "}
            {sumOrUnknown(summary.reconciliations.map((r) => r.variance_usd))}
          </p>
        </>
      ) : (
        <p>No cost data</p>
      )}
    </section>
  );
}
