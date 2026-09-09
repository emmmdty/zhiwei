// S10-T4 Admin = governance home（specs/s10 §5 Admin journey）。
// 只复用既有面：AuditLogPanel（audit）、CostsView（cost health，组件级复用
// 而非复制数据逻辑）、MembersPanel（members + 角色绑定 policy 面）。不新增
// 任何后端调用；本分区不发明 policy 端点——角色绑定即 PEP 输入，成员管理
// 入口的权限由 server PEP 强制（§4 最后一段）。

import { AuditLogPanel } from "./AuditLogPanel";
import { CostsView } from "../costs/CostsView";
import { MembersPanel } from "../members/MembersPanel";
import { StateBanner } from "../../components/StateBanner";
import type { SessionUser } from "../../lib/session";

export function AdminView({
  user,
  readOnly,
  onSessionExpired,
}: {
  user: SessionUser;
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}) {
  return (
    <section aria-label="Admin">
      <h2>Admin</h2>
      <h3>Members &amp; policy</h3>
      {user.organization_id && user.workspace_id ? (
        <MembersPanel
          user={user}
          orgId={user.organization_id}
          wsId={user.workspace_id}
          onSessionExpired={onSessionExpired}
        />
      ) : (
        <StateBanner tone="empty" text="No organization context" />
      )}
      <AuditLogPanel />
      <h3>Cost health</h3>
      <CostsView onSessionExpired={onSessionExpired} />
      {readOnly && <p>Read-only session: governance mutations are disabled.</p>}
    </section>
  );
}
