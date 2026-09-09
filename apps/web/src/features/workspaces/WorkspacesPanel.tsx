// S1-T6 工作区面板：workspace 列表 + 创建（org_owner）+ 角色入口显隐 + 审计
// 日志（auditor）+ 成员面板下钻（自 App.tsx 内联组件迁移，S10-T1）。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import {
  hasRole,
  useSession,
  writeWorkspaceSelection,
  type SessionUser,
} from "../../lib/session";
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
  const { refresh } = useSession();

  // 选中 = 写本地选择（org 作用域持久化）→ refresh() 一次让 /me 带头回显确认。
  // 回显成功前不假定选中生效：aria-pressed 只跟随 server 校验后的
  // user.workspace_id（声明由 membership 校验，前端不发明授权事实）。
  const selectWorkspace = async (workspaceId: string) => {
    writeWorkspaceSelection(orgId, workspaceId);
    await refresh();
  };

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
                const workspaceId = crypto.randomUUID();
                try {
                  await api.post(`/api/v1/organizations/${orgId}/workspaces`, {
                    workspace_id: workspaceId,
                    name: wsName,
                  });
                  setShowCreate(false);
                  setWsName("");
                  // Create 成功即自动选中新 workspace：bootstrap 已同事务授予
                  // workspace_admin（membership 必在），/me 带头回显可直接确认。
                  await selectWorkspace(workspaceId);
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
            <li key={ws.id}>
              <button
                type="button"
                aria-pressed={user.workspace_id === ws.id}
                onClick={() => selectWorkspace(ws.id)}
              >
                {ws.name}
              </button>
            </li>
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
