// S9-T7 Releases 视图：列表 + 生命周期 stepper + advance/route/rollback 动作。
//
// 契约（api/releases.py ReleaseView）：列表/详情只暴露 release_id/agent_id/
// agent_version/state/manifest_digest/default_version——manifest 内的 approver
// 与 rollout cohorts 不是 API 投影字段，UI 以 unknown 原样呈现（不发明字段）。
// 角色显隐镜像 api/releases.py _RELEASE_ROLE_PLATFORM_ROLES；真正的 SoD 由
// server PEP + 域层双重强制，前端只负责入口可见性。
// security suspend 指示器常驻：suspend 先于一切 pin 判定（agents/rollout.py）。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { hasRole, type SessionUser } from "../../lib/session";

interface ReleaseListItem {
  release_id: string;
  agent_id: string;
  agent_version: number;
  state: string;
  manifest_digest: string;
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

// 迁移矩阵（api/releases.py ALLOWED_RELEASE_TRANSITIONS 的 UI 镜像）：每个
// 状态的唯一下一步与所需 release 角色。
const NEXT_TRANSITIONS: Partial<Record<string, { next: string; role: string }>> = {
  draft: { next: "sandbox", role: "builder" },
  sandbox: { next: "evaluated", role: "builder" },
  evaluated: { next: "review", role: "reviewer" },
  review: { next: "staged", role: "approver" },
  staged: { next: "published", role: "release_manager" },
  published: { next: "deprecated", role: "release_manager" },
  deprecated: { next: "retired", role: "release_manager" },
};

function releaseRoles(user: SessionUser): Set<string> {
  const names = new Set(user.role_bindings.map((b) => b.name));
  // api/releases.py _RELEASE_ROLE_PLATFORM_ROLES：workspace_admin 同时映射
  // reviewer/approver/release_manager（权限取并集）
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

interface ReleasesViewProps {
  user: SessionUser;
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}

export function ReleasesView({ user, readOnly, onSessionExpired }: ReleasesViewProps) {
  const [releases, setReleases] = useState<ReleaseListItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setReleases(await api.get<ReleaseListItem[]>("/api/v1/releases"));
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

  if (selected) {
    return (
      <ReleaseDetailView
        releaseId={selected}
        user={user}
        readOnly={readOnly}
        onBack={() => {
          setSelected(null);
          load();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }

  return (
    <section aria-label="Releases">
      <h2>Releases</h2>
      {loading ? (
        <StateBanner tone="loading" text="Loading releases…" />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : releases.length === 0 ? (
        <StateBanner tone="empty" text="No releases" />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Release</th>
              <th>Agent</th>
              <th>Version</th>
              <th>State</th>
              <th>Default pin</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {releases.map((release) => (
              <tr key={release.release_id}>
                <td>{release.release_id}</td>
                <td>{release.agent_id}</td>
                <td>{release.agent_version}</td>
                <td>{release.state}</td>
                <td>{release.default_version ?? "unknown"}</td>
                <td>
                  <button onClick={() => setSelected(release.release_id)}>Open</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

interface RollbackOutcome {
  release_id: string;
  applies_to: string;
  executed: boolean;
  in_flight_disposition: string;
  in_flight_run_ids: string[];
  default_version: number | null;
}

function ReleaseDetailView({
  releaseId,
  user,
  readOnly,
  onBack,
  onSessionExpired,
}: {
  releaseId: string;
  user: SessionUser;
  readOnly: boolean;
  onBack: () => void;
  onSessionExpired: () => Promise<void>;
}) {
  const [release, setRelease] = useState<ReleaseListItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [advancing, setAdvancing] = useState(false);
  const [routeResult, setRouteResult] = useState<string | null>(null);
  const [routeBusy, setRouteBusy] = useState(false);
  const [suspended, setSuspended] = useState(false);
  const [showRollback, setShowRollback] = useState(false);
  const [rollbackTo, setRollbackTo] = useState("1");
  const [inFlightIds, setInFlightIds] = useState("");
  const [rollbackBusy, setRollbackBusy] = useState(false);
  const [rollbackOutcome, setRollbackOutcome] = useState<RollbackOutcome | null>(null);

  const roles = releaseRoles(user);
  // PEP cell agent_publish.rollback = workspace_admin（冻结矩阵；approver 平台
  // 角色单独持有不通过 PEP，UI 与之一致）
  const canRollback = hasRole(user, "workspace_admin");

  const load = async () => {
    try {
      setRelease(await api.get<ReleaseListItem>(`/api/v1/releases/${releaseId}`));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [releaseId]);

  const advance = async (target: string) => {
    setAdvancing(true);
    setError(null);
    try {
      setRelease(
        await api.post<ReleaseListItem>(`/api/v1/releases/${releaseId}/advance`, {
          target_state: target,
        })
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAdvancing(false);
    }
  };

  const resolveRoute = async () => {
    setRouteBusy(true);
    setError(null);
    try {
      const result = await api.post<{ release_id: string; version: number }>(
        `/api/v1/releases/${releaseId}/route`,
        { suspended }
      );
      setRouteResult(`route: version ${result.version}`);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setRouteResult(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRouteBusy(false);
    }
  };

  const rollback = async () => {
    setRollbackBusy(true);
    setError(null);
    try {
      const ids = inFlightIds
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const outcome = await api.post<RollbackOutcome>(
        `/api/v1/releases/${releaseId}/rollback`,
        { to_version: Number(rollbackTo), in_flight_run_ids: ids }
      );
      setRollbackOutcome(outcome);
      setShowRollback(false);
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRollbackBusy(false);
    }
  };

  if (loading) return <StateBanner tone="loading" text="Loading release…" />;
  if (error && !release) return <StateBanner tone="error" text={`Error: ${error}`} />;
  if (!release) return <div>Release not found</div>;

  const transition = NEXT_TRANSITIONS[release.state];
  const canAdvance =
    transition !== undefined && !readOnly && roles.has(transition.role);

  return (
    <section aria-label={`Release ${releaseId}`}>
      <button onClick={onBack}>Back</button>
      <h2>Release</h2>
      <p>
        State: {release.state} (ID: {release.release_id})
      </p>
      {error && (
        <StateBanner
          tone="error"
          text={`Something went wrong: ${error}`}
        />
      )}

      <h3>Lifecycle</h3>
      <ol aria-label="Release lifecycle">
        {LIFECYCLE.map((state) => (
          <li key={state}>
            {state}
            {state === release.state ? " (current)" : ""}
          </li>
        ))}
      </ol>

      <h3>Manifest</h3>
      <ul>
        <li>manifest digest: {release.manifest_digest}</li>
        <li>agent: {release.agent_id}</li>
        <li>agent version: {release.agent_version}</li>
        <li>default pin: {release.default_version ?? "unknown"}</li>
        {/* approver/cohorts 是 manifest 记录字段，ReleaseView 投影不含 → unknown */}
        <li>approver: unknown</li>
        <li>cohorts: unknown</li>
      </ul>

      <h3>Transition</h3>
      {transition ? (
        <button
          onClick={() => advance(transition.next)}
          disabled={!canAdvance || advancing}
        >
          Advance to {transition.next}
        </button>
      ) : (
        <p>No further transition (terminal state)</p>
      )}

      <h3>Routing</h3>
      {/* suspend 指示器常驻：suspend 生效时先于一切 pin 判定（不受理 pin 保护） */}
      <p>Security suspend overrides all release pins when active.</p>
      <label>
        <input
          type="checkbox"
          checked={suspended}
          onChange={(e) => setSuspended(e.target.checked)}
        />{" "}
        Suspended
      </label>
      <button onClick={resolveRoute} disabled={readOnly || routeBusy}>
        Resolve route
      </button>
      {routeResult && <p>{routeResult}</p>}

      <h3>Rollback</h3>
      <button onClick={() => setShowRollback(true)} disabled={readOnly || !canRollback}>
        Rollback
      </button>
      {showRollback && (
        <div role="dialog" aria-label="Rollback">
          {/* 回滚只改 default pin 且只影响新 Run；在途 Run 按 rollback 声明处置 */}
          <p>
            Rollback applies to new runs only; in-flight runs follow the declared
            disposition.
          </p>
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
          <button onClick={rollback} disabled={rollbackBusy}>
            Confirm rollback
          </button>
        </div>
      )}
      {rollbackOutcome && (
        <ul>
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
    </section>
  );
}
