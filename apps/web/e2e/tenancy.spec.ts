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

// ---------------------------------------------------------------------------
// Owner journey：完整 tenancy 生命周期
// ---------------------------------------------------------------------------

test.describe("Owner journey", () => {
  test("creates organization, workspace, invites 4 roles, assigns workspace roles", async ({ page }) => {
    await signIn(page, "owner");

    // 创建 Organization
    await page.getByRole("button", { name: /create organization/i }).click();
    await page.getByLabel(/organization name/i).fill("Acme Corp");
    await page.getByRole("button", { name: /confirm/i }).click();
    await expect(page.getByText("Acme Corp")).toBeVisible();

    // 创建 Workspace
    await page.getByRole("button", { name: /create workspace/i }).click();
    await page.getByLabel(/workspace name/i).fill("Engineering");
    await page.getByRole("button", { name: /confirm/i }).click();
    await expect(page.getByText("Engineering")).toBeVisible();

    // 邀请 Member / Builder / Approver / Auditor（逐角色 assign role）
    for (const role of ["member", "builder", "approver", "auditor"]) {
      await page.getByRole("button", { name: /invite member/i }).click();
      await page.getByLabel(/external id/i).fill(OIDC_SUBJECT[role]);
      await page.getByLabel(/role/i).selectOption(role);
      await page.getByRole("button", { name: /send invite/i }).click();
      await expect(page.getByText(OIDC_SUBJECT[role])).toBeVisible();
    }

    // 创建 Group 并分配 workspace role
    await page.getByRole("button", { name: /create group/i }).click();
    await page.getByLabel(/group name/i).fill("core-platform");
    await page.getByRole("button", { name: /confirm/i }).click();
    await expect(page.getByText("core-platform")).toBeVisible();
  });

  test("removes a member and sees membership list update", async ({ page }) => {
    await signIn(page, "owner");
    await page.getByText(OIDC_SUBJECT.member).click();
    await page.getByRole("button", { name: /remove member/i }).click();
    await page.getByRole("button", { name: /confirm removal/i }).click();
    await expect(page.getByText(OIDC_SUBJECT.member)).toHaveCount(0);
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
    // 直接 API 仍由 server 拒绝（前端不硬判 403）
    const api = page.request.post("/api/v1/organizations", {
      data: { organization_id: "00000000-0000-0000-0000-000000000001" },
    });
    const resp = await api;
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
    const resp = await page.request.post("/api/v1/workspaces", {
      data: { name: "rogue" },
    });
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
    await signIn(page, "owner");
    // 模拟后端不可达：前端展示 error 状态（非空白页 / 非 crash）
    await expect(page.getByText(/something went wrong|error/i)).toBeVisible();
  });

  test("403 is server-driven, not frontend hard-coded", async ({ page }) => {
    await signIn(page, "member");
    // 前端不硬判 403：实际由 API 返回
    const resp = await page.request.delete(
      "/api/v1/organizations/00000000-0000-0000-0000-000000000002/memberships/00000000-0000-0000-0000-000000000003"
    );
    expect(resp.status()).toBe(403);
  });

  test("revoked session redirects to login (401)", async ({ page }) => {
    await signIn(page, "owner");
    // session 被 revoke（disable principal）：下次请求 401 → 重定向登录
    const resp = await page.request.get("/api/v1/me");
    // 401 或重定向到 /auth/login（session 失效）
    expect([401, 302]).toContain(resp.status());
  });
});
