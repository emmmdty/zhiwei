// S4 capability-hub e2e（specs/s4-capability-hub.md §6 → Gate 例外条目
// docs/handoffs/s4-capability-hub-e2e-exception.md 的解锁 spec）。
//
// 与 S4 §6 Web journey 的映射（以真实路由/UI 为准，mock 模式先例
// runtime-approval.spec.ts / full-product.spec.ts）：
//   spec §6 旅程                                  | 本 spec 落点（真实 UI）
//   ----------------------------------------------+----------------------------------
//   Publisher 导入（Registry/URL）                | ImportProviderForm → POST
//                                                 | /api/v1/capabilities/providers
//                                                 | （name + source_url 为真实
//                                                 | RegisterProviderRequest 字段）
//   检视 source/version/risk/test                 | ProviderDetailView（
//                                                 | classification/risk_level/
//                                                 | content_digest/source_url 逐字）
//                                                 | + VersionDetailView（type/name/
//                                                 | version/status/digest/test
//                                                 | digest/parent/metadata + diff）
//   批准                                          | Admit → status approved →
//                                                 | Publish → published（能力版本
//                                                 | 状态跟随 provider，api/
//                                                 | capabilities.py 语义）
//   创建 Workspace Connection 并 test             | ConnectionsPanel create →
//                                                 | GET /connections/{id}/status
//                                                 | （fingerprint + credential_
//                                                 | status 是 API 提供的 test 面）
//   Builder 只能绑定 published CapabilityVersion  | BindForm → POST bindings；
//                                                 | 未发布绑定被 409 拒，机器可读
//                                                 | 原因原样上浮
//   Security Admin suspend/revoke → 结构化失败    | ProviderDetailView suspend/
//   与受影响版本                                  | revoke → provider 状态投影 +
//                                                 | 受影响能力版本清单（版本状态
//                                                 | 跟随 provider）；绑定被拒/
//                                                 | connection 复动作为结构化失败
//                                                 | （409 detail 逐字）
//
// 后端边界（mock 模式，operator 已授权同 runtime-approval.spec.ts）：响应形状
// 1:1 对齐 src/zhiwei/api/{capabilities,connections}.py 的 Pydantic 投影（含
// test_digest="" 的真实 domain 默认值）；不发任何真实外部请求；未模拟的 /api
// 路径一律 500 显式失败（fail loud）。检视只渲染 API 真实字段——不发明
// SBOM/vulnerability 检查项（本 spec 不替代 §7 真实 provider reference
// integration，Fake 件边界纪律不变）。

import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

// 固定标识（本 spec 内 mock 域；不依赖 compose 种子数据）
const ORG_ID = "3a1a8d1c-a63f-4bed-87d1-b67948aea7ac";
const WS_ID = "6f1c2a34-9b7e-4d0a-8f61-0c5b2d7e9a11";
const CSRF = "e2e-csrf-token";
const PUBLISHER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
const BUILDER_ID = "3383f6a7-d17b-44c2-802c-d67c3974e13a";
const SECADMIN_ID = "8d2f5b1a-93c4-4e87-a6d9-5f0c7b8e2a44";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// mock 域确定性 id（池序号分配，同 full-product.spec.ts 先例）
function newSeqId(pool: number, seq: number): string {
  return `00000000-${String(pool).padStart(4, "0")}-4000-8000-${String(seq).padStart(12, "0")}`;
}

function digest(seed: string): string {
  let hex = "";
  for (let i = 0; i < 64; i++) hex += ((seed.charCodeAt(i % seed.length) + i) % 16).toString(16);
  return `sha256:${hex}`;
}

type Actor = "publisher" | "builder" | "security_admin";

// 平台角色绑定（policy/roles.py Role 冻结词汇；session.tsx 经 members 列表解析）。
// security_admin 是纯角色（无 capability_publisher）——S4 §6 的双主体分离：
// 发布与安全停用由不同主体执行。
const ACTORS: Record<Actor, { principal: string; role_bindings: string[] }> = {
  publisher: { principal: PUBLISHER_ID, role_bindings: ["capability_publisher"] },
  builder: { principal: BUILDER_ID, role_bindings: ["agent_builder"] },
  security_admin: { principal: SECADMIN_ID, role_bindings: ["security_admin"] },
};

// ---------------------------------------------------------------------------
// mock 域状态（字段名 = api/capabilities.py / api/connections.py 投影字段名）
// ---------------------------------------------------------------------------

interface ProviderRecord {
  id: string;
  provider_id: string;
  name: string;
  version: number;
  description: string;
  status: string;
  classification: string;
  source_url: string | null;
  risk_level: string;
  content_digest: string;
}

interface CapabilityVersionRecord {
  id: string;
  capability_type: string;
  name: string;
  version: number;
  status: string;
  risk_level: string;
  content_digest: string;
  test_digest: string;
  parent_id: string | null;
  metadata: Record<string, string>;
}

interface BindingRecord {
  id: string;
  organization_id: string;
  workspace_id: string;
  agent_definition_id: string;
  agent_version_id: string;
  capability_version_id: string;
  status: string;
}

interface ConnectionRecord {
  id: string;
  organization_id: string;
  workspace_id: string;
  provider_version_id: string;
  subject_mode: string;
  status: string;
  principal_id: string | null;
  version: number;
  fingerprint: string;
}

interface MockState {
  providers: ProviderRecord[];
  capVersions: CapabilityVersionRecord[];
  bindings: BindingRecord[];
  connections: ConnectionRecord[];
  seq: { provider: number; binding: number; connection: number };
  lastMutation: { method: string; path: string; headers: Record<string, string> } | null;
  lastBind: { body: Record<string, string> } | null;
}

function newState(): MockState {
  return {
    providers: [],
    capVersions: [],
    bindings: [],
    connections: [],
    seq: { provider: 0, binding: 0, connection: 0 },
    lastMutation: null,
    lastBind: null,
  };
}

// ---------------------------------------------------------------------------
// 网络层 mock：单一 catch-all 路由内部分发；未模拟路径 500 fail loud
// ---------------------------------------------------------------------------

function installApiMocks(context: BrowserContext, state: MockState, actor: Actor): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  context.route("/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();

    // 会话引导（session.tsx 消费的真实契约）
    if (path === "/api/v1/me" && method === "GET") {
      return fulfill(route, 200, {
        principal: { id: ACTORS[actor].principal },
        organizations: [{ id: ORG_ID, status: "active" }],
        context: { organization_id: ORG_ID, workspace_id: WS_ID },
        csrf_token: CSRF,
      });
    }
    if (path === "/api/v1/organizations" && method === "GET") {
      return fulfill(route, 200, [{ id: ORG_ID, status: "active" }]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/members` && method === "GET") {
      return fulfill(route, 200, Object.values(ACTORS).map((a) => ({
        principal_id: a.principal,
        organization_id: ORG_ID,
        role_bindings: a.role_bindings,
      })));
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    // Workbench 默认分区挂载拉取（本 spec 不消费 run 面）
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, []);
    }

    // ------------------------------------------------------------------
    // Capabilities（api/capabilities.py 契约，逐字段对齐 Pydantic 投影）
    // ------------------------------------------------------------------
    if (path === "/api/v1/capabilities/providers" && method === "GET") {
      return fulfill(route, 200, state.providers);
    }
    if (path === "/api/v1/capabilities/providers" && method === "POST") {
      state.lastMutation = { method, path, headers: req.headers() };
      const body = req.postDataJSON() as {
        name: string;
        description?: string;
        source_url?: string | null;
        classification?: string;
        risk_level?: string;
      };
      state.seq.provider += 1;
      const n = state.seq.provider;
      const providerId = newSeqId(1, n);
      const capVersionId = newSeqId(11, n);
      const provider: ProviderRecord = {
        id: providerId,
        provider_id: newSeqId(2, n),
        name: body.name,
        version: 1,
        description: body.description ?? "",
        status: "discovered",
        classification: body.classification ?? "PUBLIC",
        source_url: body.source_url ?? null,
        risk_level: body.risk_level ?? "low",
        content_digest: digest(`provider-${n}`),
      };
      state.providers.push(provider);
      // 能力版本随注册创建（register_provider 语义），test_digest 为 domain 默认 ""
      state.capVersions.push({
        id: capVersionId,
        capability_type: "provider",
        name: body.name,
        version: 1,
        status: "discovered",
        risk_level: provider.risk_level,
        content_digest: provider.content_digest,
        test_digest: "",
        parent_id: null,
        metadata: { provider_version_id: providerId },
      });
      return fulfill(route, 201, provider);
    }
    const providerMatch = path.match(/^\/api\/v1\/capabilities\/providers\/([0-9a-f-]{36})$/);
    if (providerMatch && method === "GET") {
      const provider = state.providers.find((p) => p.id === providerMatch[1]);
      if (!provider) return fulfill(route, 404, { detail: "provider not found" });
      return fulfill(route, 200, provider);
    }
    const providerAction = path.match(/^\/api\/v1\/capabilities\/providers\/([0-9a-f-]{36})\/actions$/);
    if (providerAction && method === "POST") {
      const provider = state.providers.find((p) => p.id === providerAction[1]);
      if (!provider) return fulfill(route, 404, { detail: "provider not found" });
      const body = req.postDataJSON() as { action?: string };
      state.lastMutation = { method, path, headers: req.headers() };
      const transitions: Record<string, string> = {
        quarantine: "quarantined",
        inspect: "inspected",
        test: "tested",
        admit: "approved",
        publish: "published",
        suspend: "suspended",
        revoke: "revoked",
      };
      if (!body.action || !(body.action in transitions)) {
        return fulfill(route, 422, { detail: `unknown action: ${body.action ?? ""}` });
      }
      provider.status = transitions[body.action];
      // 能力版本状态跟随 provider（api/capabilities.py provider_action 语义）
      for (const cv of state.capVersions) {
        if (cv.metadata.provider_version_id === provider.id) cv.status = provider.status;
      }
      return fulfill(route, 200, provider);
    }
    if (path === "/api/v1/capabilities/versions" && method === "GET") {
      return fulfill(route, 200, state.capVersions);
    }
    const versionMatch = path.match(/^\/api\/v1\/capabilities\/versions\/([0-9a-f-]{36})$/);
    if (versionMatch && method === "GET") {
      const version = state.capVersions.find((v) => v.id === versionMatch[1]);
      if (!version) return fulfill(route, 404, { detail: "capability version not found" });
      return fulfill(route, 200, version);
    }
    const diffMatch = path.match(/^\/api\/v1\/capabilities\/versions\/([0-9a-f-]{36})\/diff$/);
    if (diffMatch && method === "GET") {
      const version = state.capVersions.find((v) => v.id === diffMatch[1]);
      if (!version) return fulfill(route, 404, { detail: "capability version not found" });
      // v1 无前版本：from 0，三标志 true（api/capabilities.py 语义）
      return fulfill(route, 200, {
        from_version: 0,
        to_version: version.version,
        content_changed: true,
        risk_changed: true,
        status_changed: true,
      });
    }
    if (path === "/api/v1/capabilities/bindings" && method === "GET") {
      return fulfill(route, 200, state.bindings);
    }
    if (path === "/api/v1/capabilities/bindings" && method === "POST") {
      const body = req.postDataJSON() as {
        agent_definition_id: string;
        agent_version_id: string;
        capability_version_id: string;
      };
      state.lastBind = { body };
      state.lastMutation = { method, path, headers: req.headers() };
      const capVersion = state.capVersions.find((v) => v.id === body.capability_version_id);
      if (!capVersion) return fulfill(route, 404, { detail: "capability version not found" });
      if (capVersion.status !== "published") {
        // 机器可读拒绝面（api/capabilities.py create_binding 409 原文）
        return fulfill(route, 409, {
          detail: "can only bind published capability versions",
        });
      }
      state.seq.binding += 1;
      const binding: BindingRecord = {
        id: newSeqId(3, state.seq.binding),
        organization_id: ORG_ID,
        workspace_id: WS_ID,
        agent_definition_id: body.agent_definition_id,
        agent_version_id: body.agent_version_id,
        capability_version_id: body.capability_version_id,
        status: "active",
      };
      state.bindings.push(binding);
      return fulfill(route, 201, binding);
    }
    const bindingMatch = path.match(/^\/api\/v1\/capabilities\/bindings\/([0-9a-f-]{36})$/);
    if (bindingMatch && method === "DELETE") {
      const before = state.bindings.length;
      state.bindings = state.bindings.filter((b) => b.id !== bindingMatch[1]);
      if (state.bindings.length === before) {
        return fulfill(route, 404, { detail: "binding not found" });
      }
      return fulfill(route, 204, null);
    }

    // ------------------------------------------------------------------
    // Connections（api/connections.py 契约）
    // ------------------------------------------------------------------
    if (path === "/api/v1/connections" && method === "GET") {
      return fulfill(route, 200, state.connections);
    }
    if (path === "/api/v1/connections" && method === "POST") {
      const body = req.postDataJSON() as {
        provider_version_id: string;
        subject_mode?: string;
      };
      state.lastMutation = { method, path, headers: req.headers() };
      state.seq.connection += 1;
      const connection: ConnectionRecord = {
        id: newSeqId(4, state.seq.connection),
        organization_id: ORG_ID,
        workspace_id: WS_ID,
        provider_version_id: body.provider_version_id,
        subject_mode: body.subject_mode ?? "workspace_service",
        status: "active",
        principal_id: null,
        version: 1,
        fingerprint: digest(`connection-${state.seq.connection}`),
      };
      state.connections.push(connection);
      return fulfill(route, 201, connection);
    }
    const connMatch = path.match(/^\/api\/v1\/connections\/([0-9a-f-]{36})$/);
    if (connMatch && method === "GET") {
      const connection = state.connections.find((c) => c.id === connMatch[1]);
      if (!connection) return fulfill(route, 404, { detail: "connection not found" });
      return fulfill(route, 200, connection);
    }
    const connStatus = path.match(/^\/api\/v1\/connections\/([0-9a-f-]{36})\/status$/);
    if (connStatus && method === "GET") {
      const connection = state.connections.find((c) => c.id === connStatus[1]);
      if (!connection) return fulfill(route, 404, { detail: "connection not found" });
      // credential_status 为不透明投影（api/connections.py：active 之外的
      // 状态逐字透出）
      return fulfill(route, 200, {
        connection_id: connection.id,
        status: connection.status,
        fingerprint: connection.fingerprint,
        credential_status:
          connection.status === "active" ? "active" : connection.status,
      });
    }
    const connAction = path.match(/^\/api\/v1\/connections\/([0-9a-f-]{36})\/actions$/);
    if (connAction && method === "POST") {
      const connection = state.connections.find((c) => c.id === connAction[1]);
      if (!connection) return fulfill(route, 404, { detail: "connection not found" });
      const body = req.postDataJSON() as { action?: string };
      state.lastMutation = { method, path, headers: req.headers() };
      const transitions: Record<string, string> = { suspend: "suspended", revoke: "revoked" };
      if (!body.action || !(body.action in transitions)) {
        return fulfill(route, 422, { detail: `unknown action: ${body.action ?? ""}` });
      }
      if (connection.status === "revoked") {
        // 结构化失败（api/connections.py 409 原文）
        return fulfill(route, 409, { detail: "cannot act on revoked connection" });
      }
      connection.status = transitions[body.action];
      connection.version += 1;
      return fulfill(route, 200, connection);
    }

    // 未模拟路径显式失败（fail loud，不静默穿透 dev proxy）
    return fulfill(route, 500, { detail: `unmocked: ${method} ${path}` });
  });
}

async function newContextWithMocks(
  browser: Browser,
  state: MockState,
  actor: Actor
): Promise<BrowserContext> {
  const context = await browser.newContext();
  installApiMocks(context, state, actor);
  return context;
}

async function openSection(page: Page, section: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByRole("button", { name: section, exact: true }).click();
  await expect(page.getByRole("heading", { name: section, exact: true })).toBeVisible();
}

// ---------------------------------------------------------------------------
// (1) Publisher journey：导入 → 检视（元数据逐字）→ 批准（admit→publish，能力
//     版本跟随）→ 版本检视 + diff → 创建 Workspace Connection 并 test
// ---------------------------------------------------------------------------

test.describe("S4 publisher journey", () => {
  test("imports, inspects verbatim, approves, publishes, connects and tests connection", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "publisher");
    const page = await context.newPage();

    await openSection(page, "Capabilities");

    // 导入（POST providers → 201；mutation PEP 契约：CSRF + Idempotency-Key）
    await page.getByRole("button", { name: "Import provider" }).click();
    await page.getByLabel("Provider name").fill("github-mcp");
    await page.getByLabel("Source URL").fill("https://registry.example/github-mcp");
    await page.getByRole("button", { name: "Register provider" }).click();
    const providerId = newSeqId(1, 1);
    const capVersionId = newSeqId(11, 1);
    const providerRow = page
      .getByRole("table", { name: "Providers" })
      .getByRole("row", { name: new RegExp(providerId) });
    await expect(providerRow).toContainText("github-mcp");
    await expect(providerRow).toContainText("discovered");
    expect(state.lastMutation).not.toBeNull();
    expect(state.lastMutation!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastMutation!.headers["idempotency-key"])).toBe(true);

    // 检视：元数据逐字（API 投影字段；source/url/classification/risk/digest）
    await providerRow.getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Provider" })).toBeVisible();
    await expect(page.getByText("classification: PUBLIC")).toBeVisible();
    await expect(page.getByText("risk level: low")).toBeVisible();
    await expect(page.getByText("content digest: sha256:")).toBeVisible();
    await expect(
      page.getByText("source url: https://registry.example/github-mcp")
    ).toBeVisible();

    // 批准：admit → approved → publish → published（单一生命周期入口序列）
    await page.getByRole("button", { name: "Admit" }).click();
    await expect(page.getByText("status: approved")).toBeVisible();
    await page.getByRole("button", { name: "Publish" }).click();
    await expect(page.getByText("status: published")).toBeVisible();

    // 能力版本状态跟随 provider（受影响版本投影的列表面）
    await page.getByRole("button", { name: "Back" }).click();
    await expect(
      page
        .getByRole("table", { name: "Capability versions" })
        .getByRole("row", { name: new RegExp(capVersionId) })
    ).toContainText("published");

    // 版本检视：真实投影字段逐字（test_digest 为 domain 默认 "" → 视图按
    // 「缺席 → unknown」呈现；metadata 逐字 JSON）
    await page
      .getByRole("table", { name: "Capability versions" })
      .getByRole("row", { name: new RegExp(capVersionId) })
      .getByRole("button", { name: "Inspect" })
      .click();
    await expect(page.getByRole("heading", { name: "Capability version" })).toBeVisible();
    await expect(page.getByText("capability type: provider")).toBeVisible();
    await expect(page.getByText("test digest: unknown")).toBeVisible();
    await expect(
      page.getByText(`metadata: {"provider_version_id":"${providerId}"}`)
    ).toBeVisible();

    // diff（v1 无前版本：from 0，三标志 true）
    await page.getByRole("button", { name: "Show diff" }).click();
    await expect(page.getByText("diff: v0 → v1")).toBeVisible();
    await expect(page.getByText("content changed: true")).toBeVisible();
    await expect(page.getByText("risk changed: true")).toBeVisible();
    await expect(page.getByText("status changed: true")).toBeVisible();
    // 无 SBOM/漏洞数据即不渲染（不发明检查项）
    await expect(page.getByText(/sbom/i)).toHaveCount(0);
    await expect(page.getByText(/vulnerability/i)).toHaveCount(0);

    // 创建 Workspace Connection 并 test（status 投影 = API 提供的 test 面：
    // fingerprint + credential_status）
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Create connection" }).click();
    await page.getByLabel("Provider version id").fill(providerId);
    await page.getByRole("button", { name: "Confirm connection" }).click();
    const connectionId = newSeqId(4, 1);
    const connectionRow = page
      .getByRole("table", { name: "Connections" })
      .getByRole("row", { name: new RegExp(connectionId) });
    await expect(connectionRow).toContainText("workspace_service");
    await expect(connectionRow).toContainText("active");
    await connectionRow.getByRole("button", { name: "Status" }).click();
    await expect(page.getByText(`fingerprint: ${digest("connection-1")}`)).toBeVisible();
    await expect(page.getByText("credential: active")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (2) Builder journey：无导入/lifecycle 控件（角色显隐）；绑定 published 版本
//     成功；绑定未发布版本被 409 拒——机器可读原因原样上浮
// ---------------------------------------------------------------------------

test.describe("S4 builder journey", () => {
  test("binds only the published version; unpublished bind refused with machine reason", async ({ browser }) => {
    const state = newState();
    // 种子先于分区挂载（CapabilitiesView 挂载即拉取版本列表）：
    // published 版本可绑定；discovered 版本绑定必被拒
    const publishedVersion = newSeqId(11, 1);
    const draftVersion = newSeqId(11, 2);
    state.providers.push(
      {
        id: newSeqId(1, 1),
        provider_id: newSeqId(2, 1),
        name: "github-mcp",
        version: 1,
        description: "",
        status: "published",
        classification: "PUBLIC",
        source_url: "https://registry.example/github-mcp",
        risk_level: "low",
        content_digest: digest("provider-1"),
      },
      {
        id: newSeqId(1, 2),
        provider_id: newSeqId(2, 2),
        name: "jira-mcp",
        version: 1,
        description: "",
        status: "discovered",
        classification: "PUBLIC",
        source_url: null,
        risk_level: "low",
        content_digest: digest("provider-2"),
      }
    );
    state.capVersions.push(
      {
        id: publishedVersion,
        capability_type: "provider",
        name: "github-mcp",
        version: 1,
        status: "published",
        risk_level: "low",
        content_digest: digest("provider-1"),
        test_digest: "",
        parent_id: null,
        metadata: { provider_version_id: newSeqId(1, 1) },
      },
      {
        id: draftVersion,
        capability_type: "provider",
        name: "jira-mcp",
        version: 1,
        status: "discovered",
        risk_level: "low",
        content_digest: digest("provider-2"),
        test_digest: "",
        parent_id: null,
        metadata: { provider_version_id: newSeqId(1, 2) },
      }
    );
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();

    await openSection(page, "Capabilities");

    // 角色显隐：builder 无导入与 lifecycle 入口（权限由 server PEP 强制）
    await expect(page.getByRole("button", { name: "Import provider" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Admit" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Publish" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Suspend" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Revoke" })).toHaveCount(0);

    // 绑定 published 版本 → 201 → bindings 表呈现
    await page.getByRole("button", { name: "Bind capability" }).click();
    await page.getByLabel("Capability version id").fill(publishedVersion);
    await page.getByLabel("Agent definition id").fill(newSeqId(9, 1));
    await page.getByLabel("Agent version id").fill(newSeqId(10, 1));
    await page.getByRole("button", { name: "Create binding" }).click();
    await expect(
      page
        .getByRole("table", { name: "Bindings" })
        .getByRole("row", { name: new RegExp(publishedVersion) })
    ).toContainText("active");
    expect(state.lastBind).not.toBeNull();
    expect(state.lastBind!.body.capability_version_id).toBe(publishedVersion);

    // 绑定未发布版本 → 409 机器可读原因原样上浮（S4 契约）
    await page.getByRole("button", { name: "Bind capability" }).click();
    await page.getByLabel("Capability version id").fill(draftVersion);
    await page.getByLabel("Agent definition id").fill(newSeqId(9, 2));
    await page.getByLabel("Agent version id").fill(newSeqId(10, 2));
    await page.getByRole("button", { name: "Create binding" }).click();
    await expect(
      page.getByText("can only bind published capability versions")
    ).toBeVisible();
    expect(state.lastBind!.body.capability_version_id).toBe(draftVersion);
    // 被拒绑定不得入库
    expect(
      state.bindings.filter((b) => b.capability_version_id === draftVersion)
    ).toHaveLength(0);

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (3) Security Admin journey（纯 security_admin 主体，与 publisher 分离）：
//     suspend/revoke → provider 状态投影 + 受影响能力版本清单；受影响版本对
//     builder 呈结构化失败（绑定 409）；connection suspend/revoke → 复动作 409
// ---------------------------------------------------------------------------

test.describe("S4 security admin journey", () => {
  test("suspends and revokes with affected versions shown; structured failures surface", async ({ browser }) => {
    const state = newState();
    const providerId = newSeqId(1, 1);
    const capVersionId = newSeqId(11, 1);
    state.providers.push({
      id: providerId,
      provider_id: newSeqId(2, 1),
      name: "github-mcp",
      version: 1,
      description: "",
      status: "published",
      classification: "PUBLIC",
      source_url: "https://registry.example/github-mcp",
      risk_level: "low",
      content_digest: digest("provider-1"),
    });
    state.capVersions.push({
      id: capVersionId,
      capability_type: "provider",
      name: "github-mcp",
      version: 1,
      status: "published",
      risk_level: "low",
      content_digest: digest("provider-1"),
      test_digest: "",
      parent_id: null,
      metadata: { provider_version_id: providerId },
    });
    state.connections.push({
      id: newSeqId(4, 1),
      organization_id: ORG_ID,
      workspace_id: WS_ID,
      provider_version_id: providerId,
      subject_mode: "workspace_service",
      status: "active",
      principal_id: null,
      version: 1,
      fingerprint: digest("connection-1"),
    });

    // Security Admin：suspend/revoke 主体
    const secadminContext = await newContextWithMocks(browser, state, "security_admin");
    const page = await secadminContext.newPage();
    await openSection(page, "Capabilities");
    await page
      .getByRole("table", { name: "Providers" })
      .getByRole("row", { name: new RegExp(providerId) })
      .getByRole("button", { name: "Open" })
      .click();

    // 纯 security_admin：无发布面入口（S4 §6 双主体分离）
    await expect(page.getByRole("button", { name: "Admit" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Publish" })).toHaveCount(0);

    // suspend → provider 状态投影 + 受影响版本清单（版本状态跟随 provider）
    await page.getByRole("button", { name: "Suspend" }).click();
    await expect(page.getByText("status: suspended")).toBeVisible();
    const affected = page.getByRole("list", { name: "Affected versions" });
    await expect(affected.getByText(`${capVersionId}: suspended`)).toBeVisible();

    // revoke → 受影响版本投影更新为 revoked
    await page.getByRole("button", { name: "Revoke" }).click();
    await expect(page.getByText("status: revoked")).toBeVisible();
    await expect(affected.getByText(`${capVersionId}: revoked`)).toBeVisible();

    // connection suspend/revoke；revoked 后复动作为结构化失败（409 原文）
    await page.getByRole("button", { name: "Back" }).click();
    const connectionRow = page
      .getByRole("table", { name: "Connections" })
      .getByRole("row", { name: new RegExp(newSeqId(4, 1)) });
    await connectionRow.getByRole("button", { name: "Suspend" }).click();
    await expect(connectionRow).toContainText("suspended");
    await connectionRow.getByRole("button", { name: "Status" }).click();
    await expect(page.getByText("credential: suspended")).toBeVisible();
    await connectionRow.getByRole("button", { name: "Revoke" }).click();
    await expect(connectionRow).toContainText("revoked");
    await connectionRow.getByRole("button", { name: "Suspend" }).click();
    await expect(page.getByText("cannot act on revoked connection")).toBeVisible();
    await secadminContext.close();

    // Builder（同一 mock 域）：受影响版本列表呈 revoked；绑定被结构化拒绝
    const builderContext = await newContextWithMocks(browser, state, "builder");
    const builderPage = await builderContext.newPage();
    await openSection(builderPage, "Capabilities");
    await expect(
      builderPage
        .getByRole("table", { name: "Capability versions" })
        .getByRole("row", { name: new RegExp(capVersionId) })
    ).toContainText("revoked");
    await builderPage.getByRole("button", { name: "Bind capability" }).click();
    await builderPage.getByLabel("Capability version id").fill(capVersionId);
    await builderPage.getByLabel("Agent definition id").fill(newSeqId(9, 1));
    await builderPage.getByLabel("Agent version id").fill(newSeqId(10, 1));
    await builderPage.getByRole("button", { name: "Create binding" }).click();
    await expect(
      builderPage.getByText("can only bind published capability versions")
    ).toBeVisible();
    await builderContext.close();
  });
});
