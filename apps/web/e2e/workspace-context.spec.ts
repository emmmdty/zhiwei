// S11 followup-2 任务二 RED：workspace 上下文死环修复（mock 面，进 CI web-e2e job）。
//
// 死环根因（docs/handoffs/s11-followup-2-publish-demo.md §2.1，已逐条验证）：
// /me 在无 ws 头时 workspace_id 恒 null（api/auth.py org 兜底、ws 硬编码 null）；
// 前端仅在 me.context.workspace_id 非空时注入 X-ZhiWei-Workspace（session.tsx）；
// 全前端无任何写入口 → /me null → 不发头 → 下次 /me 仍 null。
//
// 修复契约（纯前端；服务端零改动，/me 无头语义不变 → 冻结契约零触碰）：
// - WorkspacesPanel workspace 行可选中（button + aria-pressed，S10 a11y 基线）；
//   点击 = 写本地选择（localStorage，key 按组织作用域 `zhiwei.ws.<orgId>`，刷新
//   不丢、多 org 不串）→ refresh() 一次，/me 带头回显确认；
// - 头注入优先级 = 本地选中值优先、/me 回显兜底；选中后后续分区 API 请求携带
//   X-ZhiWei-Workspace；
// - 失效降级：本地选择失效（membership 被撤 → 服务端 GET 404 fail closed）时，
//   清除该选择、去 ws 头重试 /me，回退到仅 org 上下文——不出现假登录态。
//
// mock 语义镜像真实服务端（resolve_context 逐条校验声明并回显：GET 下
// MembershipScopeError → 404；org 未声明时单 org 兜底、ws 恒 null）。
// 网络层 mock：单一 catch-all 路由内部分发；未模拟路径 500 fail loud。
// 权限由 server PEP 强制：前端多发头只是声明请求，不构成越权（§2.2 评估结论）。

import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

// 固定标识（本 spec 内 mock 域；不依赖 compose 种子数据）
const ORG_ID = "3a1a8d1c-a63f-4bed-87d1-b67948aea7ac";
const WS_A = "6f1c2a34-9b7e-4d0a-8f61-0c5b2d7e9a11";
const WS_B = "6f1c2a34-9b7e-4d0a-8f61-0c5b2d7e9a12";
// 已撤销 membership 的 workspace：mock 对它返回 404（fail closed 降级触发面）
const WS_REVOKED = "6f1c2a34-9b7e-4d0a-8f61-0c5b2d7e9a99";
const PRINCIPAL_ID = "3383f6a7-d17b-44c2-802c-d67c3974e13a";
const CSRF = "e2e-csrf-token";
const SELECTION_KEY = `zhiwei.ws.${ORG_ID}`;

interface MeCall {
  status: number;
  organization: string | null;
  workspace: string | null;
}

interface MockState {
  meCalls: MeCall[];
  evalsCalls: { organization: string | null; workspace: string | null }[];
}

function newState(): MockState {
  return { meCalls: [], evalsCalls: [] };
}

function tenantHeaders(req: { headers: () => Record<string, string> }): {
  organization: string | null;
  workspace: string | null;
} {
  const h = req.headers();
  return {
    organization: h["x-zhiwei-organization"] ?? null,
    workspace: h["x-zhiwei-workspace"] ?? null,
  };
}

// /me 语义逐分支镜像 api/auth.py + identity/sessions.py resolve_context：
// - org 未声明：单 org 兜底 → {org, ws: null}（principal-only actor 由 /me 落
//   organizations 兜底分支；ws 头无 org 头 → MembershipScopeError）；
// - org 已声明：membership 校验（本 mock 恒 member）→ ws 未声明回显 null、
//   ws 已声明回显该值（回显 = 校验通过后的 resolved context）；
// - ws 头声明了非成员 workspace → GET 404（fail closed，前端降级护栏的触发面）。
function handleMe(state: MockState, route: Route): void {
  const declared = tenantHeaders(route.request());
  if (declared.workspace !== null && declared.organization === null) {
    state.meCalls.push({ status: 404, ...declared });
    void route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "resource not found" }),
    });
    return;
  }
  if (declared.organization !== null && declared.organization !== ORG_ID) {
    state.meCalls.push({ status: 404, ...declared });
    void route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "resource not found" }),
    });
    return;
  }
  const staleWorkspace =
    declared.workspace !== null &&
    declared.workspace !== WS_A &&
    declared.workspace !== WS_B;
  if (staleWorkspace) {
    state.meCalls.push({ status: 404, ...declared });
    void route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "resource not found" }),
    });
    return;
  }
  state.meCalls.push({
    // 成功调用记录 resolved context（与 UI 实际收到的一致）：org 未声明时
    // 服务端走单 org 兜底，/me 回显的 organization_id 即兜底 org
    status: 200,
    organization: declared.organization ?? ORG_ID,
    workspace: declared.workspace,
  });
  void route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      principal: { id: PRINCIPAL_ID },
      organizations: [{ id: ORG_ID, status: "active" }],
      context: {
        organization_id: declared.organization ?? ORG_ID,
        workspace_id: declared.workspace,
      },
      csrf_token: CSRF,
    }),
  });
}

function installApiMocks(context: BrowserContext, state: MockState): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  context.route("/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();

    if (path === "/api/v1/me" && method === "GET") {
      handleMe(state, route);
      return;
    }
    if (path === "/api/v1/organizations" && method === "GET") {
      return fulfill(route, 200, [{ id: ORG_ID, status: "active" }]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/members` && method === "GET") {
      return fulfill(route, 200, [
        {
          principal_id: PRINCIPAL_ID,
          organization_id: ORG_ID,
          role_bindings: ["org_owner", "agent_builder"],
        },
      ]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [
        { id: WS_A, name: "Engineering" },
        { id: WS_B, name: "Research" },
      ]);
    }
    if (path === "/api/v1/evals" && method === "GET") {
      state.evalsCalls.push(tenantHeaders(req));
      return fulfill(route, 200, []);
    }
    // 未模拟路径一律 500：fail loud，防止测试在假成功上通过
    return fulfill(route, 500, { detail: `mock not implemented: ${method} ${path}` });
  });
}

async function openAuthenticated(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByText(/signed in as/i)).toBeVisible();
  await expect(page.getByRole("link", { name: /sign in/i })).toHaveCount(0);
}

// ---------------------------------------------------------------------------
// (1) 选中路径：点击 workspace → /me 带头回显确认 → 后续分区 API 携带
//     X-ZhiWei-Workspace（修复前：无 button 可点 → 失败于首次交互元素查找）
// ---------------------------------------------------------------------------

test.describe("workspace context selection", () => {
  test("selecting a workspace round-trips via /me echo and stamps tenant headers on section APIs", async ({
    browser,
  }) => {
    const state = newState();
    const context: BrowserContext = await browser.newContext();
    installApiMocks(context, state);
    const page: Page = await context.newPage();

    await openAuthenticated(page);
    // 初始死环面：/me 无头回显 ws null → 无任何分区数据请求带 ws 头
    expect(state.meCalls.length).toBeGreaterThan(0);
    for (const call of state.meCalls) expect(call.workspace).toBeNull();

    // WorkspacesPanel：workspace 行可选中（button + aria-pressed）
    const engineering = page.getByRole("button", { name: "Engineering" });
    await expect(engineering).toBeVisible();
    await expect(engineering).toHaveAttribute("aria-pressed", "false");
    await engineering.click();

    // 选中 → refresh()：/me 带 {org, ws} 头回显确认（本地选中值优先）
    await expect(engineering).toHaveAttribute("aria-pressed", "true");
    const confirmed = state.meCalls.find(
      (call) => call.status === 200 && call.workspace === WS_A
    );
    expect(confirmed).toBeTruthy();
    expect(confirmed!.organization).toBe(ORG_ID);

    // 后续分区 API（Evals）请求携带 X-ZhiWei-Workspace（死环解除的落点断言）
    await page.getByRole("button", { name: "Evals" }).click();
    await expect(page.getByRole("heading", { name: "Evals" })).toBeVisible();
    expect(state.evalsCalls.length).toBeGreaterThan(0);
    for (const call of state.evalsCalls) {
      expect(call.organization).toBe(ORG_ID);
      expect(call.workspace).toBe(WS_A);
    }

    // 刷新后选中态保持（localStorage org 作用域持久化 → /me 带头回显）
    await page.reload();
    await openAuthenticated(page);
    await expect(page.getByRole("button", { name: "Engineering" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
    const evalsCallsAfterReload = state.evalsCalls.length;
    await page.getByRole("button", { name: "Evals" }).click();
    await expect
      .poll(() => state.evalsCalls.length, { timeout: 5_000 })
      .toBeGreaterThan(evalsCallsAfterReload);
    expect(state.evalsCalls[evalsCallsAfterReload].workspace).toBe(WS_A);

    // 切换选中：aria-pressed 跟随 /me 回显迁移（单选中语义）
    await page.getByRole("button", { name: "Research" }).click();
    await expect(page.getByRole("button", { name: "Research" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
    await expect(page.getByRole("button", { name: "Engineering" })).toHaveAttribute(
      "aria-pressed",
      "false"
    );
  });

  // -------------------------------------------------------------------------
  // (2) 降级护栏：本地选择失效（membership 被撤 → /me GET 404 fail closed）→
  //     清除选择、去 ws 头重试，回退仅 org 上下文——不出现假登录态
  // -------------------------------------------------------------------------

  test("stale selection degrades: 404 on /me clears the choice and retries without workspace header", async ({
    browser,
  }) => {
    const state = newState();
    const context: BrowserContext = await browser.newContext();
    installApiMocks(context, state);
    // 预置失效选择：storage 里存了一个已不属于该用户的 workspace
    await context.addInitScript(
      ([key, value]) => {
        window.localStorage.setItem(key!, value!);
      },
      [SELECTION_KEY, WS_REVOKED] as [string, string]
    );
    const page: Page = await context.newPage();

    await openAuthenticated(page);

    // 降级动作：带头 /me 404 → 清除选择 → 去头重试成功（仅 org 上下文）
    const withHeader = state.meCalls.find((call) => call.workspace === WS_REVOKED);
    expect(withHeader).toBeTruthy();
    expect(withHeader!.status).toBe(404);
    const fallback = state.meCalls.find(
      (call) => call.status === 200 && call.workspace === null && call.organization !== null
    );
    expect(fallback).toBeTruthy();

    // 选择已清除（下次刷新不再带上失效头）
    const remaining = await page.evaluate((key) => window.localStorage.getItem(key), SELECTION_KEY);
    expect(remaining).toBeNull();

    // 仍登录态（非假登录），分区 API 以仅 org 上下文发出（不带失效 ws 头）
    await page.getByRole("button", { name: "Evals" }).click();
    await expect(page.getByRole("heading", { name: "Evals" })).toBeVisible();
    expect(state.evalsCalls.length).toBeGreaterThan(0);
    for (const call of state.evalsCalls) {
      expect(call.organization).toBe(ORG_ID);
      expect(call.workspace).toBeNull();
    }
  });
});
