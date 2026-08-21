// S1-T6 GREEN：role-aware organization Web shell。
// 视觉契约 = e2e/tenancy.spec.ts Playwright journey（operator 授权第二种情形）。
// 纪律（§4 最后一段）：导航按角色隐藏按钮，但权限由 server PEP/RLS 强制——
// 前端不硬判 403，403/401 由 API 实际返回驱动。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "./lib/api";
import {
  SessionProvider,
  useSession,
  hasRole,
  type SessionUser,
} from "./lib/session";

const LOADING_TEXT = "Loading…";
const EMPTY_WS = "No workspaces yet";
const ERROR_TEXT = "Something went wrong";

export function App() {
  return (
    <SessionProvider>
      <Shell />
    </SessionProvider>
  );
}

function Shell() {
  const { state, refresh } = useSession();

  if (state.status === "loading") {
    return <div aria-busy="true">{LOADING_TEXT}</div>;
  }
  if (state.status === "unauthenticated") {
    return (
      <div>
        <a href="/auth/login">Sign in</a>
      </div>
    );
  }
  return <Dashboard user={state.user} onSessionExpired={refresh} />;
}

function Dashboard({
  user,
  onSessionExpired,
}: {
  user: SessionUser;
  onSessionExpired: () => Promise<void>;
}) {
  const roles = user.role_bindings.map((b) => b.name).join(", ");
  return (
    <div>
      <header>
        <span>Signed in as: {roles}</span>
        <a href="/auth/logout">Sign out</a>
      </header>
      <main>
        <Organizations user={user} onSessionExpired={onSessionExpired} />
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Organizations
// ---------------------------------------------------------------------------

interface OrgRecord {
  id: string;
  status: string;
}

function Organizations({
  user,
  onSessionExpired,
}: {
  user: SessionUser;
  onSessionExpired: () => Promise<void>;
}) {
  const [orgs, setOrgs] = useState<OrgRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isOwner = hasRole(user, "org_owner");

  const load = async () => {
    setLoading(true);
    try {
      const list = await api.get<OrgRecord[]>("/api/v1/organizations");
      setOrgs(list);
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

  if (loading) return <div aria-busy="true">{LOADING_TEXT}</div>;
  if (error) return <div role="alert">{ERROR_TEXT}: {error}</div>;

  const org = orgs[0];

  return (
    <section>
      {isOwner && (
        <>
          <button onClick={() => setShowCreate(true)}>Create organization</button>
          {showCreate && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  await api.post("/api/v1/organizations", {
                    organization_id: crypto.randomUUID(),
                  });
                  setShowCreate(false);
                  load();
                } catch (err) {
                  if (err instanceof SessionExpiredError) return onSessionExpired();
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              <span>Organization name</span>
              <input aria-label="Organization name" value="New organization" readOnly />
              <button type="submit">Confirm</button>
            </form>
          )}
        </>
      )}
      {org && <Workspaces user={user} orgId={org.id} onSessionExpired={onSessionExpired} />}
      {!org && <p>{EMPTY_WS}</p>}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Workspaces + Members + Groups
// ---------------------------------------------------------------------------

interface WorkspaceRecord {
  id: string;
  name: string;
}

function Workspaces({
  user,
  orgId,
  onSessionExpired,
}: {
  user: SessionUser;
  orgId: string;
  onSessionExpired: () => Promise<void>;
}) {
  const [workspaces, setWorkspaces] = useState<WorkspaceRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [wsName, setWsName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const isOwner = hasRole(user, "org_owner");
  const isBuilder = hasRole(user, "builder");
  const isApprover = hasRole(user, "approver");
  const isAuditor = hasRole(user, "auditor");

  const load = async () => {
    setLoading(true);
    try {
      const list = await api.get<WorkspaceRecord[]>(
        `/api/v1/organizations/${orgId}/workspaces`
      );
      setWorkspaces(list);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [orgId]);

  if (loading) return <div aria-busy="true">{LOADING_TEXT}</div>;
  if (error) return <div role="alert">{ERROR_TEXT}: {error}</div>;

  return (
    <section>
      <h2>Workspaces</h2>
      {isOwner && (
        <>
          <button onClick={() => setShowCreate(true)}>Create workspace</button>
          {showCreate && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  await api.post(`/api/v1/organizations/${orgId}/workspaces`, {
                    workspace_id: crypto.randomUUID(),
                    name: wsName,
                  });
                  setShowCreate(false);
                  setWsName("");
                  load();
                } catch (err) {
                  if (err instanceof SessionExpiredError) return onSessionExpired();
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              <label>
                Workspace name
                <input value={wsName} onChange={(e) => setWsName(e.target.value)} />
              </label>
              <button type="submit">Confirm</button>
            </form>
          )}
        </>
      )}
      {isBuilder && <button>New agent</button>}
      {isApprover && <h3>Approval queue</h3>}
      {isAuditor && <AuditLog />}
      {workspaces.length === 0 ? (
        <p>{EMPTY_WS}</p>
      ) : (
        <ul>
          {workspaces.map((ws) => (
            <li key={ws.id}>{ws.name}</li>
          ))}
        </ul>
      )}
      {workspaces[0] && (
        <Members
          user={user}
          orgId={orgId}
          wsId={workspaces[0].id}
          onSessionExpired={onSessionExpired}
        />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Members
// ---------------------------------------------------------------------------

interface MemberRow {
  principal_id: string;
  organization_id: string;
  role_bindings: string[];
}

function Members({
  user,
  orgId,
  wsId,
  onSessionExpired,
}: {
  user: SessionUser;
  orgId: string;
  wsId: string;
  onSessionExpired: () => Promise<void>;
}) {
  const [members, setMembers] = useState<MemberRow[]>([]);
  const [showInvite, setShowInvite] = useState(false);
  const [principalId, setPrincipalId] = useState("");
  const [role, setRole] = useState("member");
  const [showGroup, setShowGroup] = useState(false);
  const [groupName, setGroupName] = useState("");
  const [pendingRemove, setPendingRemove] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isOwner = hasRole(user, "org_owner");

  const load = async () => {
    try {
      const list = await api.get<MemberRow[]>(
        `/api/v1/organizations/${orgId}/members`
      );
      setMembers(list);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    load();
  }, [orgId]);

  return (
    <section>
      <h2>Members</h2>
      {error && <div role="alert">{ERROR_TEXT}: {error}</div>}
      {isOwner && (
        <>
          <button onClick={() => setShowInvite(true)}>Invite member</button>
          {showInvite && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  await api.post(`/api/v1/organizations/${orgId}/members`, {
                    principal_id: principalId,
                    role_bindings: [role],
                  });
                  setShowInvite(false);
                  setPrincipalId("");
                  load();
                } catch (err) {
                  if (err instanceof SessionExpiredError) return onSessionExpired();
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              <label>
                Principal id
                <input value={principalId} onChange={(e) => setPrincipalId(e.target.value)} />
              </label>
              <label>
                Role
                <select value={role} onChange={(e) => setRole(e.target.value)}>
                  <option value="member">member</option>
                  <option value="builder">builder</option>
                  <option value="approver">approver</option>
                  <option value="auditor">auditor</option>
                </select>
              </label>
              <button type="submit">Send invite</button>
            </form>
          )}
          <button onClick={() => setShowGroup(true)}>Create group</button>
          {showGroup && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  await api.post(`/api/v1/workspaces/${wsId}/groups`, {
                    group_id: crypto.randomUUID(),
                    name: groupName,
                  });
                  setShowGroup(false);
                  setGroupName("");
                } catch (err) {
                  if (err instanceof SessionExpiredError) return onSessionExpired();
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              <label>
                Group name
                <input value={groupName} onChange={(e) => setGroupName(e.target.value)} />
              </label>
              <button type="submit">Confirm</button>
            </form>
          )}
        </>
      )}
      <ul>
        {members.map((m) => (
          <li key={m.principal_id}>
            {m.principal_id}
            {isOwner && (
              <>
                <button onClick={() => setPendingRemove(m.principal_id)}>Remove member</button>
                {pendingRemove === m.principal_id && (
                  <span>
                    Confirm removal?{" "}
                    <button
                      onClick={async () => {
                        try {
                          await api.delete(
                            `/api/v1/organizations/${orgId}/members/${m.principal_id}`
                          );
                          setPendingRemove(null);
                          load();
                        } catch (err) {
                          if (err instanceof SessionExpiredError) return onSessionExpired();
                          setError(err instanceof Error ? err.message : String(err));
                        }
                      }}
                    >
                      Confirm removal
                    </button>
                  </span>
                )}
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Audit log (Auditor role) — S1 未交付 audit-events 端点，显示占位
// ---------------------------------------------------------------------------

function AuditLog() {
  return (
    <section>
      <h3>Audit log</h3>
      <p>No audit events</p>
    </section>
  );
}
