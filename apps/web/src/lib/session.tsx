// S1-T6 session context：fetch /api/v1/me 获取 principal + organizations +
// context + CSRF；再经 GET /organizations/{org}/members 解析当前用户的
// role_bindings（/me 不返回角色，成员列表是唯一权威角色来源）。
// 401 → unauthenticated（Sign in）；200 → authenticated（角色 + 导航）。
// 角色判定只做导航隐藏，权限由 server PEP/RLS 强制（§4 最后一段）。

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, setSessionMeta } from "./api";

export interface RoleBinding {
  name: string;
  scope: "org" | "workspace";
}

export interface SessionUser {
  principal_id: string;
  organization_id: string | null;
  workspace_id: string | null;
  role_bindings: RoleBinding[];
}

interface MembershipRow {
  principal_id: string;
  organization_id: string;
  role_bindings: string[];
}

// 镜像 src/zhiwei/policy/roles.py LEGACY_ROLE_ALIASES：membership 里存储的是
// 历史自由字符串（bootstrap 写 "owner"，邀请 UI 发 "builder"），PEP 求值前经
// normalize_role 归一。前端角色判定必须消费同一归一结果，否则 bootstrapped
// owner 的 isOwner 恒 false（s1-t6 §5-3 N-2）。未知字符串保持原样——权限仍由
// server PEP 强制，前端归一只影响导航显隐。
const LEGACY_ROLE_ALIASES: Record<string, string> = {
  owner: "org_owner",
  builder: "agent_builder",
};

function normalizeRoleName(name: string): string {
  return LEGACY_ROLE_ALIASES[name] ?? name;
}

interface MeResponse {
  principal: { id: string };
  organizations: { id: string; status: string }[];
  context: { organization_id: string | null; workspace_id: string | null };
  csrf_token: string;
}

export type SessionState =
  | { status: "loading" }
  | { status: "unauthenticated" }
  | { status: "authenticated"; user: SessionUser };

const SessionContext = createContext<{
  state: SessionState;
  refresh: () => Promise<void>;
}>({
  state: { status: "loading" },
  refresh: async () => {},
});

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "loading" });

  const refresh = async () => {
    try {
      const me = await api.get<MeResponse>("/api/v1/me");
      setSessionMeta(me.csrf_token, {
        ...(me.context.organization_id
          ? { "X-ZhiWei-Organization": me.context.organization_id }
          : {}),
        ...(me.context.workspace_id
          ? { "X-ZhiWei-Workspace": me.context.workspace_id }
          : {}),
      });
      let role_bindings: RoleBinding[] = [];
      if (me.context.organization_id) {
        try {
          const members = await api.get<MembershipRow[]>(
            `/api/v1/organizations/${me.context.organization_id}/members`
          );
          const mine = members.find(
            (m) => m.principal_id === me.principal.id
          );
          role_bindings = (mine?.role_bindings ?? []).map((name) => ({
            name: normalizeRoleName(name),
            scope: "org" as const,
          }));
        } catch {
          role_bindings = [];
        }
      }
      setState({
        status: "authenticated",
        user: {
          principal_id: me.principal.id,
          organization_id: me.context.organization_id,
          workspace_id: me.context.workspace_id,
          role_bindings,
        },
      });
    } catch (e) {
      setState({ status: "unauthenticated" });
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  return (
    <SessionContext.Provider value={{ state, refresh }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  return useContext(SessionContext);
}

export function hasRole(user: SessionUser | null, role: string): boolean {
  if (!user) return false;
  return user.role_bindings.some((b) => b.name === role);
}
