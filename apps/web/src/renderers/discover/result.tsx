// S10-T4c：Discover 的 result renderer——S8 Workbench journey 的消费面
//（specs/s8-discover-actions.md §6：Feed/Triage → Case → gated action →
// HumanResolution；handoff s8-discover-case-action-e2e-exception 解锁条件）。
//
// 数据纪律：
// - 只渲染 api/discover.py 投影已携带的字段（status/owner/score/severity/
//   evidence 计数/freshness/dedupe 逐字）；score 是域名词（启发式分值），
//   本视图不引入任何 probability 标注；
// - triage/action/resolution 控件对已认证用户常显：权限由 server PEP 强制，
//   403/409 由 API 实际返回驱动呈现（前端不硬判角色）；
// - action 提交的 409 拒绝 detail 逐字渲染（server-driven 门禁——高风险动作
//   不默认执行）；提交后投影重取呈现 pending_approval 状态；
// - 刷新恢复：feed 与 case 详情在挂载/重开时从 server projection 重取
//   （请求计数自证恢复语义，CaseView 同款）；
// - 刷新/重试不复制：同 hypothesis 重复建 case、同内容重复提交 action、
//   已终态 case 重复 resolution 都由服务端 409 拒绝，本视图如实呈现错误。

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../lib/api";
import { useSession } from "../../lib/session";
import { StateBanner } from "../../components/StateBanner";
import { DiscoverInputRenderer } from "./input";
import type { ViewManifestProps } from "../registry";

// api/discover.py FeedHypothesisView 形状（机器字段逐字）
interface FeedHypothesis {
  id: string;
  title: string;
  description: string;
  status: string;
  owner: string;
  kind: string;
  severity: string;
  score: number | null;
  supporting_count: number;
  contradicting_count: number;
  missing_count: number;
  freshness_hours: number;
  dedup_key: string;
  suggested_validation_actions: string[];
  case_id: string | null;
  created_at: string;
  updated_at: string;
}

interface ActionView {
  id: string;
  case_id: string;
  hypothesis_id: string;
  action_type: string;
  tool_name: string;
  parameters: Record<string, unknown>;
  rationale: string;
  requested_by: string;
  status: string;
  s2_decision_id: string | null;
  approved_by: string | null;
  approval_timestamp: string | null;
  created_at: string;
}

interface ResolutionView {
  id: string;
  case_id: string;
  hypothesis_id: string;
  kind: string;
  rationale: string;
  resolved_by: string;
  approved_by: string;
  notes: string;
  evidence_refs: string[];
  approval_timestamp: string;
  created_at: string;
}

interface CaseDetail {
  id: string;
  hypothesis_id: string;
  hypothesis_ids: string[];
  title: string;
  description: string;
  status: string;
  severity: string;
  owner: string;
  dedup_key: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  actions: ActionView[];
  resolutions: ResolutionView[];
}

// 与域模型词汇一致（src/zhiwei/discover/actions.py ActionType / resolutions.py
// ResolutionKind）——不发明服务端不接受的取值
const ACTION_TYPES = ["query", "create", "modify", "delete", "notify", "export"] as const;
const RESOLUTION_KINDS = ["accepted", "dismissed", "false_positive", "mitigated"] as const;

function triageActions(
  status: string
): { label: string; toStatus: string; withOwner: boolean }[] {
  switch (status) {
    case "ready_for_triage":
      return [{ label: "Claim triage", toStatus: "in_triage", withOwner: true }];
    case "in_triage":
      return [
        { label: "Accept", toStatus: "accepted", withOwner: false },
        { label: "Dismiss", toStatus: "dismissed", withOwner: false },
      ];
    case "dismissed":
      return [{ label: "Reopen", toStatus: "in_triage", withOwner: true }];
    default:
      return [];
  }
}

export function DiscoverResultRenderer({ run }: ViewManifestProps) {
  const { state: sessionState } = useSession();
  const [feed, setFeed] = useState<FeedHypothesis[] | null>(null);
  const [feedFetches, setFeedFetches] = useState(0);
  const [openCaseId, setOpenCaseId] = useState<string | null>(null);
  const [caseDetail, setCaseDetail] = useState<CaseDetail | null>(null);
  const [caseFetches, setCaseFetches] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [actionType, setActionType] = useState<string>("modify");
  const [toolName, setToolName] = useState("");
  const [actionRationale, setActionRationale] = useState("");
  const [resolutionKind, setResolutionKind] = useState<string>("accepted");
  const [resolutionRationale, setResolutionRationale] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);

  // feed 是租户作用域 workbench 投影（不绑定 run 终态）；挂载即取，刷新恢复
  const loadFeed = useCallback(async () => {
    try {
      setFeed(await api.get<FeedHypothesis[]>("/api/v1/discover/feed"));
      setFeedFetches((count) => count + 1);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadCase = useCallback(async (caseId: string) => {
    try {
      setCaseDetail(await api.get<CaseDetail>(`/api/v1/discover/cases/${caseId}`));
      setCaseFetches((count) => count + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    loadFeed();
  }, [loadFeed]);

  useEffect(() => {
    if (openCaseId) loadCase(openCaseId);
  }, [openCaseId, loadCase]);

  const triage = async (hypothesis: FeedHypothesis, toStatus: string, withOwner: boolean) => {
    setError(null);
    try {
      // claim/reopen 写 owner（当前 principal）；accept/dismiss 不改写 owner
      const body: Record<string, string> = { status: toStatus };
      if (withOwner && sessionState.status === "authenticated") {
        body.owner = sessionState.user.principal_id;
      }
      await api.post(
        `/api/v1/discover/hypotheses/${hypothesis.id}/triage`,
        body
      );
      await loadFeed();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const createCase = async (hypothesis: FeedHypothesis) => {
    setError(null);
    setRefusal(null);
    try {
      await api.post(`/api/v1/discover/hypotheses/${hypothesis.id}/cases`, {});
      await loadFeed();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const submitAction = async () => {
    if (!caseDetail) return;
    setError(null);
    setRefusal(null);
    try {
      await api.post(`/api/v1/discover/cases/${caseDetail.id}/actions`, {
        action_type: actionType,
        tool_name: toolName,
        rationale: actionRationale,
      });
      // 提交成功路径不存在（服务端门禁恒 409）——防御性重取
      await loadCase(caseDetail.id);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // server-driven 门禁：request 已落账（pending_approval），执行被拒绝。
        // 拒绝文本逐字呈现 + 投影重取呈现 pending 状态。
        setRefusal(e.detail);
        await loadCase(caseDetail.id);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const approveAction = async (action: ActionView) => {
    if (!caseDetail) return;
    setError(null);
    setRefusal(null);
    try {
      await api.post(`/api/v1/discover/actions/${action.id}/approve`);
      await loadCase(caseDetail.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const recordResolution = async () => {
    if (!caseDetail) return;
    setError(null);
    setRefusal(null);
    try {
      await api.post(`/api/v1/discover/cases/${caseDetail.id}/resolutions`, {
        kind: resolutionKind,
        rationale: resolutionRationale,
      });
      await loadCase(caseDetail.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const detail = caseDetail && openCaseId === caseDetail.id ? caseDetail : null;

  return (
    <section aria-label="Discover app result">
      <DiscoverInputRenderer run={run} />
      {error && <StateBanner tone="error" text={`Error: ${error}`} />}
      {feed === null && !error ? (
        <StateBanner tone="loading" text="Loading feed…" />
      ) : (
        <section aria-label="Discover feed">
          <h4>Discover feed</h4>
          <p data-testid="discover-feed-fetches">{feedFetches}</p>
          {feed && feed.length === 0 && (
            <StateBanner tone="empty" text="No hypotheses in triage queue" />
          )}
          <ul aria-label="Hypotheses">
            {(feed ?? []).map((hypothesis) => (
              <li key={hypothesis.id}>
                <p>{hypothesis.title}</p>
                {/* score 以域名词呈现（启发式分值，非概率——投影层无该语义） */}
                {hypothesis.score !== null && <p>score: {hypothesis.score}</p>}
                <p>status: {hypothesis.status}</p>
                <p>owner: {hypothesis.owner || "—"}</p>
                <p>severity: {hypothesis.severity}</p>
                <p>
                  evidence: +{hypothesis.supporting_count}/-
                  {hypothesis.contradicting_count}/?
                  {hypothesis.missing_count}
                </p>
                <p>freshness: {hypothesis.freshness_hours}h</p>
                <p>dedup: {hypothesis.dedup_key || "—"}</p>
                {triageActions(hypothesis.status).map((transition) => (
                  <button
                    key={transition.label}
                    onClick={() =>
                      triage(hypothesis, transition.toStatus, transition.withOwner)
                    }
                  >
                    {transition.label}
                  </button>
                ))}
                {!hypothesis.case_id && (
                  <button onClick={() => createCase(hypothesis)}>Create case</button>
                )}
                {hypothesis.case_id && (
                  <button onClick={() => setOpenCaseId(hypothesis.case_id)}>
                    Open case
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {openCaseId && !detail && <StateBanner tone="loading" text="Loading case…" />}
      {detail && (
        <section aria-label="Discover case">
          <h4>Discover case</h4>
          <button
            onClick={() => {
              setOpenCaseId(null);
              setCaseDetail(null);
            }}
          >
            Back
          </button>
          <p>Title: {detail.title}</p>
          <p>Status: {detail.status}</p>
          <p>Owner: {detail.owner || "—"}</p>
          <p>Hypotheses: {detail.hypothesis_ids.join(", ")}</p>
          <p data-testid="discover-case-fetches">{caseFetches}</p>

          <h5>Actions</h5>
          {detail.actions.length === 0 ? (
            <StateBanner tone="empty" text="No actions submitted" />
          ) : (
            <ul aria-label="Actions">
              {detail.actions.map((action) => (
                <li key={action.id}>
                  <p>
                    {action.action_type} {action.tool_name} — {action.status}
                  </p>
                  <p>rationale: {action.rationale}</p>
                  <p>requested by: {action.requested_by}</p>
                  {action.approved_by && <p>approved by: {action.approved_by}</p>}
                  {action.status === "pending_approval" && (
                    <button onClick={() => approveAction(action)}>Approve</button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div>
            <label htmlFor="discover-action-type">Action type</label>
            <select
              id="discover-action-type"
              aria-label="Action type"
              value={actionType}
              onChange={(e) => setActionType(e.target.value)}
            >
              {ACTION_TYPES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <label htmlFor="discover-tool-name">Tool name</label>
            <input
              id="discover-tool-name"
              aria-label="Tool name"
              value={toolName}
              onChange={(e) => setToolName(e.target.value)}
            />
            <label htmlFor="discover-action-rationale">Rationale</label>
            <input
              id="discover-action-rationale"
              aria-label="Rationale"
              value={actionRationale}
              onChange={(e) => setActionRationale(e.target.value)}
            />
            <button onClick={submitAction}>Submit action</button>
          </div>
          {/* server-driven 门禁拒绝：detail 逐字呈现（不翻译、不改写） */}
          {refusal && <p role="alert">{refusal}</p>}

          <h5>Resolution</h5>
          {detail.resolutions.length === 0 ? (
            <p>No resolution recorded yet</p>
          ) : (
            detail.resolutions.map((resolution) => (
              <div key={resolution.id}>
                <p>Resolution: {resolution.kind}</p>
                <p>Resolution rationale: {resolution.rationale}</p>
                <p>Resolved by: {resolution.resolved_by}</p>
                <p>Approved by: {resolution.approved_by}</p>
              </div>
            ))
          )}
          <div>
            <label htmlFor="discover-resolution-kind">Resolution kind</label>
            <select
              id="discover-resolution-kind"
              aria-label="Resolution kind"
              value={resolutionKind}
              onChange={(e) => setResolutionKind(e.target.value)}
            >
              {RESOLUTION_KINDS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <label htmlFor="discover-resolution-rationale">Resolution rationale</label>
            <input
              id="discover-resolution-rationale"
              aria-label="Resolution rationale"
              value={resolutionRationale}
              onChange={(e) => setResolutionRationale(e.target.value)}
            />
            <button onClick={recordResolution}>Record resolution</button>
          </div>
        </section>
      )}
    </section>
  );
}
