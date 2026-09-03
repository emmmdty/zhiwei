// S2-T7 审批队列（run 内）：列出审批请求 + 决策按钮（decision fail-closed 枚举）。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";

interface ApprovalRecord {
  request_id: string;
  run_id: string;
  task_id: string;
  status: string;
  requester: string;
}

interface ApprovalsViewProps {
  runId: string;
  onSessionExpired: () => Promise<void>;
}

export function ApprovalsView({ runId, onSessionExpired }: ApprovalsViewProps) {
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setApprovals(
        await api.get<ApprovalRecord[]>(`/api/v1/runs/${runId}/approvals`)
      );
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

  const decide = async (requestId: string, decision: "approved" | "rejected") => {
    setError(null);
    try {
      await api.post(`/api/v1/runs/${runId}/approvals/${requestId}/decision`, {
        decision,
        reason: "",
      });
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const pending = approvals.filter((a) => a.status === "pending");

  return (
    <section aria-label="Approvals">
      <h3>Approvals</h3>
      {loading ? (
        <div aria-busy="true">Loading approvals…</div>
      ) : error ? (
        <div role="alert">Error: {error}</div>
      ) : pending.length === 0 ? (
        <p>No pending approvals</p>
      ) : (
        <ul>
          {pending.map((a) => (
            <li key={a.request_id}>
              task {a.task_id} ({a.status}, requester {a.requester}){" "}
              <button onClick={() => decide(a.request_id, "approved")}>
                Approve
              </button>{" "}
              <button onClick={() => decide(a.request_id, "rejected")}>
                Reject
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
