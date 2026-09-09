// S10-T4b：Ask 的 result renderer——经 GET /api/v1/runs/{id}/evidence
// （api/evidence.py，S6 收口补齐的通用投影端点）渲染完成的 run 携带的
// claim/verify/answer 形态。数据纪律：
// - 只渲染 evidence 投影已携带的字段；投影缺席的字段如实呈现（不猜默认）；
// - verified 标注差异是本视图的核心契约（specs/s6 §5）：Fact/Quote 绑定
//   可复算证据（verified-anchored），Inference/Recommendation 只绑定输入
//   （derived，不声称 deterministic verify）；
// - 点击 claim 展开 source locator / canonical value（同一载荷，无二次端点）；
//   verify result 是第一等标注（tamper 反例无需展开即可见），不在折叠面板里；
// manifest 注册（appId/templateId 绑定）在本目录 index.tsx 完成：通用层永不
// 按名字引用本 App。

import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { AskInputRenderer } from "./input";
import type { ViewManifestProps } from "../registry";

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
  snapshot_digest?: string;
}

interface ClaimView {
  claim_ref: string;
  claim_type: string | null;
  verified: boolean | null;
  quote_text: string | null;
  evidence_refs: EvidenceRef[];
  canonical_value: { type?: string; value?: unknown } | null;
}

interface ConflictView {
  field: string;
  values: Record<string, unknown>;
}

interface EvidencePayload {
  run_id: string;
  run_status: string;
  answer_status: string | null;
  claims: ClaimView[];
  verification: Record<string, unknown> | null;
  unknowns: string[];
  clarification: { needed?: boolean; questions?: string[] } | null;
  conflicts: ConflictView[];
}

function annotationClass(claimType: string | null): string {
  if (claimType === "Fact" || claimType === "Quote") return "verified-anchored";
  if (claimType === "Inference" || claimType === "Recommendation") return "derived";
  return "unclassified";
}

function verifyText(verified: boolean | null): string {
  if (verified === true) return "verify: verified";
  if (verified === false) return "verify: verification failed";
  return "verify: not deterministically verified";
}

// source locator 摘要：按 ref 词汇渲染定位字段（specs/s6 §5 点击 Claim 打开）
function locatorText(ref: EvidenceRef): string {
  switch (ref.ref_type) {
    case "QueryReplay":
      return `QueryReplay: ${ref.sql ?? ""} (snapshot ${ref.snapshot_digest ?? "n/a"})`;
    case "DocRef":
      return `DocRef: ${ref.document_uri ?? ""}`;
    case "CodeRef":
      return `CodeRef: ${ref.file_path ?? ""}`;
    case "CellRef":
      return `CellRef: ${ref.table ?? ""}.${ref.column ?? ""}`;
    case "GitHubRef":
      return `GitHubRef: ${ref.repository ?? ""}`;
    case "ApiRef":
      return `ApiRef: ${ref.endpoint ?? ""}`;
    case "PatternRef":
      return `PatternRef: ${ref.pattern_name ?? ""}`;
    default:
      return `Ref: ${ref.ref_type ?? "unknown"}`;
  }
}

export function AskResultRenderer({ run }: ViewManifestProps) {
  const [evidence, setEvidence] = useState<EvidencePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const terminal = ["completed", "failed", "cancelled"].includes(run.status);

  const load = useCallback(async () => {
    try {
      setEvidence(
        await api.get<EvidencePayload>(`/api/v1/runs/${run.runId}/evidence`)
      );
      setError(null);
    } catch (e) {
      // renderer 槽位契约（ViewManifestProps）不含 session handler：401 等
      // 失败在此如实呈现为错误态，会话过期跳转由 shell 层统一处理
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [run.runId]);

  useEffect(() => {
    // 非终态 run 无 evidence 投影可读——不发起请求（投影以终态为契约面）
    if (terminal) load();
  }, [terminal, load]);

  return (
    <section aria-label="Ask app result">
      <AskInputRenderer run={run} />
      {!terminal ? (
        <StateBanner tone="loading" text={`Evidence pending (run ${run.runId}: ${run.status})`} />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : !evidence ? (
        <StateBanner tone="loading" text="Loading evidence…" />
      ) : (
        <div>
          <p>Answer status: {evidence.answer_status ?? "not reported"}</p>
          {(evidence.unknowns.length > 0) && (
            <div>
              <h4>Unknowns</h4>
              <ul>
                {evidence.unknowns.map((unknown) => (
                  <li key={unknown}>{unknown}</li>
                ))}
              </ul>
            </div>
          )}
          {evidence.clarification?.questions && evidence.clarification.questions.length > 0 && (
            <div>
              <h4>Clarification</h4>
              <ul>
                {evidence.clarification.questions.map((question) => (
                  <li key={question}>{question}</li>
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
                  <button
                    aria-expanded={expanded === claim.claim_ref}
                    onClick={() =>
                      setExpanded((current) =>
                        current === claim.claim_ref ? null : claim.claim_ref
                      )
                    }
                  >
                    {claim.claim_ref} — {claim.claim_type ?? "unclassified"} (
                    {annotationClass(claim.claim_type)})
                  </button>
                  {/* verify 状态常显（RED 契约：篡改副本的 Fact 无需展开即区分呈现） */}
                  <p>{verifyText(claim.verified)}</p>
                  {expanded === claim.claim_ref && (
                    <div>
                      {claim.quote_text && <p>quote: {claim.quote_text}</p>}
                      {claim.canonical_value && (
                        <p>
                          canonical value: {claim.canonical_value.type ?? "?"} ={" "}
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
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
          {evidence.verification && (
            <p>
              verification:{" "}
              {evidence.verification.verification_ok === true ? "ok" : "failed"} (
              exit_code {String(evidence.verification.exit_code ?? "n/a")})
            </p>
          )}
          {evidence.conflicts.map((conflict) => (
            <p key={conflict.field}>
              conflict on {conflict.field}: values preserved side by side
            </p>
          ))}
        </div>
      )}
    </section>
  );
}
