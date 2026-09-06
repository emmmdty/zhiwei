// S8 discover-case-action e2e（specs/s8-discover-actions.md §6 → Gate 例外条目
// docs/handoffs/s8-discover-case-action-e2e-exception.md 的解锁 spec，S10-T4c）。
//
// 与 spec §6 Workbench journey 的映射（以真实路由/UI 为准，mock 模式先例
// runtime-approval.spec.ts / ask-evidence.spec.ts）：
//   spec §6 旅程                                | 本 spec 落点（真实 UI）
//   --------------------------------------------+----------------------------------
//   Discover feed（RiskHypothesis 列表，        | discover-v1 run 经 Workbench 打开
//   score 不得标注 probability）                | → AppRendererSlot → renderers/
//                                               | discover/result.tsx，feed 经
//                                               | GET /api/v1/discover/feed
//                                               | （api/discover.py）渲染；score
//                                               | 以域名 "score" 呈现，页面断言
//                                               | 不出现 probability 字样
//   人工 triage（owner/status 状态迁移）        | feed 行内 Claim triage（claim 写
//                                               | owner=principal）→ POST
//                                               | /api/v1/discover/hypotheses/
//                                               | {id}/triage；非法迁移由服务端
//                                               | 状态机拒绝（契约测试钉住）
//   创建 Case                                   | POST /api/v1/discover/hypotheses/
//                                               | {id}/cases（S8 DiscoverCase 聚合，
//                                               | 与 S6 run-case 面分离）→ feed 刷新
//                                               | 出现 Open case
//   请求 tool action → 审批（高风险不默认执行） | POST /api/v1/discover/cases/{id}/
//                                               | actions 返回 409 逐字拒绝
//                                               | （server-driven 门禁，action 落
//                                               | pending_approval）→ Approve →
//                                               | POST /api/v1/discover/actions/
//                                               | {id}/approve → 投影呈现 approved
//   记录 Resolution                             | POST /api/v1/discover/cases/{id}/
//                                               | resolutions → HumanResolution
//                                               | 投影 + case 转为 resolved
//   刷新后从 projection 恢复                    | page.reload() 后 feed/case 重取
//                                               | （请求计数自证恢复语义）
//
// 后端边界（mock 模式，operator 已授权同 runtime-approval.spec.ts）：响应形状
// 1:1 对齐 src/zhiwei/api/discover.py 的 Pydantic 投影；不发任何真实外部请求；
// 未模拟的 /api 路径一律 500 显式失败（fail loud）。
// SoD（requester ≠ approver）由后端契约测试钉住（tests/integration/discover/
// test_discover_api.py 自批 409 反例）；本单会话 mock 旅程只验证 UI 门禁渲染、
// 状态投影与刷新恢复，不承载 SoD 断言。
// 会话经模拟 GET /api/v1/me + members 直接引导（session.tsx 真实解析路径仍执行）。

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

// discover-v1 run：Discover App 的入口（trigger → StartRun 的执行载体）
const DISCOVER_RUN = "d1sc0ver-0000-4000-8000-000000000001";

// api/discover.py FeedHypothesisView 的 1:1 mock 形状
interface FeedHypothesisMock {
  id: string;
  title: string;
  description: string;
  status: string;
  owner: string;
  kind: string;
  severity: string;
  score: number | null;
  supporting_count: number;
  contradicting_count: number;
  missing_count: number;
  freshness_hours: number;
  dedup_key: string;
  suggested_validation_actions: string[];
  case_id: string | null;
  created_at: string;
  updated_at: string;
}

// api/discover.py ActionView 的 1:1 mock 形状
interface ActionMock {
  id: string;
  case_id: string;
  hypothesis_id: string;
  action_type: string;
  tool_name: string;
  parameters: Record<string, unknown>;
  rationale: string;
  requested_by: string;
  status: string;
  s2_decision_id: string | null;
  approved_by: string | null;
  approval_timestamp: string | null;
  created_at: string;
}

// api/discover.py ResolutionView 的 1:1 mock 形状
interface ResolutionMock {
  id: string;
  case_id: string;
  hypothesis_id: string;
  kind: string;
  rationale: string;
  resolved_by: string;
  approved_by: string;
  notes: string;
  evidence_refs: string[];
  approval_timestamp: string;
  created_at: string;
}

// api/discover.py CaseDetailView 的 1:1 mock 形状
interface CaseDetailMock {
  id: string;
  hypothesis_id: string;
  hypothesis_ids: string[];
  title: string;
  description: string;
  status: string;
  severity: string;
  owner: string;
  dedup_key: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  actions: ActionMock[];
  resolutions: ResolutionMock[];
}

// api/discover.py 的 409 拒绝 detail（server-driven 门禁的逐字文本）
const APPROVAL_REFUSAL = "action requires human approval before execution";

const H1 = "1a2b3c4d-0000-4000-8000-0000000000a1";
const H2 = "1a2b3c4d-0000-4000-8000-0000000000a2";

interface MockState {
  feed: FeedHypothesisMock[];
  feedFetches: number;
  caseDetail: CaseDetailMock | null;
  caseDetailFetches: number;
  lastTriage: { body: unknown } | null;
  lastCreateCase: { headers: Record<string, string>; body: unknown } | null;
  lastSubmitAction: { headers: Record<string, string>; body: unknown } | null;
  lastResolution: { body: unknown } | null;
  approveRequests: number;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function newFeed(): FeedHypothesisMock[] {
  const now = new Date().toISOString();
  return [
    {
      id: H1,
      title: "Vendor X spend anomaly",
      description: "Vendor X spending increased 300% month over month",
      status: "ready_for_triage",
      owner: "",
      kind: "supporting",
      severity: "high",
      score: 0.87,
      supporting_count: 2,
      contradicting_count: 1,
      missing_count: 0,
      freshness_hours: 3.5,
      dedup_key: "fingerprint:vendor-x-spend:v1",
      suggested_validation_actions: ["Ask for corroborating evidence"],
      case_id: null,
      created_at: now,
      updated_at: now,
    },
    {
      id: H2,
      title: "Dormant service account usage",
      description: "Dormant service account authenticated from a new ASN",
      status: "in_triage",
      owner: BUILDER_ID,
      kind: "supporting",
      severity: "warning",
      score: null,
      supporting_count: 1,
      contradicting_count: 0,
      missing_count: 2,
      freshness_hours: 26,
      dedup_key: "",
      suggested_validation_actions: [],
      case_id: null,
      created_at: now,
      updated_at: now,
    },
  ];
}

function newState(): MockState {
  return {
    feed: newFeed(),
    feedFetches: 0,
    caseDetail: null,
    caseDetailFetches: 0,
    lastTriage: null,
    lastCreateCase: null,
    lastSubmitAction: null,
    lastResolution: null,
    approveRequests: 0,
  };
}

function runDetail() {
  return {
    run_id: DISCOVER_RUN,
    status: "completed",
    organization_id: ORG_ID,
    tasks: { detect: { status: "completed", error: null } },
    template: "discover-v1",
  };
}

function installApiMocks(context: BrowserContext, state: MockState): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const nowIso = () => new Date().toISOString();

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

    // runtime 面（api/runs.py 契约；template 为前端扩展字段，architecture.spec.ts 同款）
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, [
        { run_id: DISCOVER_RUN, status: "completed", organization_id: ORG_ID },
      ]);
    }
    if (path === `/api/v1/runs/${DISCOVER_RUN}` && method === "GET") {
      return fulfill(route, 200, runDetail());
    }
    if (path === `/api/v1/runs/${DISCOVER_RUN}/approvals` && method === "GET") {
      return fulfill(route, 200, []);
    }
    if (path === `/api/v1/runs/${DISCOVER_RUN}/events` && method === "GET") {
      return fulfill(route, 200, []);
    }

    // discover 面（api/discover.py 契约，形状 1:1）
    if (path === "/api/v1/discover/feed" && method === "GET") {
      state.feedFetches += 1;
      return fulfill(route, 200, state.feed);
    }
    const triageMatch = path.match(/^\/api\/v1\/discover\/hypotheses\/([0-9a-f-]{36})\/triage$/);
    if (triageMatch && method === "POST") {
      const body = req.postDataJSON() as { status: string; owner?: string };
      state.lastTriage = { body };
      const row = state.feed.find((h) => h.id === triageMatch[1]);
      if (!row) return fulfill(route, 404, { detail: "hypothesis not found" });
      row.status = body.status;
      if (body.owner !== undefined) row.owner = body.owner;
      row.updated_at = nowIso();
      return fulfill(route, 200, row);
    }
    const createCaseMatch = path.match(/^\/api\/v1\/discover\/hypotheses\/([0-9a-f-]{36})\/cases$/);
    if (createCaseMatch && method === "POST") {
      state.lastCreateCase = { headers: req.headers(), body: req.postDataJSON() };
      const row = state.feed.find((h) => h.id === createCaseMatch[1]);
      if (!row) return fulfill(route, 404, { detail: "hypothesis not found" });
      const body = req.postDataJSON() as { title?: string; description?: string };
      const caseId = crypto.randomUUID();
      const now = nowIso();
      state.caseDetail = {
        id: caseId,
        hypothesis_id: row.id,
        hypothesis_ids: [row.id],
        title: body.title ?? row.title,
        description: body.description ?? row.description,
        status: "open",
        severity: row.severity,
        owner: row.owner,
        dedup_key: row.dedup_key,
        created_by: BUILDER_ID,
        created_at: now,
        updated_at: now,
        actions: [],
        resolutions: [],
      };
      row.case_id = caseId;
      row.updated_at = now;
      return fulfill(route, 201, state.caseDetail);
    }
    const caseMatch = path.match(/^\/api\/v1\/discover\/cases\/([0-9a-f-]{36})$/);
    if (caseMatch && method === "GET") {
      if (!state.caseDetail || state.caseDetail.id !== caseMatch[1]) {
        return fulfill(route, 404, { detail: "case not found" });
      }
      state.caseDetailFetches += 1;
      return fulfill(route, 200, state.caseDetail);
    }
    const submitActionMatch = path.match(/^\/api\/v1\/discover\/cases\/([0-9a-f-]{36})\/actions$/);
    if (submitActionMatch && method === "POST") {
      state.lastSubmitAction = { headers: req.headers(), body: req.postDataJSON() };
      if (!state.caseDetail || state.caseDetail.id !== submitActionMatch[1]) {
        return fulfill(route, 404, { detail: "case not found" });
      }
      const body = req.postDataJSON() as {
        action_type: string;
        tool_name: string;
        rationale: string;
        parameters?: Record<string, unknown>;
      };
      const now = nowIso();
      const action: ActionMock = {
        id: crypto.randomUUID(),
        case_id: state.caseDetail.id,
        hypothesis_id: state.caseDetail.hypothesis_id,
        action_type: body.action_type,
        tool_name: body.tool_name,
        parameters: body.parameters ?? {},
        rationale: body.rationale,
        requested_by: BUILDER_ID,
        // server-driven 门禁：提交即落 pending_approval，从不默认执行
        status: "pending_approval",
        s2_decision_id: crypto.randomUUID(),
        approved_by: null,
        approval_timestamp: null,
        created_at: now,
      };
      state.caseDetail.actions.push(action);
      state.caseDetail.updated_at = now;
      // mutation 已应用（request 落账），执行被门禁拒绝 → 409 逐字拒绝
      return fulfill(route, 409, { detail: APPROVAL_REFUSAL });
    }
    const approveMatch = path.match(/^\/api\/v1\/discover\/actions\/([0-9a-f-]{36})\/approve$/);
    if (approveMatch && method === "POST") {
      state.approveRequests += 1;
      const action = state.caseDetail?.actions.find((a) => a.id === approveMatch[1]);
      if (!action) return fulfill(route, 404, { detail: "action not found" });
      if (action.status !== "pending_approval") {
        return fulfill(route, 409, { detail: `action is in ${action.status} status` });
      }
      action.status = "approved";
      action.approved_by = BUILDER_ID;
      action.approval_timestamp = nowIso();
      return fulfill(route, 200, action);
    }
    const resolutionMatch = path.match(/^\/api\/v1\/discover\/cases\/([0-9a-f-]{36})\/resolutions$/);
    if (resolutionMatch && method === "POST") {
      state.lastResolution = { body: req.postDataJSON() };
      if (!state.caseDetail || state.caseDetail.id !== resolutionMatch[1]) {
        return fulfill(route, 404, { detail: "case not found" });
      }
      if (state.caseDetail.resolutions.length > 0) {
        return fulfill(route, 409, { detail: "case already has a resolution" });
      }
      const body = req.postDataJSON() as { kind: string; rationale: string; notes?: string };
      const now = nowIso();
      const resolution: ResolutionMock = {
        id: crypto.randomUUID(),
        case_id: state.caseDetail.id,
        hypothesis_id: state.caseDetail.hypothesis_id,
        kind: body.kind,
        rationale: body.rationale,
        resolved_by: BUILDER_ID,
        approved_by: BUILDER_ID,
        notes: body.notes ?? "",
        evidence_refs: [],
        approval_timestamp: now,
        created_at: now,
      };
      state.caseDetail.resolutions.push(resolution);
      state.caseDetail.status = "resolved";
      state.caseDetail.updated_at = now;
      return fulfill(route, 201, resolution);
    }

    // 未模拟路径显式失败（fail loud，不静默穿透 dev proxy）
    return fulfill(route, 500, { detail: `unmocked: ${method} ${path}` });
  });
}

async function newContextWithMocks(browser: Browser, state: MockState): Promise<BrowserContext> {
  const context = await browser.newContext();
  installApiMocks(context, state);
  return context;
}

async function openDiscoverRun(page: Page, runId: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page
    .getByRole("row", { name: new RegExp(runId) })
    .getByRole("button", { name: "Open" })
    .click();
  await expect(page.getByRole("heading", { name: "Run" })).toBeVisible();
}

// ---------------------------------------------------------------------------
// (a) 完整解锁旅程：feed（score 不标注 probability）→ triage（owner/status
//     迁移）→ 创建 Case → 高风险 action 提交（409 逐字拒绝，server-driven
//     门禁）→ 审批 → 投影呈现 approved → HumanResolution 记录 → 刷新恢复
// ---------------------------------------------------------------------------

test.describe("S8 discover case action —解锁旅程", () => {
  test("feed, triage, case, approval-gated action and human resolution recover from projection", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openDiscoverRun(page, DISCOVER_RUN);

    // feed 渲染：RiskHypothesis 行 + 域名词 "score"（无 probability 标注）
    const feed = page.getByRole("region", { name: "Discover feed" });
    await expect(feed.getByText("Vendor X spend anomaly")).toBeVisible();
    await expect(feed.getByText("score: 0.87")).toBeVisible();
    await expect(page.getByText(/probab/i)).toHaveCount(0);

    // triage：ready_for_triage → in_triage（claim 写 owner=principal）
    await feed.getByRole("button", { name: "Claim triage" }).click();
    await expect(feed.getByText("status: in_triage").first()).toBeVisible();
    expect(state.lastTriage).not.toBeNull();
    expect(state.lastTriage!.body).toMatchObject({ status: "in_triage", owner: BUILDER_ID });
    await expect(feed.getByText(`owner: ${BUILDER_ID}`).first()).toBeVisible();

    // 创建 Case（S8 DiscoverCase 聚合）→ feed 刷新出现 Open case
    await feed.getByRole("button", { name: "Create case" }).click();
    expect(state.lastCreateCase).not.toBeNull();
    // mutation PEP 契约：CSRF + Idempotency-Key（api client 统一注入）
    expect(state.lastCreateCase!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastCreateCase!.headers["idempotency-key"])).toBe(true);
    await expect(feed.getByRole("button", { name: "Open case" })).toBeVisible();

    await feed.getByRole("button", { name: "Open case" }).click();
    const caseView = page.getByRole("region", { name: "Discover case" });
    await expect(caseView.getByText("Status: open")).toBeVisible();

    // 高风险 action 提交：server-driven 门禁 409 → 拒绝文本逐字渲染
    await caseView.getByLabel("Action type").selectOption("modify");
    await caseView.getByLabel("Tool name").fill("vendor-payment-adjust");
    await caseView.getByLabel("Rationale").fill("Adjust vendor X payment terms");
    await caseView.getByRole("button", { name: "Submit action" }).click();
    await expect(page.getByRole("alert")).toHaveText(APPROVAL_REFUSAL);
    expect(state.lastSubmitAction).not.toBeNull();
    expect(state.lastSubmitAction!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastSubmitAction!.headers["idempotency-key"])).toBe(true);
    // mutation 已落账（pending_approval）且未被执行——投影呈现 pending 状态
    await expect(caseView.getByText(/modify vendor-payment-adjust — pending_approval/)).toBeVisible();

    // 审批 → 投影呈现 approved（SoD 由后端契约测试钉住，见文件头注）
    await caseView.getByRole("button", { name: "Approve" }).click();
    await expect(caseView.getByText(/— approved/)).toBeVisible();
    await expect(caseView.getByText(`approved by: ${BUILDER_ID}`)).toBeVisible();

    // HumanResolution 记录 + case 转为 resolved
    await caseView.getByLabel("Resolution kind").selectOption("accepted");
    await caseView.getByLabel("Resolution rationale").fill("Confirmed with vendor ledger");
    await caseView.getByRole("button", { name: "Record resolution" }).click();
    await expect(caseView.getByText("Resolution: accepted")).toBeVisible();
    await expect(caseView.getByText("Status: resolved")).toBeVisible();
    expect(state.lastResolution).not.toBeNull();
    expect(state.lastResolution!.body).toMatchObject({
      kind: "accepted",
      rationale: "Confirmed with vendor ledger",
    });

    // 刷新恢复：feed/case 从 server projection 重取（请求计数自证）
    await page.reload();
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
    const feedAfter = page.getByRole("region", { name: "Discover feed" });
    await expect(feedAfter.getByText("Vendor X spend anomaly")).toBeVisible();
    const feedFetches = state.feedFetches;
    expect(feedFetches).toBeGreaterThanOrEqual(2);
    await feedAfter.getByRole("button", { name: "Open case" }).click();
    const caseAfter = page.getByRole("region", { name: "Discover case" });
    await expect(caseAfter.getByText("Status: resolved")).toBeVisible();
    await expect(caseAfter.getByText(/— approved/)).toBeVisible();
    await expect(caseAfter.getByText("Resolution: accepted")).toBeVisible();
    expect(state.caseDetailFetches).toBeGreaterThanOrEqual(2);

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (b) run input（trigger/program context）的诚实缺席：runtime REST 契约不投影
//     trigger/program 字段 → 如实声明缺席，不发明触发器/程序字段，无控件
// ---------------------------------------------------------------------------

test.describe("S8 discover case action — input honesty", () => {
  test("input view states the missing trigger and program projection and offers no controls", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openDiscoverRun(page, DISCOVER_RUN);
    const inputView = page.getByRole("region", { name: "App input view" });
    await expect(
      inputView.getByText(/not projected by the runtime REST contract/)
    ).toBeVisible();
    await expect(inputView.getByText(new RegExp(DISCOVER_RUN))).toBeVisible();
    // 无 trigger/program 控件（对应投影端点不存在，不发明 UI）
    await expect(inputView.getByRole("button")).toHaveCount(0);

    await context.close();
  });
});
