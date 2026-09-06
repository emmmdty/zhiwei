// S10-T6 + fix-B：ChangeBrief 的 result renderer——views/result.yaml 声明的
// renderer_ref changeBrief/result。数据路径：GET /api/v1/runs/{id}/evidence
// （api/evidence.py 的通用投影端点，与通用 Evidence 面板同一端点）。
//
// brief artifact 在投影里的出现形态：结构化 claim 载荷——canonical_value
// （或 answer 载荷）携带 verified-brief.yaml 的字段词汇（affected_symbols 为
// 数组是结构判别键：brief 是 evidence 平面上唯一携带该词汇的对象，按结构
// 识别、不按名称猜测）。渲染逐字段对齐 pack schema；投影未携带 brief 时
// 如实渲染 pending——不发明端点、不伪造 brief 内容。
// manifest 注册（appId/templateId 绑定）在本模块完成：composition root 只
// import 一次，通用层永不按名字引用本 App。

import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { registerRenderer, registerRunBinding, type ViewManifestProps } from "../registry";
import { ChangeBriefInputRenderer } from "./input";

// 字段词汇 = solution-packs/change-brief/schemas/verified-brief.yaml（全部可选
// 读取：投影缺席的组如实渲染 "not reported"，不补默认值）。
interface VerifiedBrief {
  affected_symbols?: {
    name?: string;
    kind?: string;
    file_path?: string;
    line_start?: number;
    line_end?: number;
  }[];
  affected_dependencies?: { name?: string; version_constraint?: string; impact?: string }[];
  affected_tests?: { test_id?: string; path?: string; expected_status?: string }[];
  related_prs?: { repository?: string; pr_number?: number }[];
  related_issues?: { repository?: string; issue_number?: number }[];
  related_checks?: { name?: string; status?: string }[];
  risks?: { description?: string; severity?: string }[];
  unknowns?: string[];
  code_refs?: {
    file_path?: string;
    line_start?: number;
    line_end?: number;
    code_digest?: string;
  }[];
  github_refs?: {
    repository?: string;
    commit_sha?: string;
    path?: string;
    line_start?: number;
    line_end?: number;
    pr_number?: number;
  }[];
}

interface EvidenceRef {
  ref_type?: string;
  file_path?: string;
  line_start?: number;
  line_end?: number;
  code_digest?: string;
  snapshot_digest?: string | null;
}

interface ClaimView {
  claim_ref: string;
  claim_type: string | null;
  verified: boolean | null;
  quote_text: string | null;
  evidence_refs: EvidenceRef[];
  canonical_value: Record<string, unknown> | null;
}

interface EvidencePayload {
  run_id: string;
  run_status: string;
  answer: Record<string, unknown>;
  claims: ClaimView[];
  unknowns: string[];
}

// brief 判别：结构化载荷（affected_symbols 为数组）。字符串形态的 claim
// 载荷（opaque claim_ref）不携带 brief——如实视为无 brief。
function isBriefPayload(value: unknown): value is VerifiedBrief {
  return (
    typeof value === "object" &&
    value !== null &&
    Array.isArray((value as { affected_symbols?: unknown }).affected_symbols)
  );
}

function briefFromEvidence(payload: EvidencePayload): VerifiedBrief | null {
  if (isBriefPayload(payload.answer)) return payload.answer;
  const claim = payload.claims.find((c) => isBriefPayload(c.canonical_value));
  return claim && isBriefPayload(claim.canonical_value) ? claim.canonical_value : null;
}

function symbolText(symbol: NonNullable<VerifiedBrief["affected_symbols"]>[number]): string {
  const span =
    typeof symbol.line_start === "number" && typeof symbol.line_end === "number"
      ? `:${symbol.line_start}-${symbol.line_end}`
      : "";
  return `${symbol.name ?? "?"} (${symbol.kind ?? "unknown kind"}) — ${symbol.file_path ?? "?"}${span}`;
}

function githubRefText(ref: NonNullable<VerifiedBrief["github_refs"]>[number]): string {
  // 缺席字段以 n/a 如实呈现——ref 的 commit/PR/path 是证据定位，不猜
  const span =
    typeof ref.line_start === "number" && typeof ref.line_end === "number"
      ? `:${ref.line_start}-${ref.line_end}`
      : "";
  return `GitHubRef: ${ref.repository ?? "?"} commit ${ref.commit_sha ?? "n/a"} pr ${
    ref.pr_number ?? "n/a"
  } path ${ref.path ?? "n/a"}${span}`;
}

function BriefSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4>{title}</h4>
      {children}
    </div>
  );
}

function BriefSections({ brief }: { brief: VerifiedBrief }) {
  return (
    <>
      {brief.affected_symbols ? (
        <BriefSection title="Affected symbols">
          <ul>
            {brief.affected_symbols.map((symbol, index) => (
              <li key={index}>{symbolText(symbol)}</li>
            ))}
          </ul>
        </BriefSection>
      ) : (
        <p>Affected symbols: not reported</p>
      )}
      {brief.affected_dependencies && (
        <BriefSection title="Affected dependencies">
          <ul>
            {brief.affected_dependencies.map((dep, index) => (
              <li key={index}>{dep.name ?? "?"} ({dep.version_constraint ?? "n/a"}, {dep.impact ?? "unknown impact"})</li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.affected_tests && (
        <BriefSection title="Affected tests">
          <ul>
            {brief.affected_tests.map((test, index) => (
              <li key={index}>{test.test_id ?? "?"} (expected {test.expected_status ?? "unknown"})</li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.related_prs && (
        <BriefSection title="Related PRs">
          <ul>
            {brief.related_prs.map((pr, index) => (
              <li key={index}>{pr.repository ?? "?"}#{pr.pr_number ?? "?"}</li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.related_issues && (
        <BriefSection title="Related issues">
          <ul>
            {brief.related_issues.map((issue, index) => (
              <li key={index}>{issue.repository ?? "?"}#{issue.issue_number ?? "?"}</li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.related_checks && (
        <BriefSection title="Related checks">
          <ul>
            {brief.related_checks.map((check, index) => (
              <li key={index}>{check.name ?? "?"}: {check.status ?? "unknown"}</li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.risks && (
        <BriefSection title="Risks">
          <ul>
            {brief.risks.map((risk, index) => (
              <li key={index}>{risk.severity ?? "unknown"}: {risk.description ?? ""}</li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.unknowns && (
        <BriefSection title="Unknowns">
          {/* unknowns 逐字呈现——brief 的诚实边界，不改写不截断 */}
          <ul>
            {brief.unknowns.map((unknown) => (
              <li key={unknown}>{unknown}</li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.code_refs && (
        <BriefSection title="Code refs">
          <ul>
            {brief.code_refs.map((ref, index) => (
              <li key={index}>
                CodeRef: {ref.file_path ?? "?"}:{ref.line_start ?? "?"}-{ref.line_end ?? "?"} (digest{" "}
                {ref.code_digest ?? "n/a"})
              </li>
            ))}
          </ul>
        </BriefSection>
      )}
      {brief.github_refs && (
        <BriefSection title="GitHub refs">
          <ul>
            {brief.github_refs.map((ref, index) => (
              <li key={index}>{githubRefText(ref)}</li>
            ))}
          </ul>
        </BriefSection>
      )}
    </>
  );
}

export function ChangeBriefResultRenderer({ run }: ViewManifestProps) {
  const [evidence, setEvidence] = useState<EvidencePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  const terminal = ["completed", "failed", "cancelled"].includes(run.status);

  const load = useCallback(async () => {
    try {
      setEvidence(await api.get<EvidencePayload>(`/api/v1/runs/${run.runId}/evidence`));
      setError(null);
    } catch (e) {
      // renderer 槽位契约不含 session handler：失败如实呈现为错误态
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [run.runId]);

  useEffect(() => {
    // 非终态 run 无 evidence 投影可读——不发起请求（投影以终态为契约面）
    if (terminal) void load();
  }, [terminal, load]);

  const tasks = Object.entries(run.tasks ?? {});
  const brief = evidence ? briefFromEvidence(evidence) : null;

  return (
    <section aria-label="Verified brief">
      {!terminal ? (
        <StateBanner tone="loading" text={`Brief pending (run ${run.runId}: ${run.status})`} />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : !evidence ? (
        <StateBanner tone="loading" text="Loading evidence…" />
      ) : brief ? (
        <BriefSections brief={brief} />
      ) : (
        <>
          <StateBanner
            tone="empty"
            text={`Verified brief artifact pending (run ${run.runId}: ${run.status})`}
          />
          {/* 投影未携带 brief 时的第二重诚实事实：连 claim 记录都没有。
              本 renderer 只在绑定的 run 上渲染（此时通用 Evidence 面板让位），
              这里的空态措辞与面板一致但永不与其同页重复。 */}
          {evidence.claims.length === 0 && <StateBanner tone="empty" text="No claims" />}
        </>
      )}
      {tasks.length === 0 ? (
        <StateBanner tone="empty" text="No task state available" />
      ) : (
        <ul>
          {tasks.map(([taskId, task]) => (
            <li key={taskId}>
              {taskId}: {task.status}
              {task.error ? ` — ${task.error}` : ""}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// pack run 的 templateId 取 pack_id（与 ask-v1 绑定约定一致）；schema id 指向
// pack 声明的 verified-brief。
// creatable: 后端 pack 模板已可执行（T6 fixture 绑定），可从通用创建面发起。
registerRunBinding({ templateId: "change-brief", appId: "change-brief", creatable: true });
registerRenderer({
  appId: "change-brief",
  inputSchemaId: "verified-brief",
  resultSchemaId: "verified-brief",
  InputRenderer: ChangeBriefInputRenderer,
  ResultRenderer: ChangeBriefResultRenderer,
});
