// S1-T6 session context：fetch /api/v1/me 获取 authenticated principal + CSRF。
// 401 → unauthenticated（显示 Sign in）；200 → authenticated（显示角色 + 导航）。
// principal.role_bindings 来自 membership resolver（已验证，不信任前端声明）。

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, setCsrfToken, SessionExpiredError } from "./api";

export interface RoleBinding {
  name: string;
  scope: "org" | "workspace";
}

export interface SessionUser {
  principal_id: string;
  organization_id: string | null;
  workspace_id: string | null;
  csrf_token: string;
  role_bindings: RoleBinding[];
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
      const user = await api.get<SessionUser>("/api/v1/me");
      setCsrfToken(user.csrf_token);
      setState({ status: "authenticated", user });
    } catch (e) {
      if (e instanceof SessionExpiredError) {
        setState({ status: "unauthenticated" });
      } else {
        setState({ status: "unauthenticated" });
      }
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

// 角色判定：role_bindings 来自 server（PEP/RLS 已验证），前端只做导航隐藏，
// 不做权限决策——403/401 由 API 实际返回驱动（§4 最后一段）。
export function hasRole(
  user: SessionUser | null,
  role: string
): boolean {
  if (!user) return false;
  return user.role_bindings.some((b) => b.name === role);
}
