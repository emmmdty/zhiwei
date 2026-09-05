// S10-T1：主导航分区注册表（数据驱动）。AppShell 只遍历本表渲染——分区
// 增删不改 shell 代码。分区入口对所有已认证用户可见：读路径权限由 server
// PEP 强制；auditor 的 mutation 控件在各视图内部禁用（readOnly），前端不硬
// 判 403（§4 最后一段）。默认分区 workbench（既有 tenancy/runtime journey
// 落点不变）。

import type { ReactNode } from "react";
import { Workbench } from "../features/workbench/Workbench";
import { EvalRunsView } from "../features/evals/EvalRunsView";
import { ReleasesView } from "../features/releases/ReleasesView";
import { ObservabilityView } from "../features/observability/ObservabilityView";
import { CostsView } from "../features/costs/CostsView";
import { StudioView } from "../features/studio/StudioView";
import { hasRole, type SessionUser } from "../lib/session";

export interface SectionContext {
  user: SessionUser;
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}

export interface SectionDescriptor {
  key: string;
  label: string;
  render: (ctx: SectionContext) => ReactNode;
}

export const SECTIONS: readonly SectionDescriptor[] = [
  {
    key: "workbench",
    label: "Workbench",
    // 既有语义：无 workspace 上下文时 workbench 分区不渲染（组织面板仍在）
    render: (ctx) =>
      ctx.user.workspace_id ? (
        <Workbench
          workspaceId={ctx.user.workspace_id}
          onSessionExpired={ctx.onSessionExpired}
        />
      ) : null,
  },
  {
    key: "evals",
    label: "Evals",
    render: (ctx) => (
      <EvalRunsView readOnly={ctx.readOnly} onSessionExpired={ctx.onSessionExpired} />
    ),
  },
  {
    key: "releases",
    label: "Releases",
    render: (ctx) => (
      <ReleasesView
        user={ctx.user}
        readOnly={ctx.readOnly}
        onSessionExpired={ctx.onSessionExpired}
      />
    ),
  },
  {
    key: "observability",
    label: "Observability",
    render: (ctx) => <ObservabilityView onSessionExpired={ctx.onSessionExpired} />,
  },
  {
    key: "costs",
    label: "Costs",
    render: (ctx) => <CostsView onSessionExpired={ctx.onSessionExpired} />,
  },
  {
    key: "studio",
    label: "Agent Studio",
    render: (ctx) => (
      <StudioView readOnly={ctx.readOnly} onSessionExpired={ctx.onSessionExpired} />
    ),
  },
];

export const DEFAULT_SECTION_KEY = SECTIONS[0].key;

export function resolveReadOnly(user: SessionUser): boolean {
  return hasRole(user, "auditor");
}
