// S9-T7 Eval run 详情：scope 标签 + 完整分母状态分解 + Wilson CI + resume/seal/report。
//
// 纪律：
// - 报告（evals/reports.py EvalReportArtifact）在场时，scope 标签（mode 只能来自
//   密封载荷）与分母/CI 以报告为权威；报告缺席 → unknown 原样展示，不造 placeholder。
// - 报告必须经 GET /report 以显式 scope 查询参数现取（api/evals.py：model/version/
//   date/corpus/environment 五参数全必填，缺参 422；未密封/构建被拒 409 携带
//   机器可读 reason）。"Load report" 只打开 scope 输入行，不猜测任何默认值；
//   422/409 的机器可读拒绝面原样上浮在报告区内联。
// - 样本 outcome 只渲染 status + result_digest（metadata only）——result 正文
//   （prompt/completion）可能含敏感内容，永不进 DOM。
// - resume/seal 的状态机门禁只管 UI 入口；sealed 无出边等语义由 server 强制。

import { useEffect, useState } from "react";
import { ApiError, api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { ConfirmButton } from "../../components/ConfirmButton";

interface RegisteredUnit {
  sample_id: string;
  unit_id: string;
}

interface OutcomeRow {
  unit: RegisteredUnit;
  status: string;
  result_digest: string | null;
  result: unknown;
}

interface ReportScope {
  mode: string;
  model: string;
  version: string;
  date: string;
  corpus: string;
  environment: string;
}

interface QualityEntry {
  label: string;
  n: number;
  successes: number;
  estimate: number;
  ci_low: number;
  ci_high: number;
  denominator: {
    n_total: number;
    n_completed: number;
    n_failed: number;
    n_refused: number;
    n_error: number;
  };
}

interface ReportArtifact {
  schema_id: string;
  schema_version: number;
  generated_from: Record<string, string>;
  scope: ReportScope;
  quality: QualityEntry[];
  paired_comparison: unknown;
}

interface EvalRunDetail {
  eval_run_id: string;
  run_id: string | null;
  mode: string;
  status: string;
  sealed_at: string | null;
  registered_units: RegisteredUnit[];
  outcomes: OutcomeRow[];
  report: ReportArtifact | null;
}

const ERROR_TEXT = "Something went wrong";

// GET /report 的显式 scope 查询参数（api/evals.py get_eval_run_report，全必填）。
// mode 不在其中：mode 只能来自密封载荷，不可声明。
type ScopeField = "model" | "version" | "date" | "corpus" | "environment";
const SCOPE_FIELDS: { name: ScopeField; label: string }[] = [
  { name: "model", label: "model" },
  { name: "version", label: "version" },
  { name: "date", label: "date" },
  { name: "corpus", label: "corpus" },
  { name: "environment", label: "environment" },
];
const EMPTY_SCOPE: Record<ScopeField, string> = {
  model: "",
  version: "",
  date: "",
  corpus: "",
  environment: "",
};

// 422/409 的机器可读拒绝面原样上浮：409 detail 是 {reason, message}，422 是
// FastAPI 校验错误数组——保留 status 与逐字 detail，不退化成模糊文案。
function formatReportError(error: unknown): string {
  if (error instanceof ApiError) {
    return `report refused (${error.status}): ${error.detail}`;
  }
  return error instanceof Error ? error.message : String(error);
}

interface EvalRunDetailViewProps {
  evalRunId: string;
  readOnly: boolean;
  onBack: () => void;
  onSessionExpired: () => Promise<void>;
}

export function EvalRunDetailView({
  evalRunId,
  readOnly,
  onBack,
  onSessionExpired,
}: EvalRunDetailViewProps) {
  const [run, setRun] = useState<EvalRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  const [scopeOpen, setScopeOpen] = useState(false);
  const [scope, setScope] = useState<Record<ScopeField, string>>(EMPTY_SCOPE);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);

  const apply = (next: EvalRunDetail) => {
    setRun(next);
    setScopeOpen(false);
    setReportError(null);
  };

  const load = async () => {
    try {
      setRun(await api.get<EvalRunDetail>(`/api/v1/evals/${evalRunId}`));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [evalRunId]);

  const act = async (action: "resume" | "seal") => {
    setActing(true);
    setError(null);
    try {
      apply(
        await api.post<EvalRunDetail>(`/api/v1/evals/${evalRunId}/${action}`)
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActing(false);
    }
  };

  const fetchReport = async () => {
    setReportLoading(true);
    setReportError(null);
    try {
      // scope 五参数由调用方显式提供；空字段省略（不代填默认值），缺参由
      // 服务端 422 拒绝——机器可读拒绝面在报告区内联上浮。
      const params = new URLSearchParams();
      for (const { name } of SCOPE_FIELDS) {
        const value = scope[name].trim();
        if (value) params.set(name, value);
      }
      const query = params.toString();
      const report = await api.get<ReportArtifact>(
        `/api/v1/evals/${evalRunId}/report${query ? `?${query}` : ""}`
      );
      setRun((prev) => (prev ? { ...prev, report } : prev));
      setScopeOpen(false);
      setScope(EMPTY_SCOPE);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setReportError(formatReportError(e));
    } finally {
      setReportLoading(false);
    }
  };

  if (loading) return <StateBanner tone="loading" text="Loading eval run…" />;
  if (error && !run) return <StateBanner tone="error" text={`Error: ${error}`} />;
  if (!run) return <div>Eval run not found</div>;

  const report = run.report;
  // 分解的权威来源：密封报告的完整分母；无报告时从真实 outcome 派生（只列
  // 实际出现的状态——0 计数的状态不渲染，避免读成「验证过为零」）。
  const denominator = report?.quality[0]?.denominator;
  const derived = run.outcomes.reduce<Record<string, number>>((acc, o) => {
    acc[o.status] = (acc[o.status] ?? 0) + 1;
    return acc;
  }, {});
  const outcomeByUnit = new Map(
    run.outcomes.map((o) => [`${o.unit.sample_id}/${o.unit.unit_id}`, o])
  );

  return (
    <section aria-label={`Eval run ${evalRunId}`}>
      <button onClick={onBack}>Back</button>
      <h2>Eval run</h2>
      <p>
        Status: {run.status} (ID: {run.eval_run_id})
      </p>
      <p>Mode: {run.mode}</p>
      <p>Sealed at: {run.sealed_at ?? "unknown"}</p>
      {error && (
        <div role="alert">
          {ERROR_TEXT}: {error}
        </div>
      )}

      <h3>Scope</h3>
      <ul>
        {/* scope 是密封报告的声明标签；无报告 = 无可声明事实 → unknown */}
        <li>mode: {report?.scope.mode ?? "unknown"}</li>
        <li>model: {report?.scope.model ?? "unknown"}</li>
        <li>version: {report?.scope.version ?? "unknown"}</li>
        <li>date: {report?.scope.date ?? "unknown"}</li>
        <li>corpus: {report?.scope.corpus ?? "unknown"}</li>
        <li>environment: {report?.scope.environment ?? "unknown"}</li>
      </ul>

      <h3>Outcomes</h3>
      <ul>
        {run.registered_units.map((u) => {
          const key = `${u.sample_id}/${u.unit_id}`;
          const outcome = outcomeByUnit.get(key);
          return (
            <li key={key}>
              {key}: {outcome ? outcome.status : "no outcome"}
              {outcome?.result_digest ? ` — ${outcome.result_digest}` : ""}
            </li>
          );
        })}
      </ul>

      <h3>Status breakdown</h3>
      <ul>
        {denominator ? (
          <>
            <li>completed: {denominator.n_completed}</li>
            <li>failed: {denominator.n_failed}</li>
            <li>refused: {denominator.n_refused}</li>
            <li>error: {denominator.n_error}</li>
            <li>n_total: {denominator.n_total}</li>
          </>
        ) : (
          <>
            {Object.entries(derived).map(([status, count]) => (
              <li key={status}>
                {status}: {count}
              </li>
            ))}
            <li>n_total: {run.registered_units.length}</li>
          </>
        )}
      </ul>

      <h3>Quality</h3>
      {report ? (
        report.quality.map((entry) => (
          <p key={entry.label}>
            {entry.label}: estimate {entry.estimate}, CI [{entry.ci_low}, {entry.ci_high}]
          </p>
        ))
      ) : (
        <p>Quality: unknown</p>
      )}
      {reportError && (
        <div role="alert" aria-label="Report error">
          {reportError}
        </div>
      )}
      {scopeOpen && (
        <fieldset aria-label="Report scope">
          <legend>Report scope</legend>
          {/* mode 只能来自密封载荷，不可声明：预填自 run 详情，仅作展示 */}
          <p>mode (from run): {run.mode}</p>
          {SCOPE_FIELDS.map(({ name, label }) => (
            <label key={name} htmlFor={`report-scope-${name}`}>
              {label}
              <input
                id={`report-scope-${name}`}
                name={name}
                value={scope[name]}
                onChange={(e) => setScope((prev) => ({ ...prev, [name]: e.target.value }))}
              />
            </label>
          ))}
          <button onClick={fetchReport} disabled={reportLoading}>
            Fetch report
          </button>
        </fieldset>
      )}

      <h3>Actions</h3>
      <div>
        <ConfirmButton
          label="Resume"
          confirmLabel="Confirm resume"
          disabled={readOnly || acting || run.status !== "partial"}
          confirmDisabled={acting}
          onConfirm={() => void act("resume")}
        />
        <ConfirmButton
          label="Seal"
          confirmLabel="Confirm seal"
          disabled={readOnly || acting || run.status === "sealed"}
          confirmDisabled={acting}
          onConfirm={() => void act("seal")}
        />
        <button
          onClick={() => {
            setScopeOpen((open) => !open);
            setReportError(null);
          }}
          disabled={readOnly || reportLoading}
          aria-expanded={scopeOpen}
        >
          Load report
        </button>
      </div>
    </section>
  );
}
