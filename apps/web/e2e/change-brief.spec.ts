// S10-T7 change-brief member journey e2e（specs/s10 §5 Member journey / §6 required
// tests / §8 claim boundary，plan Task 7；R2 交叉检验 gap #4：member run 创建入口）。
//
// 事实源映射（mock 模式，先例 ask-evidence.spec.ts / architecture.spec.ts）：
//   Member 创建 ChangeBrief run            | Workbench 模板选择器含 change-brief →
//                                          | POST /api/v1/runs（生产 run 创建入口，
//                                          | api/runs.py create_run）；模板 id 逐字
//                                          | 对齐 src/zhiwei/evals/pack_templates.py
//                                          | PACK_TEMPLATE_BINDINGS（ask-v1 /
//                                          | change-brief 可执行；discover-v1 注册但
//                                          | 无 fixture 绑定 → 422 拒绝——加进选择器
//                                          | 就是死控件，S10 gate 例外 E3 登记）
//   run 详情携带 template + mode=fixture    | fix-A 契约（tests/contract/api/
//                                          | test_pack_template_runs.py：ask-v1 与
//                                          | change-brief 经同一 generic seam 断言
//                                          | detail["template"]/detail["mode"]）
//   App binding 解析（renderer 槽位激活）   | RunDetailView → AppRendererSlot →
//                                          | renderers/changeBrief（T1 注册绑定）
//   VerifiedBrief 渲染                      | GET /api/v1/runs/{id}/evidence
//                                          | （api/evidence.py RunEvidenceView）；
//                                          | brief 以结构化 claim 载荷出现
//                                          | （canonical_value 携带 verified-brief
//                                          | 字段词汇——R2 口径与 architecture.spec
//                                          | (f) 同型）。载荷内容不是手编：逐字段取自
//                                          | evals/change-brief/mixed-refs.yaml 经
//                                          | solution-packs/change-brief/runtime 纯
//                                          | 函数链（plan_retrieval → analyze_impact
//                                          | → verify_impact → synthesize_brief）的
//                                          | 真实推导输出（2026-09-06 HEAD c08f6ad）
//   非终态 run 诚实 pending                  | renderer 非 brief 投影不伪造内容
//   Auditor 只读同视图                       | 只读渲染 + 零 mutation 请求
//
// 后端边界：响应形状 1:1 对齐 src/zhiwei/api/{runs,evidence}.py 的 Pydantic 投影
// （extra=forbid 契约，逐字段）；不发任何真实外部请求；未模拟的 /api 路径一律 500
// 显式失败（fail loud）。会话经模拟 GET /api/v1/me + members 引导（session.tsx
// 真实解析路径仍执行）。

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
const MEMBER_ID = "f740acc5-03c3-486e-8384-2a9335fd4285";
const AUDITOR_ID = "63d7ef96-75e0-4c47-8edb-10dd834c9f64";
const CSRF = "e2e-csrf-token";

// mock 域确定性 id
const CREATED_RUN = "c7a00000-0000-4000-8000-000000000001";
const BRIEF_RUN = "c7a00000-0000-4000-8000-000000000002";
const PENDING_RUN = "c7a00000-0000-4000-8000-000000000003";
const NO_BRIEF_RUN = "c7a00000-0000-4000-8000-000000000004";

type Actor = "member" | "auditor";

const ACTORS: Record<Actor, { principal: string; role_bindings: string[] }> = {
  member: { principal: MEMBER_ID, role_bindings: ["member"] },
  auditor: { principal: AUDITOR_ID, role_bindings: ["auditor"] },
};

// ---------------------------------------------------------------------------
// VerifiedBrief 载荷（非手编：mixed-refs 冻结语料经 pack runtime 纯函数链的
// 真实推导输出，见文件头；10 必需字段 = solution-packs/change-brief/schemas/
// verified-brief.yaml = tests/integration/change_brief/test_change_brief_run.py
// BRIEF_REQUIRED_KEYS）。
// ---------------------------------------------------------------------------

const MIXED_REFS_BRIEF = {
  affected_symbols: [
    {
      name: "KnowledgePlanner",
      kind: "class",
      file_path: "src/zhiwei/knowledge/planner.py",
      line_start: 40,
      line_end: 180,
    },
    {
      name: "SourceVersion",
      kind: "class",
      file_path: "src/zhiwei/knowledge/contracts.py",
      line_start: 100,
      line_end: 160,
    },
  ],
  affected_dependencies: [{ name: "sqlglot", version_constraint: ">=28.0.0,<29", impact: "direct" }],
  affected_tests: [
    {
      test_id: "tests/unit/knowledge/test_contracts.py::test_source_version",
      expected_status: "fail",
    },
    {
      test_id: "tests/unit/knowledge/test_planner.py::test_plan_retrieval",
      expected_status: "fail",
    },
  ],
  related_prs: [{ repository: "zhiwei-core", pr_number: 312 }],
  related_issues: [{ repository: "zhiwei-core", issue_number: 104 }],
  related_checks: [{ name: "ci/lint", status: "passed" }],
  risks: [{ description: "dependency contract surface touched: sqlglot", severity: "medium" }],
  unknowns: [],
  code_refs: [
    {
      file_path: "src/zhiwei/knowledge/contracts.py",
      line_start: 100,
      line_end: 160,
      code_digest: "sha256:8888888888888888888888888888888888888888888888888888888888888888",
    },
    {
      file_path: "src/zhiwei/knowledge/planner.py",
      line_start: 40,
      line_end: 180,
      code_digest: "sha256:7777777777777777777777777777777777777777777777777777777777777777",
    },
  ],
  github_refs: [
    { repository: "zhiwei-core", commit_sha: "c0de5a1e", path: "src/zhiwei/knowledge/contracts.py" },
    { repository: "zhiwei-core", commit_sha: "c0de5a1e", path: "src/zhiwei/knowledge/planner.py" },
  ],
};

// api/evidence.py RunEvidenceView 的 1:1 mock 形状（12 字段，extra=forbid）
interface ClaimMock {
  claim_ref: string;
  claim_type: string | null;
  verified: boolean | null;
  quote_text: string | null;
  evidence_refs: Record<string, unknown>[];
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
  findings: Record<string, unknown>[];
  conflicts: Record<string, unknown>[];
}

// api/runs.py RunDetail 的 1:1 mock 形状（run_id/status/organization_id/tasks/
// template/mode；extra=forbid）
interface DetailMock {
  run_id: string;
  status: string;
  organization_id: string;
  tasks: Record<string, { status: string; error: string | null }>;
  template: string | null;
  mode: string | null;
}

// pack task_graph.yaml 拓扑（unit 前缀隔离，fix-A 契约的断言面同型）
const MIXED_REFS_TASKS: DetailMock["tasks"] = {
  "mixed-refs/retrieve_code_knowledge": { status: "completed", error: null },
  "mixed-refs/analyze_impact": { status: "completed", error: null },
  "mixed-refs/verify_brief": { status: "completed", error: null },
  "mixed-refs/synthesize_brief": { status: "completed", error: null },
  "mixed-refs/emit_brief": { status: "completed", error: null },
  "mixed-refs/finish": { status: "completed", error: null },
};

interface MockState {
  runs: { run_id: string; status: string; organization_id: string; template: string | null }[];
  details: Record<string, DetailMock>;
  evidence: Record<string, EvidenceMock>;
  lastCreateRun: { body: { template: string; workspace_id: string } } | null;
  mutationCount: number;
}

function newState(actor: Actor): MockState {
  const seeded: MockState = {
    runs: [
      { run_id: BRIEF_RUN, status: "completed", organization_id: ORG_ID, template: "change-brief" },
      { run_id: PENDING_RUN, status: "running", organization_id: ORG_ID, template: "change-brief" },
      {
        run_id: NO_BRIEF_RUN,
        status: "completed",
        organization_id: ORG_ID,
        template: "change-brief",
      },
    ],
    details: {
      [BRIEF_RUN]: {
        run_id: BRIEF_RUN,
        status: "completed",
        organization_id: ORG_ID,
        tasks: MIXED_REFS_TASKS,
        template: "change-brief",
        mode: "fixture",
      },
      [PENDING_RUN]: {
        run_id: PENDING_RUN,
        status: "running",
        organization_id: ORG_ID,
        tasks: { ...MIXED_REFS_TASKS, "mixed-refs/finish": { status: "pending", error: null } },
        template: "change-brief",
        mode: "fixture",
      },
      [NO_BRIEF_RUN]: {
        run_id: NO_BRIEF_RUN,
        status: "completed",
        organization_id: ORG_ID,
        tasks: {
          "mixed-refs/analyze_impact": { status: "completed", error: null },
          "mixed-refs/emit_brief": { status: "completed", error: null },
        },
        template: "change-brief",
        mode: "fixture",
      },
    },
    evidence: {
      // brief run：brief artifact 以结构化 claim 载荷出现（canonical_value 携带
      // verified-brief 字段词汇——R2 口径，与 architecture.spec (f) 同型）
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
            canonical_value: MIXED_REFS_BRIEF,
          },
        ],
        verified_claims: ["claim:verified-brief"],
        failed_claims: [],
        verification: null,
        unknowns: [],
        clarification: null,
        findings: [],
        conflicts: [],
      },
      // 非终态/无 brief run：真实端点此时无 claim 记录——诚实空投影
      [PENDING_RUN]: emptyEvidence(PENDING_RUN, "running"),
      [NO_BRIEF_RUN]: emptyEvidence(NO_BRIEF_RUN, "completed"),
    },
    lastCreateRun: null,
    mutationCount: 0,
  };
  void actor;
  return seeded;
}

function emptyEvidence(runId: string, runStatus: string): EvidenceMock {
  return {
    run_id: runId,
    run_status: runStatus,
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
      return fulfill(route, 200, [
        {
          principal_id: ACTORS[actor].principal,
          organization_id: ORG_ID,
          role_bindings: ACTORS[actor].role_bindings,
        },
      ]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    if (path === "/api/v1/cases" && method === "GET") {
      return fulfill(route, 200, []);
    }

    // ------------------------------------------------------------------
    // Workbench runs（api/runs.py 契约；RunRecord/RunDetail 1:1）
    // ------------------------------------------------------------------
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, state.runs);
    }
    if (path === "/api/v1/runs" && method === "POST") {
      const body = req.postDataJSON() as { template: string; workspace_id: string };
      state.lastCreateRun = { body };
      state.mutationCount += 1;
      // 201 响应 = create_run 真实返回形状（run_id/status/template）
      const created = { run_id: CREATED_RUN, status: "created", template: body.template };
      state.runs.push({
        run_id: CREATED_RUN,
        status: created.status,
        organization_id: ORG_ID,
        template: body.template,
      });
      state.details[CREATED_RUN] = {
        run_id: CREATED_RUN,
        status: "running",
        organization_id: ORG_ID,
        tasks: {},
        template: body.template,
        mode: "fixture",
      };
      state.evidence[CREATED_RUN] = emptyEvidence(CREATED_RUN, "running");
      return fulfill(route, 201, created);
    }
    const runMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})$/);
    if (runMatch && method === "GET") {
      const detail = state.details[runMatch[1]];
      if (!detail) return fulfill(route, 404, { detail: "run not found" });
      return fulfill(route, 200, detail);
    }
    if (path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/approvals$/) && method === "GET") {
      return fulfill(route, 200, []);
    }
    if (path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/events$/) && method === "GET") {
      return fulfill(route, 200, []);
    }

    // evidence 投影（api/evidence.py RunEvidenceView，形状 1:1）
    const evidenceMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/evidence$/);
    if (evidenceMatch && method === "GET") {
      const payload = state.evidence[evidenceMatch[1]];
      if (!payload) return fulfill(route, 404, { detail: "run not found" });
      return fulfill(route, 200, payload);
    }

    // cost 面（api/observability costs 真实形状；mock 域无成本记录 → 空数组）
    if (path === "/api/v1/observability/costs" && method === "GET") {
      return fulfill(route, 200, { reservations: [], reconciliations: [] });
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

async function openRun(page: Page, runId: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page
    .getByRole("row", { name: new RegExp(runId) })
    .getByRole("button", { name: "Open" })
    .click();
  await expect(page.getByRole("heading", { name: "Run" })).toBeVisible();
}

// ---------------------------------------------------------------------------
// (a) Member 创建 ChangeBrief run：模板选择器含 change-brief（且不含 discover-v1
//     死控件）→ POST /api/v1/runs 携带 template=change-brief → run 详情携带
//     Template: change-brief + Execution mode: fixture → App binding 解析
//     （renderer 槽位激活：非终态 run 的诚实 pending，而非 No app binding）。
//     R2 gap #4——member run 创建入口是 S10 最后的产品缺口。
// ---------------------------------------------------------------------------

test.describe("S10 change-brief — member run origination", () => {
  test("member originates a change-brief run from the workbench and lands in the app view", async ({ browser }) => {
    const state = newState("member");
    const context = await newContextWithMocks(browser, state, "member");
    const page = await context.newPage();

    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();

    // 模板选择器：change-brief 可选（pack_templates.py PACK_TEMPLATE_BINDINGS
    // 注册且可执行的模板）；discover-v1 注册但不可执行——不出现（死控件纪律）
    const templateSelect = page.getByLabel("Template");
    await expect(templateSelect.getByRole("option", { name: "change-brief" })).toBeAttached();
    await expect(templateSelect).not.toContainText("discover-v1");
    await templateSelect.selectOption("change-brief");
    await page.getByRole("button", { name: "New run" }).click();

    // 生产创建入口：POST /api/v1/runs 携带所选模板（api/runs.py create_run）
    expect(state.lastCreateRun).not.toBeNull();
    expect(state.lastCreateRun!.body.template).toBe("change-brief");
    expect(state.lastCreateRun!.body.workspace_id).toBe(WS_ID);

    // 创建后自动下钻 run 详情：template + mode 从 API 派生（fix-A 契约）
    await expect(page.getByText("Template: change-brief")).toBeVisible();
    await expect(page.getByText("Execution mode: fixture")).toBeVisible();

    // App binding 解析（renderer 槽位激活）：change-brief 绑定的 result renderer
    // 渲染非终态诚实 pending；通用空态 "No app binding" 不得出现
    const appPanel = page.getByRole("region", { name: "App panel" });
    await expect(
      appPanel.getByText(`Brief pending (run ${CREATED_RUN}: running)`)
    ).toBeVisible();
    await expect(page.getByText("No app binding")).toHaveCount(0);

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (b) 完成 run 的 VerifiedBrief 渲染：affected symbols/dependencies/tests、
//     related PRs/issues/checks、risks、unknowns 逐字；CodeRef/GitHubRef 完整
//     （载荷 = mixed-refs 冻结语料经 pack runtime 真实推导的 brief）。
// ---------------------------------------------------------------------------

test.describe("S10 change-brief — verified brief rendering", () => {
  test("renders the verified brief fields verbatim from the evidence projection", async ({ browser }) => {
    const state = newState("member");
    const context = await newContextWithMocks(browser, state, "member");
    const page = await context.newPage();

    await openRun(page, BRIEF_RUN);
    await expect(page.getByText("Template: change-brief")).toBeVisible();
    await expect(page.getByText("Execution mode: fixture")).toBeVisible();

    const brief = page.getByLabel("Verified brief", { exact: true });
    // affected symbols（快照命中，逐字）
    await expect(
      brief.getByText("KnowledgePlanner (class) — src/zhiwei/knowledge/planner.py:40-180")
    ).toBeVisible();
    await expect(
      brief.getByText("SourceVersion (class) — src/zhiwei/knowledge/contracts.py:100-160")
    ).toBeVisible();
    // dependencies / tests
    await expect(brief.getByText("sqlglot (>=28.0.0,<29, direct)")).toBeVisible();
    await expect(
      brief.getByText("tests/unit/knowledge/test_contracts.py::test_source_version (expected fail)")
    ).toBeVisible();
    await expect(
      brief.getByText("tests/unit/knowledge/test_planner.py::test_plan_retrieval (expected fail)")
    ).toBeVisible();
    // related PRs / issues / checks
    await expect(brief.getByText("zhiwei-core#312")).toBeVisible();
    await expect(brief.getByText("zhiwei-core#104")).toBeVisible();
    await expect(brief.getByText("ci/lint: passed")).toBeVisible();
    // risks（pack escalation 推导，逐字）
    await expect(
      brief.getByText("medium: dependency contract surface touched: sqlglot")
    ).toBeVisible();
    // CodeRef / GitHubRef（证据定位逐字；digest 常显）
    await expect(
      brief.getByText(
        "CodeRef: src/zhiwei/knowledge/contracts.py:100-160 (digest sha256:8888888888888888888888888888888888888888888888888888888888888888)"
      )
    ).toBeVisible();
    await expect(
      brief.getByText(
        "CodeRef: src/zhiwei/knowledge/planner.py:40-180 (digest sha256:7777777777777777777777777777777777777777777777777777777777777777)"
      )
    ).toBeVisible();
    await expect(
      brief.getByText("GitHubRef: zhiwei-core commit c0de5a1e pr n/a path src/zhiwei/knowledge/contracts.py")
    ).toBeVisible();
    await expect(
      brief.getByText("GitHubRef: zhiwei-core commit c0de5a1e pr n/a path src/zhiwei/knowledge/planner.py")
    ).toBeVisible();
    // unknowns 区如实存在（mixed-refs 语料 unknowns_empty），无编造条目
    await expect(brief.getByRole("heading", { name: "Unknowns" })).toBeVisible();

    // brief 已产出 → 不得再渲染 pending 态
    await expect(page.getByText(/Verified brief artifact pending/)).toHaveCount(0);

    // pack 拓扑任务（unit 前缀隔离）如实投影（renderer 自身任务列表锚定 brief 区）
    await expect(brief.getByText(/mixed-refs\/synthesize_brief: completed/)).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (c) 诚实 pending：非终态 run 不伪造 brief 内容；终态但投影未携带 brief →
//     "Verified brief artifact pending"（与 locked architecture.spec (g) 同面）。
// ---------------------------------------------------------------------------

test.describe("S10 change-brief — honest pending", () => {
  test("non-terminal run stays honestly pending without fabricating brief content", async ({ browser }) => {
    const state = newState("member");
    const context = await newContextWithMocks(browser, state, "member");
    const page = await context.newPage();

    await openRun(page, PENDING_RUN);

    // 非终态：renderer 槽位如实 pending（带 run 状态），不编造影响面
    const appPanel = page.getByRole("region", { name: "App panel" });
    await expect(
      appPanel.getByText(`Brief pending (run ${PENDING_RUN}: running)`)
    ).toBeVisible();
    await expect(appPanel.getByRole("heading", { name: "Affected symbols" })).toHaveCount(0);

    // 通用证据面板让位（绑定 App 自有 evidence 视图），通用 pending 文案不出现
    await expect(page.getByText(/Evidence panel pending/)).toHaveCount(0);

    await context.close();
  });

  test("completed run whose projection carries no brief states the honest pending", async ({ browser }) => {
    const state = newState("member");
    const context = await newContextWithMocks(browser, state, "member");
    const page = await context.newPage();

    await openRun(page, NO_BRIEF_RUN);

    // 投影无 brief → 诚实 pending 原文（不伪造 brief 内容）；无 claim 记录如实披露
    await expect(page.getByText(/Verified brief artifact pending/)).toBeVisible();
    await expect(page.getByText("No claims")).toBeVisible();
    await expect(page.getByText("Template: change-brief")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (d) Auditor 只读同视图：同一 brief 渲染只读可见；会话零 mutation 请求。
// ---------------------------------------------------------------------------

test.describe("S10 change-brief — auditor read-only", () => {
  test("auditor reads the same verified brief view and issues no mutations", async ({ browser }) => {
    const state = newState("auditor");
    const context = await newContextWithMocks(browser, state, "auditor");
    const page = await context.newPage();

    await openRun(page, BRIEF_RUN);

    await expect(page.getByText("Template: change-brief")).toBeVisible();
    await expect(page.getByText("Execution mode: fixture")).toBeVisible();
    const brief = page.getByLabel("Verified brief", { exact: true });
    await expect(
      brief.getByText("KnowledgePlanner (class) — src/zhiwei/knowledge/planner.py:40-180")
    ).toBeVisible();
    await expect(brief.getByText("zhiwei-core#312")).toBeVisible();

    // 只读：auditor 会话不发任何写路径（零 mutation，POST 计数为 0）
    expect(state.mutationCount).toBe(0);

    await context.close();
  });
});
