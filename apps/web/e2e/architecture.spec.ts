// S10-T1 架构 e2e（specs/s10 §2/§6 → plan Task 1）：前端架构机制 journey。
//
// 与机制层的映射（网络层 mock，形状逐字段对齐真实契约）：
//   GET  /api/v1/runs/{id}            → api/runs.py RunDetail（run_id/status/
//                                       organization_id/tasks）；template 字段
//                                       后端投影暂未下发（extra=forbid），mock
//                                       显式供给以证明 AppRunBinding 解析路径——
//                                       生产该字段缺席时通用槽位如实渲染
//                                       "No app binding"。
//   GET  /api/v1/runs/{id}/stream     → api/events.py SSE 帧：id: <sequence_no> +
//                                       data: {event_type,event_id,run_id,
//                                       task_id}；keepalive 是 ": " 注释帧。
//                                       mock 首连供给一帧后结束流（模拟断线），
//                                       断言重连携带 ?cursor=<最后 sequence>。
//   GET/PUT /api/v1/agents/{id}       → tests/contract/api/test_agents_studio_frozen.py
//                                       冻结的 draft CAS 语义：GET 带 ETag 头；
//                                       旧 If-Match 写 → 412 {reason:
//                                       "revision_conflict"}；无 If-Match 写 →
//                                       428 {reason: "if_match_required"}。
//                                       T1 阶段尚无 PUT 视图（Studio 是 Task 2），
//                                       经真实 api 客户端模块（page 内动态
//                                       import /src/lib/api.ts——app 模块图的
//                                       真实实现位置，见 lib/api.ts 偏差登记）
//                                       驱动类型化错误路径，不造 UI。
//   AppRunBinding ghost 绑定          → 经真实 renderers/registry 模块注册
//                                       （templateId 无已注册 manifest 的
//                                       appId），断言 fail-closed 的 honest
//                                       unknown 状态。
//
// 纪律：
// - 不发任何真实外部请求；未模拟的 /api 路径一律 500 显式失败（fail loud）。
// - 默认 journey 不变：live 订阅是 opt-in（默认关），断言默认状态下无 SSE 连接。

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
const CSRF = "e2e-csrf-token";

// template=ask-v1 → 绑定到已注册 renderer（机制 happy path + live 订阅载体）
const LIVE_RUN = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e10";
// template=ghost-pack → 绑定到未注册 appId（fail-closed honest unknown）
const GHOST_RUN = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e11";
// fix-B (e)：completed、无 template → 通用面板结构 + honest unknown 绑定。
// mode 显式供给（spec §6：执行模式 provenance 从 API 派生并可断言；字段名 =
// FIX-A RunDetail.mode）。
const PANEL_RUN = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e20";
// fix-B (f)：completed + template change-brief → VerifiedBrief 渲染旅程
const BRIEF_RUN = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e21";
// fix-B (g)：completed + template change-brief，但投影未携带 brief → 诚实 pending
const BRIEF_PENDING_RUN = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e22";
// 只作为 cost 反例数据存在：它的 reservation 不得出现在 PANEL_RUN 的 run 视图
const OTHER_RUN = "d0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e23";
// evaluate 的函数体按源码序列化在页面内执行，不能闭包外层常量——CAS 路径
// 以字面量内联在 evaluate 内，此常量只供 mock 路由使用。
const AGENT_ID = "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c99";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface RunDetailMock {
  run_id: string;
  status: string;
  organization_id: string;
  tasks: Record<string, { status: string; error: string | null }>;
  // FIX-A 后 RunDetail 下发 template（string | null）与执行模式 provenance；
  // null/缺席 = 前端如实渲染 unknown（spec §6：mode 从 API 派生）。
  template?: string | null;
  mode?: string | null;
}

// api/evidence.py RunEvidenceView 的 1:1 mock 形状（extra=forbid 契约，逐字段）
interface EvidenceRefMock {
  ref_type?: string;
  reproducibility_level?: string;
  file_path?: string;
  line_start?: number;
  line_end?: number;
  code_digest?: string;
  snapshot_digest?: string | null;
}

interface ClaimMock {
  claim_ref: string;
  claim_type: string | null;
  verified: boolean | null;
  quote_text: string | null;
  evidence_refs: EvidenceRefMock[];
  canonical_value: Record<string, unknown> | null;
}

interface EvidenceMock {
  run_id: string;
  run_status: string;
  answer_status: string | null;
  answer: Record<string, unknown>;
  claims: ClaimMock[];
  verified_claims: string[];
  failed_claims: string[];
  verification: Record<string, unknown> | null;
  unknowns: string[];
  clarification: Record<string, unknown> | null;
  findings: unknown[];
  conflicts: unknown[];
}

// api/observability.py CostSummary 的 1:1 mock 形状
interface CostReservationMock {
  reservation_id: string;
  run_id: string;
  amount_usd: string;
  price_source: string;
  price_confidence: string;
  created_at: string;
}

interface CostReconciliationMock {
  reservation_id: string;
  reserved_usd: string;
  actual_usd: string;
  variance_usd: string;
  retry_cost_usd: string;
  child_run_cost_usd: string;
  tool_external_cost_usd: string;
  created_at: string;
}

interface MockState {
  runs: { run_id: string; status: string; organization_id: string }[];
  details: Record<string, RunDetailMock>;
  evidence: Record<string, EvidenceMock>;
  costReservations: CostReservationMock[];
  costReconciliations: CostReconciliationMock[];
  runDetailRequests: number;
  sseRequests: string[];
  agentRequests: { method: string; headers: Record<string, string> }[];
}

// fix-B (f)：brief artifact 在 evidence 投影里的字段词汇 = 逐字段对齐
// solution-packs/change-brief/schemas/verified-brief.yaml（10 必需字段）。
const BRIEF = {
  affected_symbols: [
    {
      name: "analyze_impact",
      kind: "function",
      file_path: "src/impact.py",
      line_start: 10,
      line_end: 42,
    },
  ],
  affected_dependencies: [{ name: "fastapi", version_constraint: ">=0.110", impact: "direct" }],
  affected_tests: [
    {
      test_id: "tests/test_impact.py::test_analyze",
      path: "tests/test_impact.py",
      expected_status: "pass",
    },
  ],
  related_prs: [{ repository: "acme/widgets", pr_number: 14 }],
  related_issues: [{ repository: "acme/widgets", issue_number: 77 }],
  related_checks: [{ name: "ci-unit", status: "passed" }],
  risks: [{ description: "Verification failed; brief claims are unverified", severity: "high" }],
  unknowns: ["No coverage data for src/impact.py"],
  code_refs: [
    { file_path: "src/impact.py", line_start: 10, line_end: 42, code_digest: "sha256:deadbeef" },
  ],
  github_refs: [
    { repository: "acme/widgets", commit_sha: "abc1234", pr_number: 14, path: "src/impact.py" },
  ],
};

function newState(): MockState {
  return {
    runs: [
      { run_id: LIVE_RUN, status: "running", organization_id: ORG_ID },
      { run_id: GHOST_RUN, status: "running", organization_id: ORG_ID },
      { run_id: PANEL_RUN, status: "completed", organization_id: ORG_ID },
      { run_id: BRIEF_RUN, status: "completed", organization_id: ORG_ID },
      { run_id: BRIEF_PENDING_RUN, status: "completed", organization_id: ORG_ID },
    ],
    details: {
      [LIVE_RUN]: {
        run_id: LIVE_RUN,
        status: "running",
        organization_id: ORG_ID,
        tasks: { "task-a": { status: "completed", error: null } },
        template: "ask-v1",
      },
      [GHOST_RUN]: {
        run_id: GHOST_RUN,
        status: "running",
        organization_id: ORG_ID,
        tasks: {},
        template: "ghost-pack",
      },
      [PANEL_RUN]: {
        run_id: PANEL_RUN,
        status: "completed",
        organization_id: ORG_ID,
        tasks: {
          plan: { status: "completed", error: null },
          execute: { status: "completed", error: null },
        },
        template: null,
        mode: "fixture",
      },
      [BRIEF_RUN]: {
        run_id: BRIEF_RUN,
        status: "completed",
        organization_id: ORG_ID,
        tasks: {
          synthesize_brief: { status: "completed", error: null },
          emit_brief: { status: "completed", error: null },
        },
        template: "change-brief",
        mode: "fixture",
      },
      [BRIEF_PENDING_RUN]: {
        run_id: BRIEF_PENDING_RUN,
        status: "completed",
        organization_id: ORG_ID,
        tasks: { synthesize_brief: { status: "completed", error: null } },
        template: "change-brief",
        mode: "fixture",
      },
    },
    evidence: {
      // 无绑定 run 的通用 Evidence 面板输入：verified/failed 各一条 + CodeRef
      // locator（digest 常显）+ unknown。形状 = api/evidence.py RunEvidenceView。
      [PANEL_RUN]: {
        run_id: PANEL_RUN,
        run_status: "completed",
        answer_status: null,
        answer: {},
        claims: [
          {
            claim_ref: "claim:verified-fact",
            claim_type: "Fact",
            verified: true,
            quote_text: "plan executed deterministically",
            evidence_refs: [
              {
                ref_type: "CodeRef",
                reproducibility_level: "replayable",
                file_path: "src/plan.py",
                line_start: 3,
                line_end: 7,
                code_digest: "sha256:cafe",
                snapshot_digest: null,
              },
            ],
            canonical_value: { type: "text", value: "plan executed deterministically" },
          },
          {
            claim_ref: "claim:failed-fact",
            claim_type: "Quote",
            verified: false,
            quote_text: null,
            evidence_refs: [],
            canonical_value: null,
          },
        ],
        verified_claims: ["claim:verified-fact"],
        failed_claims: ["claim:failed-fact"],
        verification: null,
        unknowns: ["upstream schema not retrievable"],
        clarification: null,
        findings: [],
        conflicts: [],
      },
      // brief run：brief artifact 以结构化 claim 载荷出现（canonical_value 携带
      // verified-brief 字段词汇——evidence 投影的通用 dict 字段，无需新端点）
      [BRIEF_RUN]: {
        run_id: BRIEF_RUN,
        run_status: "completed",
        answer_status: null,
        answer: {},
        claims: [
          {
            claim_ref: "claim:verified-brief",
            claim_type: "Fact",
            verified: true,
            quote_text: null,
            evidence_refs: [],
            canonical_value: BRIEF,
          },
        ],
        verified_claims: ["claim:verified-brief"],
        failed_claims: [],
        verification: { verification_ok: true, exit_code: 0, check_count: 2 },
        unknowns: [],
        clarification: null,
        findings: [],
        conflicts: [],
      },
      // completed 但投影未携带 brief → renderer 必须停在诚实 pending
      [BRIEF_PENDING_RUN]: {
        run_id: BRIEF_PENDING_RUN,
        run_status: "completed",
        answer_status: null,
        answer: {},
        claims: [],
        verified_claims: [],
        failed_claims: [],
        verification: null,
        unknowns: [],
        clarification: null,
        findings: [],
        conflicts: [],
      },
    },
    costReservations: [
      {
        reservation_id: "res-panel-run",
        run_id: PANEL_RUN,
        amount_usd: "0.0000042",
        price_source: "fixture",
        price_confidence: "exact",
        created_at: "2026-09-06T00:00:00+00:00",
      },
      {
        reservation_id: "res-other-run",
        run_id: OTHER_RUN,
        amount_usd: "1.5000000",
        price_source: "fixture",
        price_confidence: "exact",
        created_at: "2026-09-06T00:00:00+00:00",
      },
    ],
    costReconciliations: [
      {
        reservation_id: "res-panel-run",
        reserved_usd: "0.0000042",
        actual_usd: "0.0000041",
        variance_usd: "-0.0000001",
        retry_cost_usd: "0",
        child_run_cost_usd: "0",
        tool_external_cost_usd: "0",
        created_at: "2026-09-06T00:00:00+00:00",
      },
    ],
    runDetailRequests: 0,
    sseRequests: [],
    agentRequests: [],
  };
}

// api/events.py _sse 的帧形状：id: <sequence> + data: <json 元数据>
function sseFrame(sequence: number, eventType: string, taskId: string | null): string {
  return `id: ${sequence}\ndata: ${JSON.stringify({
    event_type: eventType,
    event_id: `1a2b3c4d-0000-4000-8000-00000000000${sequence}`,
    run_id: LIVE_RUN,
    task_id: taskId,
  })}\n\n`;
}

function installApiMocks(context: BrowserContext, state: MockState): void {
  const fulfill = (route: Route, status: number, body: unknown, headers?: Record<string, string>) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body), headers });

  context.route("/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();

    // 会话引导（session.tsx 消费的真实契约）
    if (path === "/api/v1/me" && method === "GET") {
      return fulfill(route, 200, {
        principal: { id: BUILDER_ID },
        organizations: [{ id: ORG_ID, status: "active" }],
        context: { organization_id: ORG_ID, workspace_id: WS_ID },
        csrf_token: CSRF,
      });
    }
    if (path === "/api/v1/organizations" && method === "GET") {
      return fulfill(route, 200, [{ id: ORG_ID, status: "active" }]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/members` && method === "GET") {
      return fulfill(route, 200, [
        { principal_id: BUILDER_ID, organization_id: ORG_ID, role_bindings: ["builder"] },
      ]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    if (path === `/api/v1/workspaces/${WS_ID}/groups` && method === "GET") {
      return fulfill(route, 200, []);
    }

    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, state.runs);
    }
    const runMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})$/);
    if (runMatch && method === "GET") {
      const detail = state.details[runMatch[1]];
      if (!detail) return fulfill(route, 404, { detail: "run not found" });
      state.runDetailRequests += 1;
      return fulfill(route, 200, detail);
    }
    if (path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/approvals$/) && method === "GET") {
      return fulfill(route, 200, []);
    }
    if (path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/events$/) && method === "GET") {
      return fulfill(route, 200, []);
    }
    const evidenceMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/evidence$/);
    if (evidenceMatch && method === "GET") {
      const payload = state.evidence[evidenceMatch[1]];
      if (!payload) return fulfill(route, 404, { detail: "run not found" });
      return fulfill(route, 200, payload);
    }

    // Smoke 分区的空数据面（costs 除外——fix-B 起由 state 驱动，供 run 面板）
    if (path === "/api/v1/evals" && method === "GET") return fulfill(route, 200, []);
    if (path === "/api/v1/releases" && method === "GET") return fulfill(route, 200, []);
    if (path === "/api/v1/observability/failures" && method === "GET") {
      return fulfill(route, 200, { codes: [{ code: "MODEL_TIMEOUT" }] });
    }
    if (path === "/api/v1/observability/costs" && method === "GET") {
      // fix-B：run 面板复用同一租户级端点（reservations 携带 run_id）——面板侧
      // 客户端过滤；端点无 run 过滤参数，不发明 query。
      return fulfill(route, 200, {
        reservations: state.costReservations,
        reconciliations: state.costReconciliations,
      });
    }
    if (path === "/api/v1/claims" && method === "GET") return fulfill(route, 200, []);

    // Studio draft CAS 契约（test_agents_studio_frozen.py 的 mock 侧镜像）：
    // GET 带 ETag；PUT 首写成功并推进 revision；旧 If-Match 重写 → 412
    // revision_conflict；无 If-Match → 428 if_match_required。
    if (path === `/api/v1/agents/${AGENT_ID}` && method === "GET") {
      state.agentRequests.push({ method, headers: req.headers() });
      return fulfill(route, 200, { agent_id: AGENT_ID, description: "rev1", revision: 2 }, {
        ETag: '"rev-2"',
      });
    }
    if (path === `/api/v1/agents/${AGENT_ID}` && method === "PUT") {
      state.agentRequests.push({ method, headers: req.headers() });
      const ifMatch = req.headers()["if-match"];
      if (!ifMatch) {
        return fulfill(route, 428, { reason: "if_match_required", message: "If-Match header required" });
      }
      if (ifMatch === '"rev-2"' && state.agentRequests.filter((r) => r.method === "PUT" && r.headers["if-match"] === '"rev-2"').length > 1) {
        return fulfill(route, 412, { reason: "revision_conflict", message: "stale revision" });
      }
      return fulfill(route, 200, { agent_id: AGENT_ID, description: "rev2", revision: 3 }, {
        ETag: '"rev-3"',
      });
    }

    // 未模拟路径显式失败（fail loud，不静默穿透 dev proxy）
    return fulfill(route, 500, { detail: `unmocked: ${method} ${path}` });
  });

  // SSE stream 路由：注册顺序在 catch-all 之后（Playwright 后注册者优先）。
  // 首连：一帧后结束流（模拟连接断开）；重连：断言携带 cursor 后再给一帧；
  // 第三次起 404 终止客户端重连循环（fail-closed：4xx 不再重试）。
  context.route(`**/api/v1/runs/${LIVE_RUN}/stream*`, async (route) => {
    const url = new URL(route.request().url());
    state.sseRequests.push(url.search);
    const attempt = state.sseRequests.length;
    if (attempt === 1) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseFrame(3, "TaskCompleted", "task-a"),
      });
    }
    if (attempt === 2) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseFrame(4, "RunCompleted", null),
      });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "stream closed" }) });
  });
}

async function newContextWithMocks(browser: Browser, state: MockState): Promise<BrowserContext> {
  const context = await browser.newContext();
  installApiMocks(context, state);
  return context;
}

async function openRunDetail(page: Page, runId: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByRole("row", { name: new RegExp(runId) }).getByRole("button", { name: "Open" }).click();
  await expect(page.getByRole("heading", { name: "Run" })).toBeVisible();
}

// ---------------------------------------------------------------------------
// (a) live 订阅 + cursor 续传：opt-in（默认关 → 零 SSE 请求）；开启后订阅
//     /stream，断线（流结束）后从最后收到的 sequence 带 ?cursor 重连，REST
//     快照重取；SSE 帧元数据合入 live 面板
// ---------------------------------------------------------------------------

test.describe("S10 architecture — live resync", () => {
  test("live toggle subscribes to SSE and reconnects with cursor after a dropped connection", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRunDetail(page, LIVE_RUN);

    // 机制 happy path：template ask-v1 → 已注册 renderer（S10-T4b 起为真实
    // evidence 视图；非终态 run 的 evidence 投影未生成 → 如实渲染 pending 态）
    await expect(page.getByText(/Evidence pending \(run/)).toBeVisible();

    // 默认关：零 SSE 连接（默认 journey 行为不变）
    expect(state.sseRequests).toHaveLength(0);

    await page.getByRole("button", { name: "Go live" }).click();
    await expect(page.getByText("live", { exact: true })).toBeVisible();
    await expect(page.getByText(/#3 TaskCompleted/)).toBeVisible();

    // 首连无 cursor；断线重连必须从最后收到的 sequence 续传（resync 证明）
    await expect.poll(() => state.sseRequests.length).toBeGreaterThan(1);
    expect(state.sseRequests[0]).toBe("");
    expect(state.sseRequests[1]).toBe("?cursor=3");
    // 重连前 REST 快照重取（首载 + 断线 resync，至少两次 run 详情 GET）
    expect(state.runDetailRequests).toBeGreaterThanOrEqual(2);

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (b) fail-closed app 绑定：绑定指向未注册 appId → honest unknown 状态，
//     绝不猜测默认 renderer
// ---------------------------------------------------------------------------

test.describe("S10 architecture — app binding fail-closed", () => {
  test("unknown app binding renders the honest unknown state", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await page.goto("/");

    // 经真实 registry 模块注册 ghost 绑定（templateId → 无 manifest 的 appId）
    await page.evaluate(async () => {
      const registry: {
        registerRunBinding: (binding: { templateId: string; appId: string }) => void;
      } = await import("/src/renderers/registry.ts");
      registry.registerRunBinding({ templateId: "ghost-pack", appId: "ghost-app" });
    });

    await page.getByRole("row", { name: new RegExp(GHOST_RUN) }).getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Run" })).toBeVisible();
    await expect(page.getByText("Unknown app: ghost-app")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (c) 既有分区 smoke：shell 迁移（AppShell + routes/sections）后全部分区照常
//     渲染——行为不变回归护栏
// ---------------------------------------------------------------------------

test.describe("S10 architecture — section smoke", () => {
  test("all existing sections still render after the shell restructure", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Workspaces" })).toBeVisible();

    await page.getByRole("button", { name: "Evals" }).click();
    await expect(page.getByRole("heading", { name: "Evals" })).toBeVisible();
    await expect(page.getByText("No eval runs")).toBeVisible();

    await page.getByRole("button", { name: "Releases" }).click();
    await expect(page.getByRole("heading", { name: "Releases" })).toBeVisible();
    await expect(page.getByText("No releases")).toBeVisible();

    await page.getByRole("button", { name: "Observability" }).click();
    await expect(page.getByRole("heading", { name: "Observability" })).toBeVisible();
    await expect(page.getByText("MODEL_TIMEOUT")).toBeVisible();

    await page.getByRole("button", { name: "Costs" }).click();
    await expect(page.getByRole("heading", { name: "Costs" })).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (d) 类型化 CAS 错误（经真实 api/client 模块）：getWithETag 读 ETag；旧
//     If-Match PUT → CasConflictError(412)；无 If-Match PUT →
//     PreconditionRequiredError(428)；mutation 头（CSRF + Idempotency-Key）
//     语义不因 client 迁移漂移
// ---------------------------------------------------------------------------

test.describe("S10 architecture — typed CAS client", () => {
  test("PUT surfaces 412/428 as typed errors through the real api client", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();

    // GET + ETag 读 helper
    const read = await page.evaluate(async () => {
      const client: {
        getWithETag: (path: string) => Promise<{ data: unknown; etag: string | null }>;
      } = await import("/src/lib/api.ts");
      return client.getWithETag("/api/v1/agents/8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c99");
    });
    expect(read.etag).toBe('"rev-2"');

    // 首写成功（mock 推进 revision）
    const firstPut = await page.evaluate(async () => {
      const client: {
        api: { put: (path: string, body?: unknown, headers?: Record<string, string>) => Promise<unknown> };
      } = await import("/src/lib/api.ts");
      try {
        await client.api.put(
          "/api/v1/agents/8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c99",
          { description: "rev2" },
          { "If-Match": '"rev-2"' }
        );
        return "no-error";
      } catch (e) {
        return (e as Error).name;
      }
    });
    expect(firstPut).toBe("no-error");

    // 持旧 If-Match 重写 → 412 CasConflictError（机器可读 reason 上浮）
    const stalePut = await page.evaluate(async () => {
      const client: {
        api: { put: (path: string, body?: unknown, headers?: Record<string, string>) => Promise<unknown> };
      } = await import("/src/lib/api.ts");
      try {
        await client.api.put(
          "/api/v1/agents/8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c99",
          { description: "rev3" },
          { "If-Match": '"rev-2"' }
        );
        return { name: "no-error", status: null, detail: null };
      } catch (e) {
        return {
          name: (e as Error).name,
          status: (e as { status?: number }).status ?? null,
          detail: (e as { detail?: string }).detail ?? null,
        };
      }
    });
    expect(stalePut.name).toBe("CasConflictError");
    expect(stalePut.status).toBe(412);
    expect(stalePut.detail).toContain("revision_conflict");
    expect(stalePut.detail).toContain("stale revision");

    // 无 If-Match → 428 PreconditionRequiredError
    const missingPut = await page.evaluate(async () => {
      const client: {
        api: { put: (path: string, body?: unknown, headers?: Record<string, string>) => Promise<unknown> };
      } = await import("/src/lib/api.ts");
      try {
        await client.api.put("/api/v1/agents/8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c99", {
          description: "rev4",
        });
        return { name: "no-error", status: null };
      } catch (e) {
        return {
          name: (e as Error).name,
          status: (e as { status?: number }).status ?? null,
        };
      }
    });
    expect(missingPut.name).toBe("PreconditionRequiredError");
    expect(missingPut.status).toBe(428);

    // mutation 头契约：If-Match 原样发送；CSRF + Idempotency-Key 不漂移
    const puts = state.agentRequests.filter((r) => r.method === "PUT");
    expect(puts).toHaveLength(3);
    expect(puts[0].headers["if-match"]).toBe('"rev-2"');
    expect(puts[1].headers["if-match"]).toBe('"rev-2"');
    expect(puts[2].headers["if-match"]).toBeUndefined();
    for (const put of puts) {
      expect(put.headers["x-csrf-token"]).toBe(CSRF);
      expect(UUID_RE.test(put.headers["idempotency-key"])).toBe(true);
    }

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (e) S10 fix-B：通用 Run panels（specs/s10 §2）在 RunDetailView 的面板结构。
//     真实数据面板（Task Graph/Evidence/Cost）与诚实 pending 面板
//     （Tools/Artifacts/Context/Memory，data-panel-state 区分，§5 无假数据），
//     执行模式 provenance 从 API 派生（§6），缺席字段如实 unknown。
// ---------------------------------------------------------------------------

test.describe("S10 fix-B — generic run panels", () => {
  test("completed unbound run renders the §2 panel structure with real and honest-pending states", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRunDetail(page, PANEL_RUN);

    // §2 面板结构：标题齐全（Approval 面板既有——ApprovalsView，§2 名 Approval）
    for (const name of ["Task Graph", "Evidence", "Cost", "Tools", "Artifacts", "Context", "Memory"]) {
      await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
    }

    // Task Graph：任务列表为真实投影；edges 无投影 → 如实声明，不发明边数据
    await expect(page.getByText("plan: completed")).toBeVisible();
    await expect(page.getByText("execute: completed")).toBeVisible();
    await expect(page.getByText(/Edges are not projected by the run detail API/)).toBeVisible();

    // Execution provenance（§6：mode 明示且从 API 派生）；template null → unknown
    await expect(page.getByText("Execution mode: fixture")).toBeVisible();
    await expect(page.getByText("Template: not reported")).toBeVisible();
    await expect(page.getByText("No app binding")).toBeVisible();

    // Evidence 面板：claims + verify 状态 + source locator + digest 常显
    const evidence = page.getByLabel("Run evidence", { exact: true });
    await expect(evidence.getByText("claim:verified-fact — Fact")).toBeVisible();
    await expect(evidence.getByText("verify: verified")).toBeVisible();
    await expect(evidence.getByText("verify: verification failed")).toBeVisible();
    await expect(evidence.getByText("CodeRef: src/plan.py:3-7 (digest sha256:cafe)")).toBeVisible();
    await expect(evidence.getByText("canonical: text = plan executed deterministically")).toBeVisible();
    await expect(evidence.getByText("upstream schema not retrievable")).toBeVisible();

    // Cost 面板：租户级端点无 run 过滤参数 → 客户端按 run_id 过滤（诚实派生）。
    // 本 run 的 reservation + 关联 reconciliation 可见；他 run 的不可见。
    const cost = page.getByLabel("Run cost", { exact: true });
    await expect(cost.getByText("res-panel-run")).toBeVisible();
    await expect(cost.getByText("0.0000042", { exact: true })).toBeVisible();
    await expect(cost.getByText("-0.0000001", { exact: true })).toBeVisible();
    await expect(cost.getByText("Run total (USD): 0.0000042")).toBeVisible();
    await expect(cost.getByText("1.5000000")).toHaveCount(0);
    await expect(cost.getByText("res-other-run")).toHaveCount(0);

    // 诚实 pending 面板与数据面板视觉可区分（data-panel-state），且不携带假数据
    await expect(page.locator('[data-panel-state="pending"]')).toHaveCount(4);
    await expect(page.getByText(/Tools — panel pending backend projection/)).toBeVisible();
    await expect(page.getByText(/Would show: artifacts emitted by this run/)).toBeVisible();

    // 既有真实面板不回归
    await expect(page.getByText("No pending approvals")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (f) S10 fix-B D1：VerifiedBrief 渲染——brief artifact 以结构化 claim 载荷
//     出现在 evidence 投影（canonical_value 携带 verified-brief 字段词汇），
//     renderer 逐字段渲染；unknowns 逐字；CodeRef/GitHubRef 完整。
// ---------------------------------------------------------------------------

test.describe("S10 fix-B — verified brief rendering", () => {
  test("change brief run renders VerifiedBrief fields from the evidence projection", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRunDetail(page, BRIEF_RUN);

    const brief = page.getByLabel("Verified brief", { exact: true });
    await expect(brief.getByText("analyze_impact (function) — src/impact.py:10-42")).toBeVisible();
    await expect(brief.getByText("fastapi (>=0.110, direct)")).toBeVisible();
    await expect(brief.getByText("tests/test_impact.py::test_analyze (expected pass)")).toBeVisible();
    await expect(brief.getByText("acme/widgets#14")).toBeVisible();
    await expect(brief.getByText("acme/widgets#77")).toBeVisible();
    await expect(brief.getByText("ci-unit: passed")).toBeVisible();
    await expect(brief.getByText("high: Verification failed; brief claims are unverified")).toBeVisible();
    await expect(brief.getByText("No coverage data for src/impact.py")).toBeVisible();
    await expect(brief.getByText("CodeRef: src/impact.py:10-42 (digest sha256:deadbeef)")).toBeVisible();
    await expect(brief.getByText("GitHubRef: acme/widgets commit abc1234 pr 14 path src/impact.py")).toBeVisible();

    // brief 已产出 → 不得再渲染 pending 态
    await expect(page.getByText(/Verified brief artifact pending/)).toHaveCount(0);

    // Execution provenance 从 API 派生（template change-brief + fixture mode）
    await expect(page.getByText("Template: change-brief")).toBeVisible();
    await expect(page.getByText("Execution mode: fixture")).toBeVisible();

    await context.close();
  });

  test("change brief run without a projected brief stays honest pending", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRunDetail(page, BRIEF_PENDING_RUN);

    // 投影无 brief → pending 如实呈现（不伪造内容）；Evidence 面板如实空态
    await expect(page.getByText(/Verified brief artifact pending/)).toBeVisible();
    await expect(page.getByText("No claims")).toBeVisible();
    await expect(page.getByText("Template: change-brief")).toBeVisible();

    await context.close();
  });
});
