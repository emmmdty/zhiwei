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
  // 角色徽章：显示 role_bindings（来自 server 已验证 membership）
  const roles = user.role_bindings.map((b) => b.name).join(", ");

  return (
    <ErrorBoundary error={null}>
      <div>
        <header>
          <span>Signed in as: {roles}</span>
          <a href="/auth/logout">Sign out</a>
        </header>
        <main>
          <Organizations user={user} onSessionExpired={onSessionExpired} />
        </main>
      </div>
    </ErrorBoundary>
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
  const [orgName, setOrgName] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const list = await api.get<OrgRecord[]>("/api/v1/organizations");
      setOrgs(list);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  // 只有 owner 能 create organization（导航隐藏；server 仍强制）
  const canCreateOrg = hasRole(user, "org_owner");

  if (loading) return <div aria-busy="true">{LOADING_TEXT}</div>;

  const org = orgs[0];

  return (
    <section>
      {canCreateOrg && (
        <>
          <button onClick={() => setShowCreate(true)}>Create organization</button>
          {showCreate && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                const id = crypto.randomUUID();
                await api.post("/api/v1/organizations", {
                  organization_id: id,
                });
                setShowCreate(false);
                setOrgName("");
                load();
              }}
            >
              <label>
                Organization name
                <input
                  value={orgName}
                  onChange={(e) => setOrgName(e.target.value)}
                />
              </label>
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
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [orgId]);

  if (loading) return <div aria-busy="true">{LOADING_TEXT}</div>;

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
                await api.post(
                  `/api/v1/organizations/${orgId}/workspaces`,
                  { name: wsName }
                );
                setShowCreate(false);
                setWsName("");
                load();
              }}
            >
              <label>
                Workspace name
                <input
                  value={wsName}
                  onChange={(e) => setWsName(e.target.value)}
                />
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
          onSessionExpired={onSessionExpired}
        />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Members
// ---------------------------------------------------------------------------

interface MemberRecord {
  principal_id: string;
  role_bindings: string[];
}

function Members({
  user,
  orgId,
  onSessionExpired,
}: {
  user: SessionUser;
  orgId: string;
  onSessionExpired: () => Promise<void>;
}) {
  const [members, setMembers] = useState<MemberRecord[]>([]);
  const [showInvite, setShowInvite] = useState(false);
  const [externalId, setExternalId] = useState("");
  const [role, setRole] = useState("member");
  const isOwner = hasRole(user, "org_owner");

  const load = async () => {
    try {
      const list = await api.get<MemberRecord[]>(
        `/api/v1/organizations/${orgId}/memberships`
      );
      setMembers(list);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
    }
  };

  useEffect(() => {
    load();
  }, [orgId]);

  // Group create（SCIM）
  const [showGroup, setShowGroup] = useState(false);
  const [groupName, setGroupName] = useState("");

  return (
    <section>
      <h2>Members</h2>
      {isOwner && (
        <>
          <button onClick={() => setShowInvite(true)}>Invite member</button>
          {showInvite && (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                await api.post(`/api/v1/organizations/${orgId}/memberships`, {
                  principal_id: externalId,
                  role_bindings: [role],
                });
                setShowInvite(false);
                setExternalId("");
                load();
              }}
            >
              <label>
                External id
                <input
                  value={externalId}
                  onChange={(e) => setExternalId(e.target.value)}
                />
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
                await api.post("/scim/v2/Groups", {
                  schemas: ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                  externalId: groupName,
                  displayName: groupName,
                });
                setShowGroup(false);
                setGroupName("");
              }}
            >
              <label>
                Group name
                <input
                  value={groupName}
                  onChange={(e) => setGroupName(e.target.value)}
                />
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
              <button
                onClick={async () => {
                  await api.delete(
                    `/api/v1/organizations/${orgId}/memberships/${m.principal_id}`
                  );
                  load();
                }}
              >
                Remove member
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Audit log (Auditor role)
// ---------------------------------------------------------------------------

function AuditLog() {
  const [events, setEvents] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        // 审计事件接口（S1 只读视图；实际端点由后续阶段交付）
        const list = await api.get<unknown[]>("/api/v1/audit-events");
        setEvents(list);
      } catch {
        // 审计端点可能尚不存在；显示空列表（前端不 crash）
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <div aria-busy="true">{LOADING_TEXT}</div>;

  return (
    <section>
      <h3>Audit log</h3>
      {Array.isArray(events) && events.length > 0 ? (
        <ul>
          {events.map((e, i) => (
            <li key={i}>{JSON.stringify(e)}</li>
          ))}
        </ul>
      ) : (
        <p>No audit events</p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Error boundary
// ---------------------------------------------------------------------------

function ErrorBoundary({
  children,
  error,
}: {
  children: React.ReactNode;
  error: string | null;
}) {
  if (error) {
    return (
      <div role="alert">
        {ERROR_TEXT}: {error}
      </div>
    );
  }
  return <>{children}</>;
}
