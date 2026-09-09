// S1-T6 session context：fetch /api/v1/me 获取 principal + organizations +
// context + CSRF；再经 GET /organizations/{org}/members 解析当前用户的
// role_bindings（/me 不返回角色，成员列表是唯一权威角色来源）。
// 401 → unauthenticated（Sign in）；200 → authenticated（角色 + 导航）。
// 角色判定只做导航隐藏，权限由 server PEP/RLS 强制（§4 最后一段）。

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, api, setSessionMeta } from "./api";

// S11 followup-2 任务二：workspace 上下文死环修复的本地选择存储。服务端
// context 只从请求头解析（api/auth.py 唯一入口），/me 无头时 workspace_id 恒
// null——前端必须有写入口，选中值按组织作用域持久化（刷新不丢、多 org 不串）。
// 授权语义不变：头声明只是请求，membership 由服务端逐条校验（§2.2）。
const WS_SELECTION_PREFIX = "zhiwei.ws.";

export function readWorkspaceSelection(orgId: string): string | null {
  try {
    return localStorage.getItem(WS_SELECTION_PREFIX + orgId);
  } catch {
    // storage 不可用（隐私模式等）：选中态退化为会话内不持久，回显兜底仍工作
    return null;
  }
}

export function writeWorkspaceSelection(orgId: string, workspaceId: string | null): void {
  try {
    if (workspaceId === null) {
      localStorage.removeItem(WS_SELECTION_PREFIX + orgId);
    } else {
      localStorage.setItem(WS_SELECTION_PREFIX + orgId, workspaceId);
    }
  } catch {
    // 同上：持久化失败不阻断选中流程
  }
}

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
  // refresh() 可能经由透传回调在旧渲染闭包外被调用（AppShell onSessionExpired
  // 链），org hint 取 ref 而非闭包 state，避免陈旧 org 声明把 /me 引向 404。
  const stateRef = useRef<SessionState>({ status: "loading" });
  const commit = (next: SessionState) => {
    stateRef.current = next;
    setState(next);
  };

  const refresh = async () => {
    try {
      const me = await fetchMe();
      await applyMe(me);
    } catch {
      commit({ status: "unauthenticated" });
    }
  };

  // 头注入优先级 = 本地选中值优先、/me 回显兜底：
  // 1) 已知 org 时带上本地选中的 ws 头（点击选中 / Create 后自动选中的确认路径）；
  //    带头 /me 404（membership 被撤/失效，GET fail closed）→ 清除该选择、去
  //    ws 头重试一次，回退仅 org 上下文——否则会把仍持有效 session 的用户
  //    显示成未登录（假登录态）。
  // 2) 页面刷新恢复：首个 /me（无头）回显 ws null 时，按本地选中值补一次带头
  //    /me——回显成功即 membership 仍有效；404 则清除选择，沿用仅 org 上下文。
  const fetchMe = async (): Promise<MeResponse> => {
    const current = stateRef.current;
    const orgHint =
      current.status === "authenticated" ? current.user.organization_id : null;
    const selection = orgHint ? readWorkspaceSelection(orgHint) : null;
    let me: MeResponse;
    try {
      me = await api.get<MeResponse>("/api/v1/me", {
        ...(orgHint ? { "X-ZhiWei-Organization": orgHint } : {}),
        ...(selection ? { "X-ZhiWei-Workspace": selection } : {}),
      });
    } catch (e) {
      if (orgHint && selection && e instanceof ApiError && e.status === 404) {
        writeWorkspaceSelection(orgHint, null);
        me = await api.get<MeResponse>("/api/v1/me", {
          "X-ZhiWei-Organization": orgHint,
        });
      } else {
        throw e;
      }
    }
    const org = me.context.organization_id;
    if (org && !me.context.workspace_id) {
      const stored = readWorkspaceSelection(org);
      if (stored) {
        try {
          me = await api.get<MeResponse>("/api/v1/me", {
            "X-ZhiWei-Organization": org,
            "X-ZhiWei-Workspace": stored,
          });
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) {
            writeWorkspaceSelection(org, null);
          } else {
            throw e;
          }
        }
      }
    }
    return me;
  };

  const applyMe = async (me: MeResponse) => {
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
        const mine = members.find((m) => m.principal_id === me.principal.id);
        role_bindings = (mine?.role_bindings ?? []).map((name) => ({
          name: normalizeRoleName(name),
          scope: "org" as const,
        }));
      } catch {
        role_bindings = [];
      }
    }
    commit({
      status: "authenticated",
      user: {
        principal_id: me.principal.id,
        organization_id: me.context.organization_id,
        workspace_id: me.context.workspace_id,
        role_bindings,
      },
    });
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
