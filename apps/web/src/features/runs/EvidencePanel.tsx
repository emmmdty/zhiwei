// S10 fix-B（specs/s10 §2）：通用 Evidence 面板——GET /api/v1/runs/{id}/evidence
// （api/evidence.py RunEvidenceView 投影）的 claim/verify/source-locator 读面。
// 数据纪律：
// - 只渲染投影已携带的字段；缺席字段如实呈现，不猜默认、不造二次端点；
// - verify 状态第一等常显（篡改副本无需展开即区分）；
// - 仅对无 App 绑定的 run 渲染（RunDetailView 门控）：绑定 App 的 result
//   renderer 从同一投影渲染自己的 evidence 视图，两处同页重复渲染会互相
//   破坏 e2e 视觉契约（结构分流经 hasAppBinding，无 App 名称条件）；
// - 非终态不发起请求（投影以终态为契约面，App result renderer 同契约），
//   如实渲染 pending，文案与 App 视图的 pending 措辞刻意不同以免同页撞锚。

import { useCallback, useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";

interface EvidenceRef {
  ref_type?: string;
  reproducibility_level?: string;
  sql?: string;
  document_uri?: string;
  file_path?: string;
  line_start?: number;
  line_end?: number;
  endpoint?: string;
  table?: string;
  column?: string;
  repository?: string;
  pattern_name?: string;
  snapshot_digest?: string | null;
  code_digest?: string;
  result_copy_digest?: string;
}

interface ClaimView {
  claim_ref: string;
  claim_type: string | null;
  verified: boolean | null;
  quote_text: string | null;
  evidence_refs: EvidenceRef[];
  canonical_value: { type?: string; value?: unknown } | null;
}

interface EvidencePayload {
  run_id: string;
  run_status: string;
  claims: ClaimView[];
  unknowns: string[];
}

function verifyText(verified: boolean | null): string {
  if (verified === true) return "verify: verified";
  if (verified === false) return "verify: verification failed";
  return "verify: not deterministically verified";
}

// source locator：按 Evidence ref 词汇渲染定位字段（specs/s6 §5 同一词汇），
// digest 字段存在即常显（code/snapshot/copy-frozen digest）。
function locatorText(ref: EvidenceRef): string {
  const spans =
    typeof ref.line_start === "number" && typeof ref.line_end === "number"
      ? `:${ref.line_start}-${ref.line_end}`
      : "";
  const digest =
    ref.code_digest ?? ref.snapshot_digest ?? ref.result_copy_digest ?? null;
  const suffix = digest ? ` (digest ${digest})` : "";
  switch (ref.ref_type) {
    case "QueryReplay":
      return `QueryReplay: ${ref.sql ?? ""}${suffix}`;
    case "DocRef":
      return `DocRef: ${ref.document_uri ?? ""}${suffix}`;
    case "CodeRef":
      return `CodeRef: ${ref.file_path ?? ""}${spans}${suffix}`;
    case "CellRef":
      return `CellRef: ${ref.table ?? ""}.${ref.column ?? ""}${suffix}`;
    case "GitHubRef":
      return `GitHubRef: ${ref.repository ?? ""}${suffix}`;
    case "ApiRef":
      return `ApiRef: ${ref.endpoint ?? ""}${suffix}`;
    case "PatternRef":
      return `PatternRef: ${ref.pattern_name ?? ""}${suffix}`;
    default:
      return `Ref: ${ref.ref_type ?? "unknown"}${suffix}`;
  }
}

interface EvidencePanelProps {
  runId: string;
  runStatus: string;
  onSessionExpired: () => Promise<void>;
}

export function EvidencePanel({ runId, runStatus, onSessionExpired }: EvidencePanelProps) {
  const [evidence, setEvidence] = useState<EvidencePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  const terminal = ["completed", "failed", "cancelled"].includes(runStatus);

  const load = useCallback(async () => {
    try {
      setEvidence(await api.get<EvidencePayload>(`/api/v1/runs/${runId}/evidence`));
      setError(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [runId, onSessionExpired]);

  useEffect(() => {
    if (terminal) void load();
  }, [terminal, load]);

  return (
    <section aria-label="Run evidence" data-panel-state="data">
      <h3>Evidence</h3>
      {!terminal ? (
        <StateBanner
          tone="loading"
          text={`Evidence panel pending — run not terminal (${runStatus})`}
        />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : !evidence ? (
        <StateBanner tone="loading" text="Loading evidence…" />
      ) : (
        <>
          {evidence.unknowns.length > 0 && (
            <div>
              <h4>Unknowns</h4>
              <ul>
                {evidence.unknowns.map((unknown) => (
                  <li key={unknown}>{unknown}</li>
                ))}
              </ul>
            </div>
          )}
          {evidence.claims.length === 0 ? (
            <StateBanner tone="empty" text="No claims" />
          ) : (
            <ul aria-label="Claims">
              {evidence.claims.map((claim) => (
                <li key={claim.claim_ref}>
                  {claim.claim_ref} — {claim.claim_type ?? "unclassified"}
                  <p>{verifyText(claim.verified)}</p>
                  {claim.quote_text && <p>quote: {claim.quote_text}</p>}
                  {claim.canonical_value && (
                    <p>
                      canonical: {claim.canonical_value.type ?? "?"} ={" "}
                      {String(claim.canonical_value.value)}
                    </p>
                  )}
                  {claim.evidence_refs.length > 0 && (
                    <ul aria-label="Source locators">
                      {claim.evidence_refs.map((ref, index) => (
                        <li key={index}>{locatorText(ref)}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
