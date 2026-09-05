// S9-T7 Observability 视图：failure taxonomy（封闭 machine code 清单）+
// claim registry 元数据。
//
// 纪律：
// - taxonomy 来自 /api/v1/observability/failures 的封闭契约（telemetry/failures.py
//   FailureCode），不从日志字符串猜状态。
// - claim 只渲染 status/bound_value/seal_digest 等元数据（api/claims.py
//   ClaimView）；本阶段无 release checker findings 端点、无 traces 端点——按
//   任务纪律跳过，不发明端点。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";

interface FailureTaxonomy {
  codes: { code: string }[];
}

interface ClaimView {
  claim_id: string;
  statement: string;
  scope: Record<string, string>;
  status: string;
  bound_value: string | null;
  evidence: {
    eval_run_id: string;
    seal_digest: string;
    artifact_manifest_id: string;
    mode: string;
  } | null;
}

export function ObservabilityView({
  onSessionExpired,
}: {
  onSessionExpired: () => Promise<void>;
}) {
  const [taxonomy, setTaxonomy] = useState<FailureTaxonomy | null>(null);
  const [claims, setClaims] = useState<ClaimView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const [failures, claimList] = await Promise.all([
        api.get<FailureTaxonomy>("/api/v1/observability/failures"),
        api.get<ClaimView[]>("/api/v1/claims"),
      ]);
      setTaxonomy(failures);
      setClaims(claimList);
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
    <section aria-label="Observability">
      <h2>Observability</h2>
      {loading ? (
        <div aria-busy="true">Loading observability…</div>
      ) : error ? (
        <div role="alert">Error: {error}</div>
      ) : (
        <>
          <h3>Failure taxonomy</h3>
          <ul>
            {taxonomy?.codes.map((item) => <li key={item.code}>{item.code}</li>)}
          </ul>
          <h3>Claims</h3>
          {claims.length === 0 ? (
            <p>No claims</p>
          ) : (
            <ul>
              {claims.map((claim) => (
                <li key={claim.claim_id}>
                  {claim.claim_id}: {claim.status} — bound value:{" "}
                  {claim.bound_value ?? "unknown"}
                  {claim.evidence ? ` — seal: ${claim.evidence.seal_digest}` : ""}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
