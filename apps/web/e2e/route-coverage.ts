// S10-T4：route→API contract coverage 清单（specs/s10 §6「no dead page/action」）。
//
// 本文件是声明清单（数据，不是测试逻辑）：每个分区上每个可见控件映射到它触发的
// 真实 API 命令；没有后端行为的控件不得出现在 UI，因此清单里不存在无端点的控件。
// full-product.spec.ts 断言：
//   1) 四个产品 router（capabilities/connections/knowledge/memory，S10-T4 挂载）
//      的每个 mounted endpoint 都被某控件覆盖，或诚实登记在 noUiEndpoints
//      （如 SCIM 类 headless 面）并附 note——不允许静默丢端点；
//   2) 反向不撒谎：任何 uiMapping 声称的 endpoint 必须真实存在于
//      mountedProductEndpoints ∪ embeddedExistingEndpoints（UI 不发明 API）。
//
// 端点事实源：src/zhiwei/api/{capabilities,connections,knowledge,memory}.py
// 的 router 装饰器（path 参数用 {curly} 表示）。

export interface ApiEndpoint {
  router: string;
  method: "GET" | "POST" | "PUT" | "DELETE";
  path: string;
}

export interface UiMapping {
  section: string;
  view: string;
  /** 控件的可访问名（按钮文本 / 表单提交键），GREEN 后由 e2e journey 走通 */
  control: string;
  endpoint: ApiEndpoint;
  /** 同一控件触发的后续读回（如 mutation 后 refetch），不参与覆盖判定 */
  refetch?: ApiEndpoint;
}

export interface NoUiEndpoint {
  endpoint: ApiEndpoint;
  note: string;
}

// 四个 S10-T4 挂载 router 的全部 mounted endpoint（覆盖判定目标）。
export const mountedProductEndpoints: readonly ApiEndpoint[] = [
  // capabilities（src/zhiwei/api/capabilities.py）
  { router: "capabilities", method: "GET", path: "/api/v1/capabilities/providers" },
  { router: "capabilities", method: "POST", path: "/api/v1/capabilities/providers" },
  { router: "capabilities", method: "GET", path: "/api/v1/capabilities/providers/{provider_id}" },
  { router: "capabilities", method: "POST", path: "/api/v1/capabilities/providers/{provider_id}/actions" },
  { router: "capabilities", method: "GET", path: "/api/v1/capabilities/versions" },
  { router: "capabilities", method: "GET", path: "/api/v1/capabilities/versions/{version_id}" },
  { router: "capabilities", method: "GET", path: "/api/v1/capabilities/versions/{version_id}/diff" },
  { router: "capabilities", method: "GET", path: "/api/v1/capabilities/bindings" },
  { router: "capabilities", method: "POST", path: "/api/v1/capabilities/bindings" },
  { router: "capabilities", method: "DELETE", path: "/api/v1/capabilities/bindings/{binding_id}" },
  // connections（src/zhiwei/api/connections.py）
  { router: "connections", method: "GET", path: "/api/v1/connections" },
  { router: "connections", method: "POST", path: "/api/v1/connections" },
  { router: "connections", method: "GET", path: "/api/v1/connections/{connection_id}" },
  { router: "connections", method: "GET", path: "/api/v1/connections/{connection_id}/status" },
  { router: "connections", method: "POST", path: "/api/v1/connections/{connection_id}/actions" },
  // knowledge（src/zhiwei/api/knowledge.py）
  { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources" },
  { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources" },
  { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources/{source_id}/connect" },
  { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources/{source_id}/sync" },
  { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources/{source_id}/status" },
  { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources/{source_id}/versions" },
  { router: "knowledge", method: "PUT", path: "/api/v1/knowledge/sources/{source_id}/acl" },
  { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources/{source_id}/disable" },
  // memory（src/zhiwei/api/memory.py）
  { router: "memory", method: "GET", path: "/api/v1/memory/records" },
  { router: "memory", method: "GET", path: "/api/v1/memory/records/{record_id}" },
  { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/confirm" },
  { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/correct" },
  { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/revoke" },
  { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/delete" },
  { router: "memory", method: "POST", path: "/api/v1/memory/conflicts/resolve" },
  { router: "memory", method: "GET", path: "/api/v1/memory/conflicts" },
  { router: "memory", method: "POST", path: "/api/v1/memory/export" },
  { router: "memory", method: "GET", path: "/api/v1/memory/stats" },
];

// Admin 分区复用的既有 router 面（不是 S10-T4 覆盖目标，但清单如实登记
// AdminView 嵌入面板触发的真实调用——reuse, not duplicate）。
export const embeddedExistingEndpoints: readonly ApiEndpoint[] = [
  { router: "observability", method: "GET", path: "/api/v1/observability/costs" },
  { router: "memberships", method: "GET", path: "/api/v1/organizations/{organization_id}/members" },
  { router: "workspaces", method: "GET", path: "/api/v1/workspaces/{workspace_id}/groups" },
];

export const INVENTORY: {
  uiMappings: readonly UiMapping[];
  noUiEndpoints: readonly NoUiEndpoint[];
} = {
  uiMappings: [
    // ── Capabilities section（CapabilitiesView + ConnectionsPanel）──────
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Register provider",
      endpoint: { router: "capabilities", method: "POST", path: "/api/v1/capabilities/providers" },
      refetch: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/providers" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "providers table",
      endpoint: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/providers" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Open (provider)",
      endpoint: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/providers/{provider_id}" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Admit",
      endpoint: { router: "capabilities", method: "POST", path: "/api/v1/capabilities/providers/{provider_id}/actions" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Publish",
      endpoint: { router: "capabilities", method: "POST", path: "/api/v1/capabilities/providers/{provider_id}/actions" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Suspend (provider)",
      endpoint: { router: "capabilities", method: "POST", path: "/api/v1/capabilities/providers/{provider_id}/actions" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Revoke (provider)",
      endpoint: { router: "capabilities", method: "POST", path: "/api/v1/capabilities/providers/{provider_id}/actions" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "capability versions table",
      endpoint: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/versions" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Inspect (version)",
      endpoint: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/versions/{version_id}" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Show diff",
      endpoint: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/versions/{version_id}/diff" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "bindings table",
      endpoint: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/bindings" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Create binding",
      endpoint: { router: "capabilities", method: "POST", path: "/api/v1/capabilities/bindings" },
      refetch: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/bindings" },
    },
    {
      section: "capabilities",
      view: "CapabilitiesView",
      control: "Confirm unbind",
      endpoint: { router: "capabilities", method: "DELETE", path: "/api/v1/capabilities/bindings/{binding_id}" },
      refetch: { router: "capabilities", method: "GET", path: "/api/v1/capabilities/bindings" },
    },
    {
      section: "capabilities",
      view: "ConnectionsPanel",
      control: "connections table",
      endpoint: { router: "connections", method: "GET", path: "/api/v1/connections" },
    },
    {
      section: "capabilities",
      view: "ConnectionsPanel",
      control: "Confirm connection",
      endpoint: { router: "connections", method: "POST", path: "/api/v1/connections" },
      refetch: { router: "connections", method: "GET", path: "/api/v1/connections" },
    },
    {
      section: "capabilities",
      view: "ConnectionsPanel",
      control: "Open (connection)",
      endpoint: { router: "connections", method: "GET", path: "/api/v1/connections/{connection_id}" },
    },
    {
      section: "capabilities",
      view: "ConnectionsPanel",
      control: "Status",
      endpoint: { router: "connections", method: "GET", path: "/api/v1/connections/{connection_id}/status" },
    },
    {
      section: "capabilities",
      view: "ConnectionsPanel",
      control: "Suspend (connection)",
      endpoint: { router: "connections", method: "POST", path: "/api/v1/connections/{connection_id}/actions" },
      refetch: { router: "connections", method: "GET", path: "/api/v1/connections" },
    },
    {
      section: "capabilities",
      view: "ConnectionsPanel",
      control: "Revoke (connection)",
      endpoint: { router: "connections", method: "POST", path: "/api/v1/connections/{connection_id}/actions" },
      refetch: { router: "connections", method: "GET", path: "/api/v1/connections" },
    },

    // ── Knowledge section（KnowledgeView）──────────────────────────────
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "sources table",
      endpoint: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Save source",
      endpoint: { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources" },
      refetch: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Connect",
      endpoint: { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources/{source_id}/connect" },
      refetch: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Sync",
      endpoint: { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources/{source_id}/sync" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Status",
      endpoint: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources/{source_id}/status" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Versions",
      endpoint: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources/{source_id}/versions" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Save ACL",
      endpoint: { router: "knowledge", method: "PUT", path: "/api/v1/knowledge/sources/{source_id}/acl" },
      refetch: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Confirm disable",
      endpoint: { router: "knowledge", method: "POST", path: "/api/v1/knowledge/sources/{source_id}/disable" },
      refetch: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources" },
    },
    {
      section: "knowledge",
      view: "KnowledgeView",
      control: "Retry",
      endpoint: { router: "knowledge", method: "GET", path: "/api/v1/knowledge/sources" },
    },

    // ── Memory section（MemoryView）────────────────────────────────────
    {
      section: "memory",
      view: "MemoryView",
      control: "records table",
      endpoint: { router: "memory", method: "GET", path: "/api/v1/memory/records" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Open (record)",
      endpoint: { router: "memory", method: "GET", path: "/api/v1/memory/records/{record_id}" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Confirm",
      endpoint: { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/confirm" },
      refetch: { router: "memory", method: "GET", path: "/api/v1/memory/records" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Submit correction",
      endpoint: { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/correct" },
      refetch: { router: "memory", method: "GET", path: "/api/v1/memory/records" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Confirm revoke",
      endpoint: { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/revoke" },
      refetch: { router: "memory", method: "GET", path: "/api/v1/memory/records" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Confirm delete",
      endpoint: { router: "memory", method: "POST", path: "/api/v1/memory/records/{record_id}/delete" },
      refetch: { router: "memory", method: "GET", path: "/api/v1/memory/records" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "conflicts table",
      endpoint: { router: "memory", method: "GET", path: "/api/v1/memory/conflicts" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Resolve",
      endpoint: { router: "memory", method: "POST", path: "/api/v1/memory/conflicts/resolve" },
      refetch: { router: "memory", method: "GET", path: "/api/v1/memory/conflicts" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Export",
      endpoint: { router: "memory", method: "POST", path: "/api/v1/memory/export" },
    },
    {
      section: "memory",
      view: "MemoryView",
      control: "Stats",
      endpoint: { router: "memory", method: "GET", path: "/api/v1/memory/stats" },
    },

    // ── Admin section（AdminView：governance home，无新后端调用）────────
    {
      section: "admin",
      view: "AdminView > CostsView",
      control: "cost health embed",
      endpoint: { router: "observability", method: "GET", path: "/api/v1/observability/costs" },
    },
    {
      section: "admin",
      view: "AdminView > MembersPanel",
      control: "members embed",
      endpoint: { router: "memberships", method: "GET", path: "/api/v1/organizations/{organization_id}/members" },
    },
    {
      section: "admin",
      view: "AdminView > MembersPanel",
      control: "groups embed",
      endpoint: { router: "workspaces", method: "GET", path: "/api/v1/workspaces/{workspace_id}/groups" },
    },
  ],

  // 诚实登记：四个 router 中无 UI 的端点当前为空——33 个 mounted endpoint 全部
  // 有控件映射。后续新增 headless 面（如 SCIM 类）必须登记在此并给出来由。
  noUiEndpoints: [],
};

function endpointKey(e: ApiEndpoint): string {
  return `${e.method} ${e.path}`;
}

// 覆盖判定：mounted endpoint 必须被某控件覆盖，或诚实登记 noUi。
export function uncoveredBackendEndpoints(): ApiEndpoint[] {
  const covered = new Set(INVENTORY.uiMappings.map((m) => endpointKey(m.endpoint)));
  const waived = new Set(INVENTORY.noUiEndpoints.map((m) => endpointKey(m.endpoint)));
  return mountedProductEndpoints.filter(
    (e) => !covered.has(endpointKey(e)) && !waived.has(endpointKey(e))
  );
}

// 反向不撒谎：UI 清单声称的每个 endpoint 都必须真实存在于已挂载 router。
export function inventedApiCalls(): UiMapping[] {
  const real = new Set(
    [...mountedProductEndpoints, ...embeddedExistingEndpoints].map(endpointKey)
  );
  return INVENTORY.uiMappings.filter((m) => !real.has(endpointKey(m.endpoint)));
}
