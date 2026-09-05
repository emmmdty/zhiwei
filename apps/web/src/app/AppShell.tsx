// S10-T1：role-aware Web shell（自 App.tsx 迁移至 prescribed layout）。
// 视觉契约 = e2e/tenancy.spec.ts Playwright journey（operator 授权第二种情形）。
// 纪律（§4 最后一段）：导航按角色隐藏按钮，但权限由 server PEP/RLS 强制——
// 前端不硬判 403，403/401 由 API 实际返回驱动。分区渲染数据来自
// routes/sections 注册表；组织面板常驻（非分区）。

import { useState } from "react";
import {
  DEFAULT_SECTION_KEY,
  resolveReadOnly,
  SECTIONS,
  type SectionContext,
} from "../routes/sections";
import { OrganizationsPanel } from "../features/organizations/OrganizationsPanel";
import { useSession, type SessionUser } from "../lib/session";

const LOADING_TEXT = "Loading…";

export function AppShell() {
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
  const [section, setSection] = useState<string>(DEFAULT_SECTION_KEY);
  const ctx: SectionContext = {
    user,
    readOnly: resolveReadOnly(user),
    onSessionExpired,
  };
  const active = SECTIONS.find((s) => s.key === section) ?? SECTIONS[0];
  return (
    <div>
      <header>
        <span>Signed in as: {roles}</span>
        <a href="/auth/logout">Sign out</a>
      </header>
      <nav aria-label="Primary">
        {SECTIONS.map((s) => (
          <button
            key={s.key}
            aria-current={section === s.key ? "page" : undefined}
            onClick={() => setSection(s.key)}
          >
            {s.label}
          </button>
        ))}
      </nav>
      <main>
        <OrganizationsPanel user={user} onSessionExpired={onSessionExpired} />
        {active.render(ctx)}
      </main>
    </div>
  );
}
