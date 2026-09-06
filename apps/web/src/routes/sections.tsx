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
import { KnowledgeView } from "../features/knowledge/KnowledgeView";
import { CapabilitiesView } from "../features/capabilities/CapabilitiesView";
import { MemoryView } from "../features/memory/MemoryView";
import { AdminView } from "../features/admin/AdminView";
import { CaseView } from "../features/cases/CaseView";
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
  // S10-T4 产品壳 journey 收口：knowledge/capabilities/memory/admin。分区入口
  // 对所有已认证用户可见（与 shell 既有纪律一致——读路径权限由 server PEP 强制，
  // 403 由 API 实际返回驱动）；mutation 控件在视图内部按角色显隐/禁用：
  // capabilities 的 lifecycle 动作要求 capability_publisher / security_admin、
  // knowledge 的变更动作要求非只读（builder 等变更角色）、memory 的 confirm
  // 仅 steward 可见（server _STEWARD_ROLE_NAMES 镜像）、auditor 全局只读。
  {
    key: "knowledge",
    label: "Knowledge",
    render: (ctx) => (
      <KnowledgeView
        readOnly={ctx.readOnly}
        onSessionExpired={ctx.onSessionExpired}
      />
    ),
  },
  {
    key: "capabilities",
    label: "Capabilities",
    render: (ctx) => (
      <CapabilitiesView
        user={ctx.user}
        readOnly={ctx.readOnly}
        onSessionExpired={ctx.onSessionExpired}
      />
    ),
  },
  {
    key: "memory",
    label: "Memory",
    render: (ctx) => (
      <MemoryView
        user={ctx.user}
        readOnly={ctx.readOnly}
        onSessionExpired={ctx.onSessionExpired}
      />
    ),
  },
  {
    key: "admin",
    label: "Admin",
    render: (ctx) => (
      <AdminView
        user={ctx.user}
        readOnly={ctx.readOnly}
        onSessionExpired={ctx.onSessionExpired}
      />
    ),
  },
  // S10-T4b：case surface 分区（S6 §4 通用 Case 面；创建入口在 RunDetailView，
  // 本分区只读呈现列表/详情，状态机转移 API 未落地前不提供操作控件）
  {
    key: "cases",
    label: "Cases",
    render: (ctx) => <CaseView onSessionExpired={ctx.onSessionExpired} />,
  },
];

export const DEFAULT_SECTION_KEY = SECTIONS[0].key;

export function resolveReadOnly(user: SessionUser): boolean {
  return hasRole(user, "auditor");
}
