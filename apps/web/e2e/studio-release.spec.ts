// S10-T3 Studio release e2e（specs/s10 §3 → plan Task 3）：S9 发布流集成 journey。
//
// 与后端契约的映射（网络层 mock，形状逐字段对齐真实端点；S9 命令面与
// tests/contract/api/test_releases_api.py、T3 支撑读面与
// tests/contract/api/test_agents_release_support_api.py 的 mock 侧镜像）：
//   GET  /api/v1/agents/{id}/release-readiness → {ready, missing:[{kind, detail}]}
//                                                （eval_seal 真算；不可泛化计算的
//                                                检查以 kind=unknown 如实呈报）
//   GET  /api/v1/agents/{id}/diff              → {fields:[{field,from,to,kind}]}
//                                                （kind: dependency/permission/
//                                                budget/schema/other）
//   POST /api/v1/agents/{id}/releases          → 201 ReleaseView（S9 命令）
//   GET  /api/v1/releases                      → ReleaseView 数组（S9 列表）
//   POST /api/v1/releases/{id}/advance         → 角色映射 mock：builder 推进 draft
//                                                侧，reviewer/approver/
//                                                release_manager 各持一段；无角色
//                                                → 409 {reason,message}（域层
//                                                ReleaseTransitionDenied 镜像）
//   GET  /api/v1/releases/{id}/manifest        → manifest 全字段（digest verbatim）
//   POST /api/v1/releases/{id}/rollback        → {applies_to:"new_runs_only", ...}
//
// 纪律：
// - 无新发布状态机：mock 的 advance 按冻结迁移矩阵逐边校验角色（SoD 双重防线
//   的 mock 侧镜像）；UI 不存在 PATCH 生命周期旁路；
// - 不发任何真实外部请求；未模拟的 /api 路径一律 500 显式失败（fail loud）；
// - catch-all 路由 glob 根锚定 "/api/**"（模块脚本不受拦截，studio-draft 同款）。

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
const AGENT_ID = "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c99";
// RED 修订说明：原常量末组只有 11 个 hex 位（35 字符，非良构 UUID），本 spec
// 自己的 advance/rollback 路由按 36 字符 id 正则匹配——请求永远落入 fail-loud
// 500，journey 不可能通过。仅修 fixture 常量，不动任何断言。
const RELEASE_ID = "9f8e7d6c-5b4a-3c2d-1e0f-a1b2c3d4e5f0";
const IN_FLIGHT_RUN_ID = "4c5d6e7f-8a9b-0c1d-2e3f-4a5b6c7d8e9f";

const DIGEST = "sha256:" + "a".repeat(64);
const PACK_OLD = "sha256:" + "a".repeat(64);
const PACK_NEW = "sha256:" + "9".repeat(64);

type Actor = "builder" | "reviewer" | "approver" | "release_manager";

const ACTOR_PRINCIPAL: Record<Actor, string> = {
  builder: "3383f6a7-d17b-44c2-802c-d67c3974e13a",
  reviewer: "5b4f9e2a-1c3d-4e5f-8a6b-7c8d9e0f1a2b",
  approver: "6c7d8e9f-0a1b-2c3d-4e5f-6a7b8c9d0e1f",
  release_manager: "7d8e9f0a-1b2c-3d4e-5f6a-7b8c9d0e1f2a",
};

// 平台角色绑定（session.tsx 归一后）；reviewer/release_manager 平台角色都是
// workspace_admin（api/releases.py _RELEASE_ROLE_PLATFORM_ROLES 并集语义）
const ACTOR_PLATFORM_ROLES: Record<Actor, string[]> = {
  builder: ["builder"],
  reviewer: ["workspace_admin"],
  approver: ["approver"],
  release_manager: ["workspace_admin"],
};

interface StudioReleaseView {
  release_id: string;
  agent_id: string;
  agent_version: number;
  state: string;
  manifest_digest: string;
  default_version: number | null;
}

interface ReadinessCheck {
  kind: string;
  detail: string;
}

interface MockState {
  drafts: { agent_id: string; name: string; revision: number; lifecycle: string }[];
  readiness: { ready: boolean; missing: ReadinessCheck[] };
  releases: StudioReleaseView[];
  diff: { fields: { field: string; from: string | null; to: string | null; kind: string }[] };
  manifest: {
    release_id: string;
    agent_id: string;
    agent_version: number;
    manifest_digest: string;
    pack_digest: string;
    model_digest: string;
    knowledge_digest: string;
    memory_digest: string;
    capability_digest: string;
    policy_digest: string;
    eval_digests: string[];
    approver: string;
    rollout: { default_version: number | null; cohorts: never[] };
    rollback: { in_flight: string };
  };
  rollbackOutcome: Record<string, unknown> | null;
  advanceRequests: { actor: Actor; target: string }[];
}

// 冻结迁移矩阵（agents/release.py ALLOWED_RELEASE_TRANSITIONS 镜像）：每边的
// 授权 release 角色。mock 的 advance 按此逐边判 SoD——UI/mock 都不发明第二套。
const TRANSITIONS: { from: string; next: string; roles: string[] }[] = [
  { from: "draft", next: "sandbox", roles: ["builder"] },
  { from: "sandbox", next: "evaluated", roles: ["builder"] },
  { from: "evaluated", next: "review", roles: ["reviewer"] },
  { from: "review", next: "staged", roles: ["approver"] },
  { from: "staged", next: "published", roles: ["release_manager"] },
  { from: "published", next: "deprecated", roles: ["release_manager"] },
  { from: "deprecated", next: "retired", roles: ["release_manager"] },
];

// 平台角色 → release 角色（api/releases.py _RELEASE_ROLE_PLATFORM_ROLES 镜像）
function releaseRolesFor(actor: Actor): Set<string> {
  const platform = new Set(ACTOR_PLATFORM_ROLES[actor]);
  const roles = new Set<string>();
  if (platform.has("agent_builder") || platform.has("builder")) roles.add("builder");
  if (platform.has("workspace_admin")) {
    roles.add("reviewer");
    roles.add("approver");
    roles.add("release_manager");
  }
  if (platform.has("approver")) roles.add("approver");
  return roles;
}

function releaseView(state: string): StudioReleaseView {
  return {
    release_id: RELEASE_ID,
    agent_id: AGENT_ID,
    agent_version: 1,
    state,
    manifest_digest: "sha256:" + "3".repeat(64),
    default_version: 2,
  };
}

function newState(): MockState {
  return {
    drafts: [
      {
        agent_id: AGENT_ID,
        name: "studio-agent",
        revision: 1,
        lifecycle: "draft",
      },
    ],
    readiness: {
      ready: false,
      missing: [
        { kind: "eval_seal", detail: "no sealed eval run is bound to this agent" },
        {
          kind: "unknown",
          detail: "connection readiness cannot be computed from the agent record",
        },
        {
          kind: "unknown",
          detail: "capability publish state cannot be computed from the agent record",
        },
      ],
    },
    releases: [],
    diff: {
      fields: [
        { field: "pack_digest", from: PACK_OLD, to: PACK_NEW, kind: "dependency" },
        { field: "budget.max_model_calls", from: "2", to: "5", kind: "budget" },
      ],
    },
    manifest: {
      release_id: RELEASE_ID,
      agent_id: AGENT_ID,
      agent_version: 1,
      manifest_digest: "sha256:" + "3".repeat(64),
      pack_digest: DIGEST,
      model_digest: "sha256:" + "b".repeat(64),
      knowledge_digest: "sha256:" + "c".repeat(64),
      memory_digest: "sha256:" + "d".repeat(64),
      capability_digest: "sha256:" + "e".repeat(64),
      policy_digest: "sha256:" + "f".repeat(64),
      eval_digests: ["sha256:" + "1".repeat(64)],
      approver: "alice",
      rollout: { default_version: 2, cohorts: [] },
      rollback: { in_flight: "complete" },
    },
    rollbackOutcome: null,
    advanceRequests: [],
  };
}

function installMocks(context: BrowserContext, state: MockState, actor: Actor): void {
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

    // 会话引导（session.tsx 消费的真实契约，studio-draft 同款）
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
      return fulfill(route, 200, [
        {
          principal_id: ACTOR_PRINCIPAL[actor],
          organization_id: ORG_ID,
          role_bindings: ACTOR_PLATFORM_ROLES[actor],
        },
      ]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    if (path === `/api/v1/workspaces/${WS_ID}/groups` && method === "GET") {
      return fulfill(route, 200, []);
    }

    // ---- Studio draft 面（DraftEditor 装载所需的最小镜像）----
    if (path === "/api/v1/agents" && method === "GET") {
      return fulfill(route, 200, state.drafts);
    }
    const agentMatch = path.match(/^\/api\/v1\/agents\/([0-9a-f-]{36})$/);
    if (agentMatch && method === "GET") {
      const draft = state.drafts.find((d) => d.agent_id === agentMatch[1]);
      if (!draft) return fulfill(route, 404, { detail: "agent not found" });
      return fulfill(route, 200, { ...draft, capabilities: [], task_graph: null }, {
        ETag: `"${draft.revision}"`,
      });
    }

    // ---- T3 支撑读面（test_agents_release_support_api.py 契约镜像）----
    if (path === `/api/v1/agents/${AGENT_ID}/release-readiness` && method === "GET") {
      return fulfill(route, 200, state.readiness);
    }
    if (path === `/api/v1/agents/${AGENT_ID}/diff` && method === "GET") {
      return fulfill(route, 200, state.diff);
    }

    // ---- S9 release commands（api/releases.py / api/agents.py 契约镜像）----
    if (path === `/api/v1/agents/${AGENT_ID}/releases` && method === "POST") {
      const view = releaseView("draft");
      state.releases.push(view);
      return fulfill(route, 201, view);
    }
    if (path === "/api/v1/releases" && method === "GET") {
      return fulfill(route, 200, state.releases);
    }
    const advanceMatch = path.match(/^\/api\/v1\/releases\/([0-9a-f-]{36})\/advance$/);
    if (advanceMatch && method === "POST") {
      const target = (req.postDataJSON() as { target_state: string }).target_state;
      state.advanceRequests.push({ actor, target });
      const current = state.releases[0]?.state;
      const edge = TRANSITIONS.find((t) => t.from === current && t.next === target);
      if (!edge) {
        return fulfill(route, 409, {
          reason: "release_transition_denied",
          message: `release transition ${current} -> ${target} is not allowed`,
        });
      }
      const held = releaseRolesFor(actor);
      if (![...edge.roles].some((role) => held.has(role))) {
        return fulfill(route, 409, {
          reason: "release_transition_denied",
          message: `role '${[...held][0] ?? "none"}' may not advance release ${current} -> ${target}`,
        });
      }
      const view = releaseView(target);
      state.releases[0] = view;
      return fulfill(route, 200, view);
    }
    if (path === `/api/v1/releases/${RELEASE_ID}/manifest` && method === "GET") {
      return fulfill(route, 200, state.manifest);
    }
    const rollbackMatch = path.match(/^\/api\/v1\/releases\/([0-9a-f-]{36})\/rollback$/);
    if (rollbackMatch && method === "POST") {
      const body = req.postDataJSON() as { to_version: number };
      state.rollbackOutcome = {
        release_id: RELEASE_ID,
        applies_to: "new_runs_only",
        executed: false,
        in_flight_disposition: "terminate",
        in_flight_run_ids: [IN_FLIGHT_RUN_ID],
        default_version: body.to_version,
      };
      return fulfill(route, 200, state.rollbackOutcome);
    }

    // 未模拟路径显式失败（fail loud，不静默穿透 dev proxy）
    return fulfill(route, 500, { detail: `unmocked: ${method} ${path}` });
  });
}

async function newContext(
  browser: Browser,
  state: MockState,
  actor: Actor,
): Promise<BrowserContext> {
  const context = await browser.newContext();
  installMocks(context, state, actor);
  return context;
}

async function openStudioDraft(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByRole("button", { name: "Agent Studio" }).click();
  await page
    .getByRole("row", { name: new RegExp(AGENT_ID) })
    .getByRole("button", { name: "Open" })
    .click();
  await expect(page.getByRole("heading", { name: "Release", exact: true })).toBeVisible();
}

async function fillDigests(page: Page): Promise<void> {
  for (const label of [
    "Pack digest",
    "Model digest",
    "Knowledge digest",
    "Memory digest",
    "Capability digest",
    "Policy digest",
  ]) {
    await page.getByLabel(label).fill(DIGEST);
  }
}

// ---------------------------------------------------------------------------
// (a) readiness 如实呈现：missing eval_seal + unknown 检查逐条 verbatim
// ---------------------------------------------------------------------------

test.describe("S10 studio release — readiness", () => {
  test("readiness shows the missing eval seal and unknown checks honestly", async ({
    browser,
  }) => {
    const state = newState();
    const context = await newContext(browser, state, "builder");
    const page = await context.newPage();
    await openStudioDraft(page);

    await page.getByRole("button", { name: "Check readiness" }).click();
    const list = page.getByRole("list", { name: "Release readiness" });
    await expect(
      list.getByText("missing: eval_seal — no sealed eval run is bound to this agent"),
    ).toBeVisible();
    await expect(
      list.getByText(
        "unknown: connection readiness cannot be computed from the agent record",
      ),
    ).toBeVisible();
    await expect(
      list.getByText(
        "unknown: capability publish state cannot be computed from the agent record",
      ),
    ).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (b) builder：create release → stepper draft→sandbox；publish 按钮对 builder 禁用
// ---------------------------------------------------------------------------

test.describe("S10 studio release — builder journey", () => {
  test("builder creates a release, advances draft to sandbox, publish stays disabled", async ({
    browser,
  }) => {
    const state = newState();
    const context = await newContext(browser, state, "builder");
    const page = await context.newPage();
    await openStudioDraft(page);

    await fillDigests(page);
    await page.getByRole("button", { name: "Create draft release" }).click();
    await expect(page.getByText(RELEASE_ID)).toBeVisible();
    await expect(page.getByText("State: draft")).toBeVisible();

    const stepper = page.getByRole("list", { name: "Release lifecycle" });
    await expect(stepper.getByText("draft (current)")).toBeVisible();

    // builder 推进 draft → sandbox（矩阵边授权 builder）
    await page.getByRole("button", { name: "Advance draft to sandbox" }).click();
    await expect(page.getByText("State: sandbox")).toBeVisible();
    await expect(stepper.getByText("sandbox (current)")).toBeVisible();

    // publish 边要求 release_manager：builder 视角恒禁用（角色 × 状态双门）
    await expect(
      page.getByRole("button", { name: "Advance staged to published" }),
    ).toBeDisabled();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (c) SoD 链：reviewer 复核 → approver 批准 → release_manager 发布（分角色上下文，
//     共享同一 mock 状态——同一 release 实体沿矩阵推进）
// ---------------------------------------------------------------------------

test.describe("S10 studio release — separation of duties chain", () => {
  test("reviewer advances to review, approver stages, release_manager publishes", async ({
    browser,
  }) => {
    const state = newState();
    state.releases.push(releaseView("evaluated"));

    // reviewer：evaluated → review
    const reviewerCtx = await newContext(browser, state, "reviewer");
    const reviewerPage = await reviewerCtx.newPage();
    await openStudioDraft(reviewerPage);
    await reviewerPage.getByRole("button", { name: "Load releases" }).click();
    await reviewerPage
      .getByRole("list", { name: "Agent releases" })
      .getByRole("button", { name: "Open" })
      .click();
    await expect(reviewerPage.getByText("State: evaluated")).toBeVisible();
    await reviewerPage.getByRole("button", { name: "Advance evaluated to review" }).click();
    await expect(reviewerPage.getByText("State: review")).toBeVisible();
    await reviewerCtx.close();

    // approver：review → staged
    const approverCtx = await newContext(browser, state, "approver");
    const approverPage = await approverCtx.newPage();
    await openStudioDraft(approverPage);
    await approverPage.getByRole("button", { name: "Load releases" }).click();
    await approverPage
      .getByRole("list", { name: "Agent releases" })
      .getByRole("button", { name: "Open" })
      .click();
    await approverPage.getByRole("button", { name: "Advance review to staged" }).click();
    await expect(approverPage.getByText("State: staged")).toBeVisible();
    await approverCtx.close();

    // release_manager：staged → published（发布边）
    const managerCtx = await newContext(browser, state, "release_manager");
    const managerPage = await managerCtx.newPage();
    await openStudioDraft(managerPage);
    await managerPage.getByRole("button", { name: "Load releases" }).click();
    await managerPage
      .getByRole("list", { name: "Agent releases" })
      .getByRole("button", { name: "Open" })
      .click();
    await managerPage.getByRole("button", { name: "Advance staged to published" }).click();
    await expect(managerPage.getByText("State: published")).toBeVisible();

    // 全链只走了 S9 advance 命令，目标状态序列即矩阵边
    expect(state.advanceRequests.map((r) => r.target)).toEqual([
      "review",
      "staged",
      "published",
    ]);

    await managerCtx.close();
  });
});

// ---------------------------------------------------------------------------
// (d) 版本 diff：dependency/budget 变化按 kind 渲染
// ---------------------------------------------------------------------------

test.describe("S10 studio release — version diff", () => {
  test("diff renders dependency and budget changes by kind", async ({ browser }) => {
    const state = newState();
    const context = await newContext(browser, state, "builder");
    const page = await context.newPage();
    await openStudioDraft(page);

    await page.getByLabel("Diff from revision").fill("1");
    await page.getByLabel("Diff to revision").fill("2");
    await page.getByRole("button", { name: "Show diff" }).click();

    const list = page.getByRole("list", { name: "Agent version diff" });
    await expect(
      list.getByText(`pack_digest: ${PACK_OLD} → ${PACK_NEW} (dependency)`),
    ).toBeVisible();
    await expect(
      list.getByText("budget.max_model_calls: 2 → 5 (budget)"),
    ).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (e) manifest 不可变展示：digest 全字段 verbatim + approver 原样
// ---------------------------------------------------------------------------

test.describe("S10 studio release — immutable manifest", () => {
  test("manifest digests render verbatim with the approver untouched", async ({
    browser,
  }) => {
    const state = newState();
    state.releases.push(releaseView("staged"));
    const context = await newContext(browser, state, "builder");
    const page = await context.newPage();
    await openStudioDraft(page);

    await page.getByRole("button", { name: "Load releases" }).click();
    await page
      .getByRole("list", { name: "Agent releases" })
      .getByRole("button", { name: "Open" })
      .click();
    await page.getByRole("button", { name: "Load manifest" }).click();

    const list = page.getByRole("list", { name: "Release manifest" });
    // digest 全文渲染（Copy 按钮之外不截断）
    await expect(list.getByText(`pack digest: ${DIGEST}`)).toBeVisible();
    await expect(list.getByText(`knowledge digest: sha256:${"c".repeat(64)}`)).toBeVisible();
    await expect(list.getByText(`policy digest: sha256:${"f".repeat(64)}`)).toBeVisible();
    await expect(list.getByText("approver: alice")).toBeVisible();
    await expect(
      list.getByText(`eval digests: sha256:${"1".repeat(64)}`),
    ).toBeVisible();
    await expect(list.getByRole("button", { name: "Copy pack digest" })).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (f) 409 拒绝面：无角色的 advance 返回机器可读 reason 并结构化呈现
// ---------------------------------------------------------------------------

test.describe("S10 studio release — refusal surface", () => {
  test("role-denied advance surfaces the machine reason verbatim", async ({ browser }) => {
    const state = newState();
    state.releases.push(releaseView("evaluated"));
    const context = await newContext(browser, state, "builder");
    const page = await context.newPage();
    await openStudioDraft(page);

    await page.getByRole("button", { name: "Load releases" }).click();
    await page
      .getByRole("list", { name: "Agent releases" })
      .getByRole("button", { name: "Open" })
      .click();
    // builder 对 evaluated → review 无角色（SoD）：409 + reason/message 原样
    await page.getByRole("button", { name: "Advance evaluated to review" }).click();
    await expect(page.getByText(/refused: release_transition_denied/)).toBeVisible();
    await expect(page.getByText(/may not advance release evaluated -> review/)).toBeVisible();
    // 状态未被拒绝请求推进（域层拒绝不产生变更的 mock 侧镜像）
    await expect(page.getByText("State: evaluated")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (g) rollback：new-runs-only + 在途 Run 处置声明 verbatim
// ---------------------------------------------------------------------------

test.describe("S10 studio release — rollback", () => {
  test("rollback shows new-runs-only scope and the in-flight disposition", async ({
    browser,
  }) => {
    const state = newState();
    state.releases.push(releaseView("published"));
    const context = await newContext(browser, state, "release_manager");
    const page = await context.newPage();
    await openStudioDraft(page);

    await page.getByRole("button", { name: "Load releases" }).click();
    await page
      .getByRole("list", { name: "Agent releases" })
      .getByRole("button", { name: "Open" })
      .click();

    await page.getByLabel("Roll back to version").fill("1");
    await page.getByLabel("In-flight run IDs (comma separated)").fill(IN_FLIGHT_RUN_ID);
    await page.getByRole("button", { name: "Roll back" }).click();

    const outcome = page.getByRole("list", { name: "Rollback outcome" });
    await expect(outcome.getByText("applies to: new runs only")).toBeVisible();
    await expect(outcome.getByText("in-flight disposition: terminate")).toBeVisible();
    await expect(outcome.getByText(`in-flight run ids: ${IN_FLIGHT_RUN_ID}`)).toBeVisible();
    await expect(outcome.getByText("default version: 1")).toBeVisible();

    await context.close();
  });
});
