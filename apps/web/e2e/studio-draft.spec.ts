// S10-T2 Studio draft 编辑 e2e（specs/s10 §3 → plan Task 2）。
//
// 与后端契约的映射（网络层 mock，形状逐字段对齐 src/zhiwei/api/agents.py 投影，
// 冻结契约 tests/contract/api/test_agents_studio_frozen.py 的 mock 侧镜像）：
//   POST /api/v1/agents                    → 201 + ETag + revision（draft 创建）
//   GET  /api/v1/agents                    → AgentDraft 列表（既有消费者为零，
//                                            T2 起为 Studio 列表面）
//   GET  /api/v1/agents/{id}               → 200 + ETag（CAS 前置读）
//   PUT  /api/v1/agents/{id}               → If-Match CAS：200 + 新 revision /
//                                            412 {reason:"revision_conflict"} /
//                                            428 {reason:"if_match_required"}
//   POST /api/v1/agents/{id}/validate      → 200 {issues:[{code,task_id,field,
//                                            detail}]}（validate_studio_graph 语义）
//   POST /api/v1/agents/{id}/releases      → 201 ReleaseView（S9 ReleaseService
//                                            draft；角色映射 agent_builder→builder）
//
// 纪律：
// - 不发任何真实外部请求；未模拟的 /api 路径一律 500 显式失败（fail loud）。
// - catch-all 路由 glob 根锚定 "/api/**"（S10-T2 归位：实现本体回 src/api/client.ts，
//   `**` 跨 "/" 会把模块脚本 /src/api/* 一并截成 JSON——见 lib/api.ts 偏差登记）。
// - draft 可为非法中间态：validate 只报告 issue 不阻塞保存（保存合法性由 T3
//   release Gate 把关）；mock 的 validate 语义对齐 validate_studio_graph 的
//   capability/budget/port 判定子集。

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
const BUILDER_ID = "3383f6a7-d17b-44c2-802c-d67c3974e13a";
const AUDITOR_ID = "5b4f9e2a-1c3d-4e5f-8a6b-7c8d9e0f1a2b";
const AGENT_ID = "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c99";
const RELEASE_ID = "9f8e7d6c-5b4a-3c2d-1e0f-a1b2c3d4e5f";
const CSRF = "e2e-csrf-token";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Core primitives 词汇（镜像 src/zhiwei/agents/task_graph.py TaskPrimitive）
const PRIMITIVES = [
  "Intake",
  "Plan",
  "Clarify",
  "Retrieve",
  "Analyze",
  "InvokeTool",
  "Delegate",
  "Verify",
  "RequestApproval",
  "Synthesize",
  "EmitArtifact",
  "WriteMemoryCandidate",
  "Finish",
] as const;

// api/agents.py AgentDraft 投影（task_graph 节点 = TaskGraphNode 的 UI 子集）
interface StudioTaskNode {
  task_id: string;
  task_type: string;
  dependencies: string[];
  required_capability: string;
  budget: Record<string, number>;
  input_schema: { properties?: Record<string, { type: string }> };
  output_schema: { properties?: Record<string, { type: string }> };
}

interface AgentDraft {
  agent_id: string;
  name: string;
  description: string;
  instructions: string;
  capabilities: string[];
  task_graph: { tasks: StudioTaskNode[]; edges: [string, string][] } | null;
  revision: number;
  lifecycle: string;
  updated_at: string;
}

interface MockState {
  drafts: AgentDraft[];
  validateRequests: number;
  saves: { headers: Record<string, string>; body: unknown }[];
  conflictNext: boolean;
  releases: { headers: Record<string, string>; body: unknown }[];
}

function emptyDraft(): AgentDraft {
  return {
    agent_id: AGENT_ID,
    name: "studio-agent",
    description: "S10 studio journey",
    instructions: "",
    capabilities: ["knowledge.retrieve@1"],
    task_graph: null,
    revision: 1,
    lifecycle: "draft",
    updated_at: "2026-09-06T00:00:00+00:00",
  };
}

function newState(): MockState {
  return {
    drafts: [],
    validateRequests: 0,
    saves: [],
    conflictNext: false,
    releases: [],
  };
}

// validate mock：对齐 validate_studio_graph 的判定子集（capability/budget）。
// draft 的 declared capabilities 是唯一事实源——不在声明集内的 required_capability
// 报 unknown_capability，与 tests/unit/agents/test_studio_validation_frozen.py 同语义。
function validateGraph(
  body: { tasks?: StudioTaskNode[] },
  declared: string[],
): { code: string; task_id: string; field: string; detail: string }[] {
  const issues: { code: string; task_id: string; field: string; detail: string }[] = [];
  for (const task of body.tasks ?? []) {
    if (!declared.includes(task.required_capability)) {
      issues.push({
        code: "unknown_capability",
        task_id: task.task_id,
        field: "required_capability",
        detail: `capability ${task.required_capability} is not declared on this agent`,
      });
    }
    for (const key of Object.keys(task.budget ?? {})) {
      if (!["max_model_calls", "max_tokens", "max_usd_micros"].includes(key)) {
        issues.push({
          code: "unknown_budget_key",
          task_id: task.task_id,
          field: "budget",
          detail: `budget key ${key} is not part of the studio budget vocabulary`,
        });
      }
    }
  }
  return issues;
}

function etagFor(draft: AgentDraft): string {
  return `"${draft.revision}"`;
}

function draftBody(draft: AgentDraft): Record<string, unknown> {
  return { ...draft };
}

type Actor = "builder" | "auditor";

const ACTOR_PRINCIPAL: Record<Actor, string> = {
  builder: BUILDER_ID,
  auditor: AUDITOR_ID,
};

// 网络层 mock：单一 catch-all 路由内部分发（根锚定 glob，模块脚本不受拦截）
function installStudioMocks(context: BrowserContext, state: MockState, actor: Actor): void {
  const fulfill = (
    route: Route,
    status: number,
    body: unknown,
    headers?: Record<string, string>,
  ) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
      headers,
    });

  context.route("/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();

    // 会话引导（session.tsx 消费的真实契约）
    if (path === "/api/v1/me" && method === "GET") {
      return fulfill(route, 200, {
        principal: { id: ACTOR_PRINCIPAL[actor] },
        organizations: [{ id: ORG_ID, status: "active" }],
        context: { organization_id: ORG_ID, workspace_id: WS_ID },
        csrf_token: CSRF,
      });
    }
    if (path === "/api/v1/organizations" && method === "GET") {
      return fulfill(route, 200, [{ id: ORG_ID, status: "active" }]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/members` && method === "GET") {
      const roleBindings =
        actor === "builder"
          ? [{ principal_id: BUILDER_ID, organization_id: ORG_ID, role_bindings: ["builder"] }]
          : [{ principal_id: AUDITOR_ID, organization_id: ORG_ID, role_bindings: ["auditor"] }];
      return fulfill(route, 200, roleBindings);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    if (path === `/api/v1/workspaces/${WS_ID}/groups` && method === "GET") {
      return fulfill(route, 200, []);
    }

    // ---- agents draft 面（api/agents.py 契约镜像）----
    if (path === "/api/v1/agents" && method === "GET") {
      return fulfill(route, 200, state.drafts.map(draftBody));
    }
    if (path === "/api/v1/agents" && method === "POST") {
      const body = req.postDataJSON() as {
        name: string;
        description?: string;
        instructions?: string;
        capabilities?: string[];
      };
      const draft: AgentDraft = {
        agent_id: AGENT_ID,
        name: body.name,
        description: body.description ?? "",
        instructions: body.instructions ?? "",
        capabilities: body.capabilities ?? [],
        task_graph: null,
        revision: 1,
        lifecycle: "draft",
        updated_at: "2026-09-06T00:00:00+00:00",
      };
      state.drafts.push(draft);
      return fulfill(route, 201, draftBody(draft), { ETag: etagFor(draft) });
    }
    const agentMatch = path.match(/^\/api\/v1\/agents\/([0-9a-f-]{36})$/);
    if (agentMatch && method === "GET") {
      const draft = state.drafts.find((d) => d.agent_id === agentMatch[1]);
      if (!draft) return fulfill(route, 404, { detail: "agent not found" });
      return fulfill(route, 200, draftBody(draft), { ETag: etagFor(draft) });
    }
    if (agentMatch && method === "PUT") {
      const draft = state.drafts.find((d) => d.agent_id === agentMatch[1]);
      if (!draft) return fulfill(route, 404, { detail: "agent not found" });
      state.saves.push({ headers: req.headers(), body: req.postDataJSON() });
      const ifMatch = req.headers()["if-match"];
      if (!ifMatch) {
        return fulfill(route, 428, {
          reason: "if_match_required",
          message: "If-Match header required for draft writes",
        });
      }
      if (state.conflictNext || ifMatch !== etagFor(draft)) {
        state.conflictNext = false;
        return fulfill(route, 412, {
          reason: "revision_conflict",
          message: "stale revision",
        });
      }
      const body = req.postDataJSON() as Partial<AgentDraft>;
      if (body.name !== undefined) draft.name = body.name;
      if (body.description !== undefined) draft.description = body.description;
      if (body.instructions !== undefined) draft.instructions = body.instructions;
      if (body.capabilities !== undefined) draft.capabilities = body.capabilities;
      if (body.task_graph !== undefined) draft.task_graph = body.task_graph;
      draft.revision += 1;
      return fulfill(route, 200, draftBody(draft), { ETag: etagFor(draft) });
    }
    const validateMatch = path.match(/^\/api\/v1\/agents\/([0-9a-f-]{36})\/validate$/);
    if (validateMatch && method === "POST") {
      state.validateRequests += 1;
      const draft = state.drafts.find((d) => d.agent_id === validateMatch[1]);
      const body = req.postDataJSON() as { tasks?: StudioTaskNode[] };
      const issues = validateGraph(body, draft?.capabilities ?? []);
      return fulfill(route, 200, { issues });
    }
    const releaseMatch = path.match(/^\/api\/v1\/agents\/([0-9a-f-]{36})\/releases$/);
    if (releaseMatch && method === "POST") {
      state.releases.push({ headers: req.headers(), body: req.postDataJSON() });
      const body = req.postDataJSON() as Record<string, unknown>;
      const digestKeys = [
        "pack_digest",
        "model_digest",
        "knowledge_digest",
        "memory_digest",
        "capability_digest",
        "policy_digest",
      ];
      const digestRe = /^sha256:[0-9a-f]{64}$/;
      for (const key of digestKeys) {
        if (typeof body[key] !== "string" || !digestRe.test(body[key] as string)) {
          return fulfill(route, 422, { detail: `${key} must be a sha256 digest` });
        }
      }
      return fulfill(
        route,
        201,
        {
          release_id: RELEASE_ID,
          agent_id: releaseMatch[1],
          agent_version: 1,
          state: "draft",
          manifest_digest: "sha256:" + "9".repeat(64),
          default_version: 1,
        },
      );
    }

    // 未模拟路径显式失败（fail loud，不静默穿透 dev proxy）
    return fulfill(route, 500, { detail: `unmocked: ${method} ${path}` });
  });
}

async function newContextWithMocks(
  browser: Browser,
  state: MockState,
  actor: Actor,
): Promise<BrowserContext> {
  const context = await browser.newContext();
  installStudioMocks(context, state, actor);
  return context;
}

// 完整 13 分区标题（specs/s10 §3；Release 归 T3，以占位如实呈现）
const SECTION_HEADINGS = [
  "Overview",
  "Instructions",
  "Knowledge",
  "Memory",
  "Tools",
  "Task",
  "Triggers",
  "Model",
  "Budget",
  "Evidence",
  "Evals",
  "Access",
];

async function createDraftViaUi(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByRole("button", { name: "Agent Studio" }).click();
  await page.getByLabel("Name").fill("studio-agent");
  await page.getByLabel("Description").fill("S10 studio journey");
  await page.getByLabel("Declared capabilities").fill("knowledge.retrieve@1");
  await page.getByRole("button", { name: "Create draft" }).click();
  for (const heading of SECTION_HEADINGS) {
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
  }
  // Release 分区：T3 起为完整发布流（readiness + S9 release commands 接管 T2
  // 的诚实最小面——占位文本随占位一起移除，plan Task 3 交接口）
  await expect(page.getByRole("heading", { name: "Release", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Check readiness" })).toBeVisible();
}

// ---------------------------------------------------------------------------
// (a) 创建 draft → 13 分区如实渲染
// ---------------------------------------------------------------------------

test.describe("S10 studio — draft creation", () => {
  test("builder creates a draft and all studio sections render", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();
    await createDraftViaUi(page);

    // draft 状态可见：revision 1 + lifecycle draft（来自真实 API 投影）
    await expect(page.getByText(/Revision 1/)).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (b) Instructions/Task 编辑 + 实时校验：unknown_capability 出现后消失
// ---------------------------------------------------------------------------

test.describe("S10 studio — constrained task editor", () => {
  test("live validation reports unknown_capability for a bad capability and clears", async ({
    browser,
  }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();
    await createDraftViaUi(page);

    await page.getByLabel("Instructions").fill("Verify the brief before synthesis.");

    // Task 分区：受约束编辑器——只允许 Core primitives
    await page.getByRole("button", { name: "Add node" }).click();
    await expect(page.getByText("t1", { exact: true })).toBeVisible();
    const typeSelect = page.getByLabel("Task type", { exact: false }).first();
    for (const primitive of PRIMITIVES) {
      await expect(typeSelect.locator(`option[value="${primitive}"]`)).toHaveCount(1);
    }
    await typeSelect.selectOption("Retrieve");

    // 未声明 capability → 实时校验报 unknown_capability（debounced）
    await page.getByLabel("Required capability").fill("repo.destroy@9");
    await expect(page.getByText(/unknown_capability/)).toBeVisible();
    await expect(page.getByText(/repo\.destroy@9/).first()).toBeVisible();

    // 预算（3 键词汇）+ typed ports 编辑
    await page.getByLabel("Max model calls").fill("2");
    await page.getByRole("button", { name: "Add output port" }).click();
    await page.getByLabel("Port name").fill("brief");
    await page.getByLabel("Port type").selectOption("object");

    // 修正 capability → issue 清空
    await page.getByLabel("Required capability").fill("knowledge.retrieve@1");
    await expect(page.getByText(/unknown_capability/)).toHaveCount(0);

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (c) CAS：412 冲突 → 冲突横幅 + Reload → 保存成功
// ---------------------------------------------------------------------------

test.describe("S10 studio — CAS conflict", () => {
  test("stale save surfaces 412 banner; reload then save succeeds", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();
    await createDraftViaUi(page);

    // 首次保存成功：revision 1 → 2，If-Match 携带当前 ETag
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByText(/Saved revision 2/)).toBeVisible();

    // 服务器侧进入冲突（mock 注入：他人已推进 revision）
    await page.getByLabel("Instructions").fill("Second edit before the conflict.");
    state.conflictNext = true;
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByText(/revision_conflict/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Reload draft" })).toBeVisible();

    // Reload 拉回服务器最新 revision（CAS 前置：先读后写）→ 再次保存成功
    await page.getByRole("button", { name: "Reload draft" }).click();
    await expect(page.getByText(/Revision 2/)).toBeVisible();
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByText(/Saved revision 3/)).toBeVisible();

    // CAS 契约：每次保存都带 If-Match（428 在 UI 侧不可达）。三次保存：
    // 首存成功、冲突 412、reload 后成功——全部必须携带 If-Match。
    expect(state.saves).toHaveLength(3);
    for (const save of state.saves) {
      expect(save.headers["if-match"]).toBeDefined();
      expect(UUID_RE.test(save.headers["idempotency-key"])).toBe(true);
      expect(save.headers["x-csrf-token"]).toBe(CSRF);
    }

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (d) 角色门禁：auditor 只读（编辑器禁用 + 无创建入口）
// ---------------------------------------------------------------------------

test.describe("S10 studio — role gating", () => {
  test("auditor sees sections read-only with mutation controls disabled", async ({ browser }) => {
    const state = newState();
    state.drafts.push(emptyDraft());
    const context = await newContextWithMocks(browser, state, "auditor");
    const page = await context.newPage();

    await page.goto("/");
    await page.getByRole("button", { name: "Agent Studio" }).click();
    await page.getByRole("row", { name: new RegExp(AGENT_ID) }).getByRole("button", { name: "Open" }).click();
    for (const heading of SECTION_HEADINGS) {
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    }
    await expect(page.getByText(/read-only/)).toBeVisible();
    await expect(page.getByLabel("Instructions")).toBeDisabled();
    await expect(page.getByRole("button", { name: "Save draft" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Create draft" })).toHaveCount(0);

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (e) 经 Studio 创建 release（S9 release commands，无旁路状态机）
// ---------------------------------------------------------------------------

test.describe("S10 studio — release via studio", () => {
  test("builder creates a draft release and the release id is surfaced", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();
    await createDraftViaUi(page);

    // 六个依赖 digest 显式提供（T3 接管真实 digest 管线前的诚实最小面）
    for (const label of [
      "Pack digest",
      "Model digest",
      "Knowledge digest",
      "Memory digest",
      "Capability digest",
      "Policy digest",
    ]) {
      await page.getByLabel(label).fill("sha256:" + "a".repeat(64));
    }
    await page.getByRole("button", { name: "Create draft release" }).click();
    await expect(page.getByText(RELEASE_ID)).toBeVisible();
    expect(state.releases).toHaveLength(1);
    // release 请求体契约：依赖 digest + rollout/rollback 计划（无 agent_id/agent_version
    // ——由 agent 记录派生，见 api/agents.py）
    const releaseBody = state.releases[0].body as Record<string, unknown>;
    expect(releaseBody["pack_digest"]).toBe("sha256:" + "a".repeat(64));
    expect(releaseBody["rollout"]).toEqual({ default_version: 1, cohorts: [] });
    expect(releaseBody["rollback"]).toEqual({ in_flight: "complete" });

    await context.close();
  });
});
