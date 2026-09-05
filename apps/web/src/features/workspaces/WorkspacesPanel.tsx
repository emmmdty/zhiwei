// S1-T6 工作区面板：workspace 列表 + 创建（org_owner）+ 角色入口显隐 + 审计
// 日志（auditor）+ 成员面板下钻（自 App.tsx 内联组件迁移，S10-T1）。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { hasRole, type SessionUser } from "../../lib/session";
import { MembersPanel } from "../members/MembersPanel";
import { AuditLogPanel } from "../admin/AuditLogPanel";

const LOADING_TEXT = "Loading…";
const EMPTY_WS = "No workspaces yet";
const ERROR_TEXT = "Something went wrong";

interface WorkspaceRecord {
  id: string;
  name: string;
}

export function WorkspacesPanel({
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
  const isBuilder = hasRole(user, "agent_builder");
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

  if (loading) return <StateBanner tone="loading" text={LOADING_TEXT} />;
  if (error) return <StateBanner tone="error" text={`${ERROR_TEXT}: ${error}`} />;

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
      {isAuditor && <AuditLogPanel />}
      {workspaces.length === 0 ? (
        <StateBanner tone="empty" text={EMPTY_WS} />
      ) : (
        <ul>
          {workspaces.map((ws) => (
            <li key={ws.id}>{ws.name}</li>
          ))}
        </ul>
      )}
      {workspaces[0] && (
        <MembersPanel
          user={user}
          orgId={orgId}
          wsId={workspaces[0].id}
          onSessionExpired={onSessionExpired}
        />
      )}
    </section>
  );
}
