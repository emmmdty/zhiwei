// S2 runtime-approval e2e（specs/s2-agent-runtime.md §5 Web journey → Gate §7）。
//
// 与 spec 旅程的映射（以真实路由/UI 为准，ADR-013 反例 9 收敛）：
//   spec §5 旅程                          | 本 spec 落点（真实 UI）
//   --------------------------------------+----------------------------------------
//   Builder 创建 Run（sandbox AgentVersion)| Workbench 模板选择 + New run
//                                          | （apps/web/src/features/workbench）
//   Task Graph 实时推进                    | RunDetailView 任务投影，经 Back→Open
//                                          | 重新加载观察状态演进。注意：当前 UI 无
//                                          | SSE/轮询订阅（s2.md 交接单 §4-3 只交付
//                                          | REST PG 投影），spec §5 的「实时」腿
//                                          | 需 UI 订阅实现后方可验证——不在本 spec
//                                          | 伪装（不写等永不出现 DOM 的断言）。
//   Approver 独立账号批准/拒绝             | ApprovalsView 决策按钮；双浏览器上下文
//                                          | 模拟双账号。SoD：requester 自批 → 403
//                                          | （server-driven，前端不硬判）。
//   刷新/断网恢复                          | page.reload() 后经 REST 投影恢复 run/
//                                          | 审批状态（spec §5 恢复语义）。
//
// 未覆盖腿（计划实现，非本 spec 虚报）：spec §5 要求 Run detail 展示 actor、
// AgentVersion、attempts、artifacts、failure、cost placeholder——当前 UI 未渲染这些
// 字段，待前端补齐后扩断言。
//
// 后端边界（mock 模式，operator 已授权）：真实栈 e2e 需 Keycloak 登录编排 + Temporal
// （runs router 组装期必需 TEMPORAL_TARGET），本机均不可用（ADR-012 反例 5 / s2.md
// §6 例外仍在）。本 spec 在网络层模拟后端契约——响应形状对齐 src/zhiwei/api/runs.py
// 的 Pydantic 投影（RunRecord/RunDetail/ApprovalRequestView/DecisionResult），SoD 与
// PEP 头语义对齐真实后端；OIDC 登录腿由 tenancy.spec.ts 契约覆盖（其例外登记不变）。
// 会话经模拟 GET /api/v1/me + members 直接引导（session.tsx 的真实解析路径仍被执行）。
// 不发任何真实外部请求（不调 live 模型/外部服务）；未模拟的 /api 路径一律 500 显式
// 失败（fail loud，防止静默穿透到 dev proxy）。

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
const APPROVER_ID = "4a3e5ad8-f81e-431d-937f-55b98def2bf2";
const CSRF = "e2e-csrf-token";

interface TaskProjection {
  status: string;
  error: string | null;
}

interface ApprovalRecord {
  request_id: string;
  run_id: string;
  task_id: string;
  status: "pending" | "approved" | "rejected";
  requester: string;
}

// mock 后端的 run 聚合状态（任务词汇对齐 runtime/reducer.py：scheduled/started/
// completed/failed；run：running/completed/failed）
interface RunState {
  run_id: string;
  status: "running" | "completed" | "failed";
  organization_id: string;
  tasks: Record<string, TaskProjection>;
  approval: ApprovalRecord | null;
}

interface MockState {
  runs: RunState[];
  // 捕获 mutation 请求以断言客户端 PEP 契约（CSRF + Idempotency-Key）
  lastCreateRun: { headers: Record<string, string>; body: unknown } | null;
  lastDecision: { headers: Record<string, string>; body: unknown } | null;
}

type Actor = "builder" | "approver";

const ACTOR_PRINCIPAL: Record<Actor, string> = {
  builder: BUILDER_ID,
  approver: APPROVER_ID,
};

function newState(): MockState {
  return { runs: [], lastCreateRun: null, lastDecision: null };
}

// 任务图推进：每次 GET /runs/{id} 投影读触发至多一步演进（模拟 workflow 事件落账
// 后的投影更新）。审批 pending 时推进挂起——人类决策是唯一解锁路径（审批工作流语义）。
// 注意：React StrictMode（dev）双触发 effect，每次 Open 实际命中两次 GET——演进按
// 状态门控而非计数门控，双触发不破坏确定性。
function advance(run: RunState): void {
  if (run.status !== "running") return;
  const plan = run.tasks.plan;
  const execute = run.tasks.execute;
  if (plan.status === "scheduled") {
    plan.status = "started";
  } else if (plan.status === "started") {
    plan.status = "completed";
  } else if (execute.status === "scheduled" && run.approval === null) {
    run.approval = {
      request_id: crypto.randomUUID(),
      run_id: run.run_id,
      task_id: "execute",
      status: "pending",
      requester: BUILDER_ID,
    };
  } else if (execute.status === "scheduled" && run.approval?.status === "approved") {
    execute.status = "started";
  } else if (execute.status === "started") {
    execute.status = "completed";
    run.status = "completed";
  } else if (execute.status === "scheduled" && run.approval?.status === "rejected") {
    execute.status = "failed";
    execute.error = "approval rejected";
    run.status = "failed";
  }
  // 审批 pending → hold（等待人类决策）
}

function runDetail(run: RunState) {
  return {
    run_id: run.run_id,
    status: run.status,
    organization_id: run.organization_id,
    tasks: run.tasks,
  };
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// 网络层 mock：单一 catch-all 路由内部分发（响应形状对齐 api/runs.py 投影）。
function installApiMocks(context: BrowserContext, state: MockState, actor: Actor): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  context.route("**/api/**", async (route) => {
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
      // 成员列表是全局角色事实源（session.tsx 从中解析当前用户角色）
      return fulfill(route, 200, [
        { principal_id: BUILDER_ID, organization_id: ORG_ID, role_bindings: ["builder"] },
        { principal_id: APPROVER_ID, organization_id: ORG_ID, role_bindings: ["approver"] },
      ]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }

    // runtime 面（api/runs.py 契约）
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(
        route,
        200,
        state.runs.map((r) => ({
          run_id: r.run_id,
          status: r.status,
          organization_id: r.organization_id,
        }))
      );
    }
    if (path === "/api/v1/runs" && method === "POST") {
      const body = req.postDataJSON() as { template?: string; workspace_id?: string };
      state.lastCreateRun = { headers: req.headers(), body };
      const run: RunState = {
        run_id: crypto.randomUUID(),
        status: "running",
        organization_id: ORG_ID,
        tasks: {
          plan: { status: "scheduled", error: null },
          execute: { status: "scheduled", error: null },
        },
        approval: null,
      };
      state.runs.push(run);
      return fulfill(route, 201, { run_id: run.run_id });
    }
    const runMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})$/);
    if (runMatch && method === "GET") {
      const run = state.runs.find((r) => r.run_id === runMatch[1]);
      if (!run) return fulfill(route, 404, { detail: "run not found" });
      advance(run);
      return fulfill(route, 200, runDetail(run));
    }
    const approvalMatch = path.match(
      /^\/api\/v1\/runs\/([0-9a-f-]{36})\/approvals$/
    );
    if (approvalMatch && method === "GET") {
      const run = state.runs.find((r) => r.run_id === approvalMatch[1]);
      if (!run) return fulfill(route, 404, { detail: "run not found" });
      const pending =
        run.approval && run.approval.status === "pending" ? [run.approval] : [];
      return fulfill(route, 200, pending);
    }
    const decisionMatch = path.match(
      /^\/api\/v1\/runs\/([0-9a-f-]{36})\/approvals\/([0-9a-f-]{36})\/decision$/
    );
    if (decisionMatch && method === "POST") {
      const run = state.runs.find((r) => r.run_id === decisionMatch[1]);
      const approval = run?.approval;
      if (!run || !approval || approval.request_id !== decisionMatch[2]) {
        return fulfill(route, 404, { detail: "approval not found" });
      }
      const body = req.postDataJSON() as { decision?: string; reason?: string };
      state.lastDecision = { headers: req.headers(), body };
      // SoD：requester 不能决策自己的审批（真实后端同语义，server-driven 403）
      if (ACTOR_PRINCIPAL[actor] === approval.requester) {
        return fulfill(route, 403, {
          detail: "SoD: requester cannot decide own approval request",
        });
      }
      if (body.decision !== "approved" && body.decision !== "rejected") {
        return fulfill(route, 422, { detail: "invalid decision enum" });
      }
      approval.status = body.decision;
      return fulfill(route, 200, {
        request_id: approval.request_id,
        decision: body.decision,
        accepted: true,
      });
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

// Builder 经 UI 创建 run 并推进到「审批挂起」状态（Back→Open 触发投影刷新；
// StrictMode 下每次 Open 两次投影读 → plan 两步走完 + 创建审批）。
async function createRunPendingApproval(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByLabel("Template").selectOption("approval-chain");
  await page.getByRole("button", { name: "New run" }).click();
  await expect(page.getByRole("heading", { name: "Run", exact: true })).toBeVisible();
  await expect(page.getByText("Status: running")).toBeVisible();
  // Open#1：plan scheduled → started → completed
  await page.getByRole("button", { name: "Back" }).click();
  await page.getByRole("button", { name: "Open" }).click();
  await expect(page.getByText("plan: completed")).toBeVisible();
  // Open#2：创建审批请求，execute 挂起等待决策
  await page.getByRole("button", { name: "Back" }).click();
  await page.getByRole("button", { name: "Open" }).click();
  await expect(
    page.getByText(`task execute (pending, requester ${BUILDER_ID})`)
  ).toBeVisible();
}

// 全局 browser fixture 由 @playwright/test 注入（test 参数解构获取）。

// ---------------------------------------------------------------------------
// Builder 侧：创建 → 推进 → SoD 自批拒绝（server-driven 403）
// ---------------------------------------------------------------------------

test.describe("S2 runtime approval — Builder", () => {
  test("creates run, observes task graph advance via projection refresh, blocked from self-approval", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();

    await createRunPendingApproval(page);

    // mutation PEP 契约：POST /runs 必须携带非空 Idempotency-Key + CSRF（api.ts）
    expect(state.lastCreateRun).not.toBeNull();
    expect(UUID_RE.test(state.lastCreateRun!.headers["idempotency-key"])).toBe(true);
    expect(state.lastCreateRun!.headers["x-csrf-token"]).toBe(CSRF);
    expect(state.lastCreateRun!.body).toMatchObject({
      template: "approval-chain",
      workspace_id: WS_ID,
    });

    // SoD：builder 是 requester，自批被 server 拒绝（403），前端展示 server-driven
    // 错误而非硬判（§4 纪律）
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(page.getByText(/API 403/)).toBeVisible();

    // 刷新恢复：projection 状态仍在（run 列表 + 审批挂起均可恢复）
    await page.reload();
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
    await expect(page.getByText("running")).toBeVisible();
    await page.getByRole("button", { name: "Open" }).click();
    await expect(
      page.getByText(`task execute (pending, requester ${BUILDER_ID})`)
    ).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// Approver 侧：独立账号批准 → run 完成 → 刷新恢复
// ---------------------------------------------------------------------------

test.describe("S2 runtime approval — Approver", () => {
  test("approves pending request in independent session; run completes and restores after reload", async ({ browser }) => {
    const state = newState();
    const builder = await newContextWithMocks(browser, state, "builder");
    const builderPage = await builder.newPage();
    await createRunPendingApproval(builderPage);

    // Approver 独立账号（独立浏览器上下文 = 独立会话）
    const approver = await newContextWithMocks(browser, state, "approver");
    const page = await approver.newPage();
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
    await page.getByRole("button", { name: "Open" }).click();
    await expect(
      page.getByText(`task execute (pending, requester ${BUILDER_ID})`)
    ).toBeVisible();

    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(page.getByText("No pending approvals")).toBeVisible();

    // 决策请求 PEP 契约：CSRF + Idempotency-Key + fail-closed 枚举 body
    expect(state.lastDecision).not.toBeNull();
    expect(state.lastDecision!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastDecision!.headers["idempotency-key"])).toBe(true);
    expect(state.lastDecision!.body).toMatchObject({ decision: "approved" });

    // 决策后投影恢复：run 推进到 completed（审批解锁 execute → 完成）
    await page.reload();
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
    await page.getByRole("button", { name: "Open" }).click();
    await expect(page.getByText("Status: completed")).toBeVisible();
    await expect(page.getByText("execute: completed")).toBeVisible();

    await builder.close();
    await approver.close();
  });

  test("rejection fails the task and the run (fail-closed decision path)", async ({ browser }) => {
    const state = newState();
    const builder = await newContextWithMocks(browser, state, "builder");
    const builderPage = await builder.newPage();
    await createRunPendingApproval(builderPage);

    const approver = await newContextWithMocks(browser, state, "approver");
    const page = await approver.newPage();
    await page.goto("/");
    await page.getByRole("button", { name: "Open" }).click();
    await expect(
      page.getByText(`task execute (pending, requester ${BUILDER_ID})`)
    ).toBeVisible();

    await page.getByRole("button", { name: "Reject", exact: true }).click();
    await expect(page.getByText("No pending approvals")).toBeVisible();
    expect(state.lastDecision!.body).toMatchObject({ decision: "rejected" });

    await page.reload();
    await page.getByRole("button", { name: "Open" }).click();
    await expect(page.getByText("Status: failed")).toBeVisible();
    await expect(page.getByText("execute: failed — approval rejected")).toBeVisible();

    await builder.close();
    await approver.close();
  });
});
