// S1-T6 成员面板：成员列表 + 邀请 + 角色绑定 + 群组（org_owner 管理入口）+
// 两段式移除（自 App.tsx 内联组件迁移，S10-T1）。
// workspace 上下文由后端从路径资源推导（PEP 判定 + RLS 对齐）——前端不声明
// X-ZhiWei-Workspace：header 声明语境要求 workspace membership 行，org 作用域
// 角色没有。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { ConfirmButton } from "../../components/ConfirmButton";
import { StateBanner } from "../../components/StateBanner";
import { hasRole, type SessionUser } from "../../lib/session";

const ERROR_TEXT = "Something went wrong";

interface MemberRow {
  principal_id: string;
  organization_id: string;
  role_bindings: string[];
}

interface GroupRow {
  id: string;
  name: string;
}

export function MembersPanel({
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
  const [groups, setGroups] = useState<GroupRow[]>([]);
  const [showInvite, setShowInvite] = useState(false);
  const [principalId, setPrincipalId] = useState("");
  const [role, setRole] = useState("member");
  const [showGroup, setShowGroup] = useState(false);
  const [groupName, setGroupName] = useState("");
  const [selectedPrincipal, setSelectedPrincipal] = useState<string | null>(null);
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

  const loadGroups = async () => {
    try {
      const list = await api.get<GroupRow[]>(`/api/v1/workspaces/${wsId}/groups`);
      setGroups(list);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    load();
  }, [orgId]);

  useEffect(() => {
    loadGroups();
  }, [wsId]);

  const removeMember = async (target: string) => {
    try {
      await api.delete(`/api/v1/organizations/${orgId}/members/${target}`);
      setSelectedPrincipal(null);
      load();
    } catch (err) {
      if (err instanceof SessionExpiredError) return onSessionExpired();
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <section>
      <h2>Members</h2>
      {error && <StateBanner tone="error" text={`${ERROR_TEXT}: ${error}`} />}
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
                  await loadGroups();
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
      {groups.length > 0 && (
        <section>
          <h3>Groups</h3>
          <ul>
            {groups.map((g) => (
              <li key={g.id}>{g.name}</li>
            ))}
          </ul>
        </section>
      )}
      <ul>
        {members.map((m) => (
          <li key={m.principal_id}>
            {/* journey 语义：先点击成员行选中，再出现该成员的 Remove member——
                移除按钮按选中态渲染，避免每行一个导致的歧义。 */}
            <button
              className="member-row"
              onClick={() => setSelectedPrincipal(m.principal_id)}
            >
              {m.principal_id}
            </button>
            {isOwner && selectedPrincipal === m.principal_id && (
              <ConfirmButton
                label="Remove member"
                confirmLabel="Confirm removal"
                notice="Confirm removal?"
                onConfirm={() => void removeMember(m.principal_id)}
              />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
