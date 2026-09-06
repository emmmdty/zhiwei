// S10-T3 Studio Release 分区：S9 evaluate/review/stage/publish 流的 Studio 集成。
//
// 契约（specs/s10 §3、plan Task 3；网络形状逐字段对齐真实端点）：
// - 生命周期动作只走既有 S9 命令（POST /api/v1/agents/{id}/releases、
//   GET /api/v1/releases、POST /{id}/advance、POST /{id}/rollback）——无 PATCH
//   生命周期旁路；下方 LIFECYCLE/TRANSITIONS 是 agents/release.py 冻结矩阵的
//   展示镜像（display data），不是第二套状态机；
// - advance 按当前状态逐边呈现、按状态门禁启用：SoD 的强制点是 server（域层
//   require_transition_permission + PEP），拒绝以 409 {reason,message} 经
//   RefusalNotice 原样呈现——按角色预禁用会让机器可读拒绝面不可达（RED (f)
//   契约：builder 必须能对 evaluated→review 发起并看到 409）；
// - rollback 入口按 release 角色映射门禁（api/releases.py
//   _RELEASE_ROLE_PLATFORM_ROLES 镜像，与 ReleasesView 同一 releaseRoles）；
// - readiness/diff/manifest 是只读支撑面（test_agents_release_support_api.py
//   契约），全部按钮显式触发，不做挂载期自动请求（mock fail-loud 契约）。

import { useState } from "react";
import { api, ApiError, SessionExpiredError } from "../../../lib/api";
import { parseRefusal, RefusalNotice } from "../../../components/RefusalNotice";
import { StateBanner } from "../../../components/StateBanner";
import { useSession, type SessionUser } from "../../../lib/session";

interface ReleaseListItem {
  release_id: string;
  agent_id: string;
  agent_version: number;
  state: string;
  manifest_digest: string;
  default_version: number | null;
}

interface ReadinessCheck {
  kind: string;
  detail: string;
}

interface ReleaseReadiness {
  ready: boolean;
  missing: ReadinessCheck[];
}

interface DiffField {
  field: string;
  from: string | null;
  to: string | null;
  kind: string;
}

interface AgentRevisionDiff {
  fields: DiffField[];
}

interface ReleaseManifestView {
  release_id: string;
  agent_id: string;
  agent_version: number;
  manifest_digest: string;
  pack_digest: string;
  model_digest: string;
  knowledge_digest: string;
  memory_digest: string;
  capability_digest: string;
  policy_digest: string;
  eval_digests: string[];
  approver: string;
  rollout: { default_version: number | null; cohorts: unknown[] };
  rollback: { in_flight: string };
}

interface RollbackOutcome {
  release_id: string;
  applies_to: string;
  executed: boolean;
  in_flight_disposition: string;
  in_flight_run_ids: string[];
  default_version: number | null;
}

// agents/release.py ReleaseState 顺序（retired 是唯一终态）
const LIFECYCLE = [
  "draft",
  "sandbox",
  "evaluated",
  "review",
  "staged",
  "published",
  "deprecated",
  "retired",
] as const;

// agents/release.py ALLOWED_RELEASE_TRANSITIONS 展示镜像：每边的授权 release 角色
const TRANSITIONS: { from: string; next: string; role: string }[] = [
  { from: "draft", next: "sandbox", role: "builder" },
  { from: "sandbox", next: "evaluated", role: "builder" },
  { from: "evaluated", next: "review", role: "reviewer" },
  { from: "review", next: "staged", role: "approver" },
  { from: "staged", next: "published", role: "release_manager" },
  { from: "published", next: "deprecated", role: "release_manager" },
  { from: "deprecated", next: "retired", role: "release_manager" },
];

// api/releases.py _RELEASE_ROLE_PLATFORM_ROLES 镜像：workspace_admin 同时映射
// reviewer/approver/release_manager（权限取并集）
function releaseRoles(user: SessionUser | null): Set<string> {
  const names = new Set((user?.role_bindings ?? []).map((binding) => binding.name));
  const roles = new Set<string>();
  if (names.has("agent_builder")) roles.add("builder");
  if (names.has("workspace_admin")) {
    roles.add("reviewer");
    roles.add("approver");
    roles.add("release_manager");
  }
  if (names.has("approver")) roles.add("approver");
  return roles;
}

const DIGEST_FIELDS = [
  ["pack_digest", "Pack digest"],
  ["model_digest", "Model digest"],
  ["knowledge_digest", "Knowledge digest"],
  ["memory_digest", "Memory digest"],
  ["capability_digest", "Capability digest"],
  ["policy_digest", "Policy digest"],
] as const;

type DigestKey = (typeof DIGEST_FIELDS)[number][0];

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// 剪贴板在无权限环境（headless、非安全上下文）会缺失或拒绝——复制失败静默，
// digest 全文始终在列表里 verbatim 呈现（Copy 只是便利入口，不是唯一读面）
function copyDigest(value: string): void {
  void navigator.clipboard?.writeText(value)?.catch(() => {});
}

interface ReleasePanelProps {
  agentId: string;
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}

export function ReleasePanel({ agentId, readOnly, onSessionExpired }: ReleasePanelProps) {
  const { state: sessionState } = useSession();
  const user = sessionState.status === "authenticated" ? sessionState.user : null;
  const roles = releaseRoles(user);

  const [readiness, setReadiness] = useState<ReleaseReadiness | null>(null);
  const [readinessBusy, setReadinessBusy] = useState(false);
  const [releases, setReleases] = useState<ReleaseListItem[] | null>(null);
  const [releasesBusy, setReleasesBusy] = useState(false);
  const [selected, setSelected] = useState<ReleaseListItem | null>(null);
  const [advancing, setAdvancing] = useState<string | null>(null);
  const [manifest, setManifest] = useState<ReleaseManifestView | null>(null);
  const [manifestBusy, setManifestBusy] = useState(false);
  const [refusal, setRefusal] = useState<{ reason: string; message: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [digests, setDigests] = useState<Record<string, string>>({});
  const [approver, setApprover] = useState("");
  const [rolloutVersion, setRolloutVersion] = useState("1");
  const [inFlight, setInFlight] = useState<"complete" | "terminate">("complete");
  const [creating, setCreating] = useState(false);

  const [fromRevision, setFromRevision] = useState("1");
  const [toRevision, setToRevision] = useState("2");
  const [diff, setDiff] = useState<AgentRevisionDiff | null>(null);
  const [diffBusy, setDiffBusy] = useState(false);

  const [rollbackTo, setRollbackTo] = useState("1");
  const [inFlightIds, setInFlightIds] = useState("");
  const [rollbackBusy, setRollbackBusy] = useState(false);
  const [rollbackOutcome, setRollbackOutcome] = useState<RollbackOutcome | null>(null);

  const selectRelease = (release: ReleaseListItem) => {
    setSelected(release);
    setManifest(null);
    setRollbackOutcome(null);
    setRefusal(null);
    setNotice(null);
  };

  const checkReadiness = async () => {
    setReadinessBusy(true);
    setNotice(null);
    try {
      setReadiness(
        await api.get<ReleaseReadiness>(`/api/v1/agents/${agentId}/release-readiness`)
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setNotice(errorText(e));
    } finally {
      setReadinessBusy(false);
    }
  };

  const loadReleases = async () => {
    setReleasesBusy(true);
    setNotice(null);
    try {
      const all = await api.get<ReleaseListItem[]>("/api/v1/releases");
      setReleases(all.filter((release) => release.agent_id === agentId));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setNotice(errorText(e));
    } finally {
      setReleasesBusy(false);
    }
  };

  const createRelease = async () => {
    setCreating(true);
    setNotice(null);
    try {
      const view = await api.post<ReleaseListItem>(`/api/v1/agents/${agentId}/releases`, {
        pack_digest: digests.pack_digest ?? "",
        model_digest: digests.model_digest ?? "",
        knowledge_digest: digests.knowledge_digest ?? "",
        memory_digest: digests.memory_digest ?? "",
        capability_digest: digests.capability_digest ?? "",
        policy_digest: digests.policy_digest ?? "",
        eval_digests: [],
        // approver 留空 → server 落 actor principal（不伪造审批人）
        ...(approver.trim() ? { approver: approver.trim() } : {}),
        rollout: {
          default_version: Number(rolloutVersion) > 0 ? Number(rolloutVersion) : null,
          cohorts: [],
        },
        rollback: { in_flight: inFlight },
      });
      setReleases(null);
      selectRelease(view);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setNotice(errorText(e));
    } finally {
      setCreating(false);
    }
  };

  const advance = async (target: string) => {
    if (!selected) return;
    setAdvancing(target);
    setRefusal(null);
    setNotice(null);
    try {
      setSelected(
        await api.post<ReleaseListItem>(`/api/v1/releases/${selected.release_id}/advance`, {
          target_state: target,
        })
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      // 409 {reason,message} 是 SoD 的正常拒绝面（域层 ReleaseTransitionDenied
      // 镜像）：结构化呈现，状态保持不变（域层拒绝不产生变更）
      if (e instanceof ApiError) {
        const parsed = parseRefusal(e.detail);
        if (parsed) {
          setRefusal(parsed);
          return;
        }
      }
      setNotice(errorText(e));
    } finally {
      setAdvancing(null);
    }
  };

  const loadManifest = async () => {
    if (!selected) return;
    setManifestBusy(true);
    setNotice(null);
    try {
      setManifest(
        await api.get<ReleaseManifestView>(
          `/api/v1/releases/${selected.release_id}/manifest`
        )
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setNotice(errorText(e));
    } finally {
      setManifestBusy(false);
    }
  };

  const rollback = async () => {
    if (!selected) return;
    setRollbackBusy(true);
    setRefusal(null);
    setNotice(null);
    try {
      const ids = inFlightIds
        .split(",")
        .map((id) => id.trim())
        .filter(Boolean);
      const outcome = await api.post<RollbackOutcome>(
        `/api/v1/releases/${selected.release_id}/rollback`,
        { to_version: Number(rollbackTo), in_flight_run_ids: ids }
      );
      setRollbackOutcome(outcome);
      // 回滚只改 default pin（对新 Run 生效）——本地投影同步，不额外请求
      setSelected({ ...selected, default_version: outcome.default_version });
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      if (e instanceof ApiError) {
        const parsed = parseRefusal(e.detail);
        if (parsed) {
          setRefusal(parsed);
          return;
        }
      }
      setNotice(errorText(e));
    } finally {
      setRollbackBusy(false);
    }
  };

  const showDiff = async () => {
    setDiffBusy(true);
    setNotice(null);
    try {
      setDiff(
        await api.get<AgentRevisionDiff>(
          `/api/v1/agents/${agentId}/diff?from_revision=${Number(fromRevision)}&to_revision=${Number(toRevision)}`
        )
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setNotice(errorText(e));
    } finally {
      setDiffBusy(false);
    }
  };

  const diffParamsValid =
    Number.isInteger(Number(fromRevision)) &&
    Number(fromRevision) >= 1 &&
    Number.isInteger(Number(toRevision)) &&
    Number(toRevision) >= 1;

  return (
    <div>
      <button disabled={readinessBusy} onClick={() => void checkReadiness()}>
        Check readiness
      </button>
      {readiness && (
        <>
          <p>{readiness.ready ? "Release readiness: ready" : "Release readiness: not ready"}</p>
          <ul aria-label="Release readiness">
            {readiness.missing.map((check, index) => (
              <li key={`${check.kind}-${index}`}>
                {check.kind === "unknown"
                  ? `unknown: ${check.detail}`
                  : `missing: ${check.kind} — ${check.detail}`}
              </li>
            ))}
          </ul>
        </>
      )}

      <button disabled={releasesBusy} onClick={() => void loadReleases()}>
        Load releases
      </button>
      {releases !== null &&
        (releases.length === 0 ? (
          <p>No releases for this agent.</p>
        ) : (
          <ul aria-label="Agent releases">
            {releases.map((release) => (
              <li key={release.release_id}>
                {release.release_id} · agent version {release.agent_version} · state{" "}
                {release.state}
                <button onClick={() => selectRelease(release)}>Open</button>
              </li>
            ))}
          </ul>
        ))}

      {selected && (
        <div>
          <p>
            State: {selected.state} (ID: {selected.release_id})
          </p>
          <p>Default pin: {selected.default_version ?? "unknown"}</p>
          {refusal && <RefusalNotice refusal={refusal} />}
          <ol aria-label="Release lifecycle">
            {LIFECYCLE.map((state) => (
              <li key={state}>
                {state}
                {state === selected.state ? " (current)" : ""}
              </li>
            ))}
          </ol>
          {TRANSITIONS.map((edge) => (
            <button
              key={`${edge.from}-${edge.next}`}
              disabled={readOnly || selected.state !== edge.from || advancing !== null}
              onClick={() => void advance(edge.next)}
            >
              Advance {edge.from} to {edge.next}
            </button>
          ))}
          <button disabled={manifestBusy || advancing !== null} onClick={() => void loadManifest()}>
            Load manifest
          </button>
          {manifest && (
            <ul aria-label="Release manifest">
              <li>manifest digest: {manifest.manifest_digest}</li>
              {DIGEST_FIELDS.map(([key, label]) => (
                <li key={key}>
                  {`${label.toLowerCase()}: ${manifest[key as DigestKey]}`}
                  <button onClick={() => copyDigest(manifest[key as DigestKey])}>
                    Copy {label.toLowerCase()}
                  </button>
                </li>
              ))}
              <li>eval digests: {manifest.eval_digests.join(", ")}</li>
              <li>approver: {manifest.approver}</li>
              <li>agent: {manifest.agent_id}</li>
              <li>agent version: {manifest.agent_version}</li>
              <li>rollout default version: {manifest.rollout.default_version ?? "unknown"}</li>
              <li>rollback in-flight: {manifest.rollback.in_flight}</li>
            </ul>
          )}
          <div>
            {/* 回滚只改 default pin 且只影响新 Run；在途 Run 按 rollback 声明处置 */}
            <label>
              Roll back to version
              <input
                type="number"
                min={1}
                value={rollbackTo}
                onChange={(e) => setRollbackTo(e.target.value)}
              />
            </label>
            <label>
              In-flight run IDs (comma separated)
              <input
                value={inFlightIds}
                onChange={(e) => setInFlightIds(e.target.value)}
              />
            </label>
            <button
              disabled={readOnly || !roles.has("release_manager") || rollbackBusy}
              onClick={() => void rollback()}
            >
              Roll back
            </button>
          </div>
          {rollbackOutcome && (
            <ul aria-label="Rollback outcome">
              <li>applies to: {rollbackOutcome.applies_to.replace(/_/g, " ")}</li>
              <li>executed: {String(rollbackOutcome.executed)}</li>
              <li>in-flight disposition: {rollbackOutcome.in_flight_disposition}</li>
              <li>
                in-flight run ids:{" "}
                {rollbackOutcome.in_flight_run_ids.length > 0
                  ? rollbackOutcome.in_flight_run_ids.join(", ")
                  : "none"}
              </li>
              <li>default version: {rollbackOutcome.default_version ?? "unknown"}</li>
            </ul>
          )}
        </div>
      )}

      {!readOnly && (
        <div>
          {DIGEST_FIELDS.map(([key, label]) => (
            <label key={key}>
              {label}
              <input
                value={digests[key] ?? ""}
                onChange={(e) => setDigests({ ...digests, [key]: e.target.value })}
              />
            </label>
          ))}
          <label>
            Approver
            <input value={approver} onChange={(e) => setApprover(e.target.value)} />
          </label>
          <label>
            Rollout default version
            <input
              type="number"
              min={1}
              value={rolloutVersion}
              onChange={(e) => setRolloutVersion(e.target.value)}
            />
          </label>
          <label>
            Rollback in-flight
            <select
              value={inFlight}
              onChange={(e) => setInFlight(e.target.value as "complete" | "terminate")}
            >
              <option value="complete">complete</option>
              <option value="terminate">terminate</option>
            </select>
          </label>
          <button disabled={creating} onClick={() => void createRelease()}>
            Create draft release
          </button>
        </div>
      )}

      <div>
        <label>
          Diff from revision
          <input
            type="number"
            min={1}
            value={fromRevision}
            onChange={(e) => setFromRevision(e.target.value)}
          />
        </label>
        <label>
          Diff to revision
          <input
            type="number"
            min={1}
            value={toRevision}
            onChange={(e) => setToRevision(e.target.value)}
          />
        </label>
        <button
          disabled={diffBusy || !diffParamsValid}
          onClick={() => void showDiff()}
        >
          Show diff
        </button>
        {diff && (
          <ul aria-label="Agent version diff">
            {diff.fields.map((field, index) => (
              <li key={`${field.field}-${index}`}>
                {`${field.field}: ${field.from ?? "unset"} → ${field.to ?? "unset"} (${field.kind})`}
              </li>
            ))}
          </ul>
        )}
      </div>

      {notice && <StateBanner tone="error" text={`Error: ${notice}`} />}
    </div>
  );
}
