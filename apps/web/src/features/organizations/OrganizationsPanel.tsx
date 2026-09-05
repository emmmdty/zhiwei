// S1-T6 组织面板：org 列表 + bootstrap 创建入口（自 App.tsx 内联组件迁移，
// S10-T1 prescribed layout）。语义不变：bootstrap 入口可见性按 operator
// 2026-09-05 journey 修订裁决——权限由 PEP 的 bootstrap_org_create 在服务端
// 强制，前端只管入口显隐。

import { useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { hasRole, useSession, type SessionUser } from "../../lib/session";
import { WorkspacesPanel } from "../workspaces/WorkspacesPanel";

const LOADING_TEXT = "Loading…";
const EMPTY_WS = "No workspaces yet";
const ERROR_TEXT = "Something went wrong";

interface OrgRecord {
  id: string;
  status: string;
}

export function OrganizationsPanel({
  user,
  onSessionExpired,
}: {
  user: SessionUser;
  onSessionExpired: () => Promise<void>;
}) {
  const { refresh } = useSession();
  const [orgs, setOrgs] = useState<OrgRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isOwner = hasRole(user, "org_owner");
  // 首登用户的 bootstrap 入口：零角色且无组织上下文时可见（operator 2026-09-05
  // journey 修订裁决）。导航显隐仅是入口可见性，权限仍由 PEP 的
  // bootstrap_org_create（零角色 + 零 active org）在服务端强制。
  const canBootstrap =
    isOwner || (user.role_bindings.length === 0 && user.organization_id === null);

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

  if (loading) return <StateBanner tone="loading" text={LOADING_TEXT} />;
  if (error) return <StateBanner tone="error" text={`${ERROR_TEXT}: ${error}`} />;

  const org = orgs[0];

  return (
    <section>
      {canBootstrap && (
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
                  // bootstrap 后 principal 才有第一个 membership：先 refresh()
                  // 让 /me 解析出新 org context（tenant header 全局注入），
                  // 再拉 org 列表——否则后续 mutation 带陈旧/缺失
                  // X-ZhiWei-Organization 被 PEP 拒绝（s1-t6 §5 N-3）。
                  await refresh();
                  await load();
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
      {org && (
        <WorkspacesPanel user={user} orgId={org.id} onSessionExpired={onSessionExpired} />
      )}
      {!org && <StateBanner tone="empty" text={EMPTY_WS} />}
    </section>
  );
}
