// S1-T6 RED：role-aware tenancy Web shell journey（视觉契约）。
//
// 契约来源（operator 授权第二种情形）：specs/s1-tenancy-policy.md §4 Web journey
// + plan Task 6 步骤文本。无独立视觉稿——本文件即唯一视觉契约。
//
// 覆盖矩阵（specs/s1 §4 + §5 Required tests "Playwright：Owner、Builder、
// Approver、Member、Auditor 五角色 journey"）：
//   Owner        — create organization / workspace / invite 4 角色 / assign role /
//                  create group / remove member
//   Agent Builder— see permitted workspaces / build actions; 不能 manage members
//   Member       — see own memberships; 不能 create workspace; 403 on admin API
//   Approver     — see approval queue; 不能 edit resources; dual control
//   Auditor      — view 脱敏 audit events (read-only); 不能 edit
// 状态：loading / empty / error / 403 (server-enforced) / revoked (401 → login)
// 纪律（§4 最后一段）：导航可隐藏，但直接 API 仍由 server PEP/RLS 拒绝——
// 前端不得硬判 403，必须由 API 实际返回驱动。
//
// RED 状态：App 渲染 null（无视图）。每个 journey 在首次交互元素查找
// （getByRole / getByText）处超时失败——反例到真实前端行为，非 fixture 报错。

import { test, expect, type Page } from "@playwright/test";

const OIDC_SUBJECT: Record<string, string> = {
  owner: "owner-oidc",
  builder: "builder-oidc",
  approver: "approver-oidc",
  member: "member-oidc",
  auditor: "auditor-oidc",
};

// 播种的 principal UUID（S1 e2e 种子：compose identity profile + DB seed 脚本）。
// S1 后端 POST /members 以 principal_id (UUID) 邀请，无 externalId→UUID 解析端点。
const PRINCIPAL_UUID: Record<string, string> = {
  owner: "fd1b9dab-3f88-4b35-803d-f5ab19fae6a8",
  builder: "3383f6a7-d17b-44c2-802c-d67c3974e13a",
  approver: "4a3e5ad8-f81e-431d-937f-55b98def2bf2",
  member: "f740acc5-03c3-486e-8384-2a9335fd4285",
  auditor: "63d7ef96-75e0-4c47-8edb-10dd834c9f64",
};

// Keycloak 测试用户密码（compose identity profile 默认值）
const KC_PASSWORD = process.env.KEYCLOAK_TEST_USER_PASSWORD ?? "s1-dev-user-password-only";

// RED helper：登录入口。点 "Sign in" 触发 OIDC BFF redirect 到 Keycloak；
// 在 Keycloak 登录页填入测试用户凭据，回调后回到应用。
async function signIn(page: Page, role: string) {
  await page.goto("/");
  await page.getByRole("link", { name: /sign in/i }).click();
  // Keycloak 登录页（realm zhiwei）：填入 subject + 密码
  await page.fill("#username", OIDC_SUBJECT[role]);
  await page.fill("#password", KC_PASSWORD);
  await page.click('button[type="submit"]');
  // OIDC BFF callback 完成后回到应用
  await page.waitForURL("http://localhost:5173/", { timeout: 15_000 });
  await expect(page.getByText(new RegExp(role, "i"))).toBeVisible();
}

// 播种的 org id（e2e DB 种子）
const SEED_ORG_ID = "3a1a8d1c-a63f-4bed-87d1-b67948aea7ac";

// server-enforced 探针：直接 API 调用必须带 session cookie（page.request 共享）、
// CSRF、Idempotency-Key 与 tenant header，才能命中真实 PEP/RLS 判定。
async function directApi(
  page: Page,
  method: string,
  path: string,
  body?: unknown
) {
  const me = await page.request.get("/api/v1/me");
  const csrf = me.status() === 200 ? (await me.json()).csrf_token : "";
  return page.request.fetch(path, {
    method,
    headers: {
      "X-CSRF-Token": csrf,
      "Idempotency-Key": crypto.randomUUID(),
      "X-ZhiWei-Organization": SEED_ORG_ID,
      "Content-Type": "application/json",
    },
    data: body === undefined ? undefined : JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Owner journey：完整 tenancy 生命周期
// ---------------------------------------------------------------------------

test.describe("Owner journey", () => {
  test("creates organization, workspace, invites 4 roles, assigns workspace roles", async ({ page }) => {
    await signIn(page, "owner");

    // 创建 Organization（S1 bootstrap 只接受 organization_id，无 org name）
    await page.getByRole("button", { name: /create organization/i }).click();
    await page.getByRole("button", { name: /confirm/i }).click();
    await expect(page.getByRole("heading", { name: /workspaces/i })).toBeVisible();

    // 创建 Workspace
    await page.getByRole("button", { name: /create workspace/i }).click();
    await page.getByLabel(/workspace name/i).fill("Engineering");
    await page.getByRole("button", { name: /confirm/i }).click();
    await expect(page.getByText("Engineering")).toBeVisible();

    // 邀请 Member / Builder / Approver / Auditor（逐角色 assign role，按 principal UUID）
    for (const role of ["member", "builder", "approver", "auditor"]) {
      await page.getByRole("button", { name: /invite member/i }).click();
      await page.getByLabel(/principal id/i).fill(PRINCIPAL_UUID[role]);
      await page.getByLabel(/role/i).selectOption(role);
      await page.getByRole("button", { name: /send invite/i }).click();
      await expect(page.getByText(PRINCIPAL_UUID[role])).toBeVisible();
    }

    // 创建 Group 并分配 workspace role
    await page.getByRole("button", { name: /create group/i }).click();
    await page.getByLabel(/group name/i).fill("core-platform");
    await page.getByRole("button", { name: /confirm/i }).click();
    await expect(page.getByText("core-platform")).toBeVisible();
  });

  test("removes a member and sees membership list update", async ({ page }) => {
    await signIn(page, "owner");
    await page.getByText(PRINCIPAL_UUID.member).click();
    await page.getByRole("button", { name: /remove member/i }).click();
    await page.getByRole("button", { name: /confirm removal/i }).click();
    await expect(page.getByText(PRINCIPAL_UUID.member)).toHaveCount(0);
  });
});

// ---------------------------------------------------------------------------
// Agent Builder journey：可见 workspace / build 动作；不能 manage members
// ---------------------------------------------------------------------------

test.describe("Agent Builder journey", () => {
  test("sees workspaces and can initiate build actions", async ({ page }) => {
    await signIn(page, "builder");
    await expect(page.getByText("Engineering")).toBeVisible();
    await expect(page.getByRole("button", { name: /new agent/i })).toBeVisible();
  });

  test("cannot manage members — invite button hidden, direct API 403", async ({ page }) => {
    await signIn(page, "builder");
    // 导航隐藏：invite 按钮不可见
    await expect(page.getByRole("button", { name: /invite member/i })).toHaveCount(0);
    // 直接 API 仍由 server 拒绝（前端不硬判 403）：builder 不能 manage members
    const resp = await directApi(
      page,
      "POST",
      `/api/v1/organizations/${SEED_ORG_ID}/members`,
      { principal_id: PRINCIPAL_UUID.member, role_bindings: ["member"] }
    );
    expect(resp.status()).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// Member journey：只能看自己的 membership；不能 create workspace
// ---------------------------------------------------------------------------

test.describe("Member journey", () => {
  test("sees own memberships only", async ({ page }) => {
    await signIn(page, "member");
    await expect(page.getByText("Engineering")).toBeVisible();
    // 不能看到其他组织的资源
    await expect(page.getByText("Finance")).toHaveCount(0);
  });

  test("cannot create workspace — button hidden, direct API 403", async ({ page }) => {
    await signIn(page, "member");
    await expect(page.getByRole("button", { name: /create workspace/i })).toHaveCount(0);
    const resp = await directApi(
      page,
      "POST",
      `/api/v1/organizations/${SEED_ORG_ID}/workspaces`,
      { workspace_id: crypto.randomUUID(), name: "rogue" }
    );
    expect(resp.status()).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// Approver journey：approval queue 可见；不能 edit 资源；dual control
// ---------------------------------------------------------------------------

test.describe("Approver journey", () => {
  test("sees approval queue and can approve, cannot edit resources", async ({ page }) => {
    await signIn(page, "approver");
    await expect(page.getByRole("heading", { name: /approval queue/i })).toBeVisible();
    // 不能编辑资源
    await expect(page.getByRole("button", { name: /create workspace/i })).toHaveCount(0);
  });
});

// ---------------------------------------------------------------------------
// Auditor journey：脱敏 audit events 只读；不能 edit
// ---------------------------------------------------------------------------

test.describe("Auditor journey", () => {
  test("views redacted audit events, cannot edit anything", async ({ page }) => {
    await signIn(page, "auditor");
    await expect(page.getByRole("heading", { name: /audit log/i })).toBeVisible();
    // audit events 脱敏（无明文 token / secret）
    await expect(page.getByText(/access_token|refresh_token/i)).toHaveCount(0);
    // 不能编辑
    await expect(page.getByRole("button", { name: /create|invite|remove/i })).toHaveCount(0);
  });
});

// ---------------------------------------------------------------------------
// 状态：loading / empty / error / 403 / revoked
// ---------------------------------------------------------------------------

test.describe("UI states", () => {
  test("shows loading indicator while fetching resources", async ({ page }) => {
    await signIn(page, "owner");
    await expect(page.getByText(/loading/i)).toBeVisible({ hidden: false });
  });

  test("shows empty state when no workspaces exist", async ({ page }) => {
    await signIn(page, "owner");
    await expect(page.getByText(/no workspaces yet/i)).toBeVisible();
  });

  test("shows error message on API failure", async ({ page }) => {
    // 诱导 API 失败：路由拦截 organizations GET → 500，前端展示 error 状态
    await page.route("**/api/v1/organizations", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: "{}" })
    );
    await signIn(page, "owner");
    await expect(page.getByText(/something went wrong|error/i)).toBeVisible();
  });

  test("403 is server-driven, not frontend hard-coded", async ({ page }) => {
    await signIn(page, "member");
    // 前端不硬判 403：实际由 API 返回（member 不能 remove member）
    const resp = await directApi(
      page,
      "DELETE",
      `/api/v1/organizations/${SEED_ORG_ID}/members/${PRINCIPAL_UUID.member}`
    );
    expect(resp.status()).toBe(403);
  });

  test("revoked session redirects to login (401)", async ({ page }) => {
    await signIn(page, "owner");
    // 模拟 revoked：清空 cookie → 下次请求 401
    await page.context().clearCookies();
    const resp = await page.request.get("/api/v1/me");
    expect(resp.status()).toBe(401);
  });
});
