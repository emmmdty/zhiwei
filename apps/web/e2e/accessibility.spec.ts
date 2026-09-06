// S10 fix-B D7（specs/s10 §6）：responsive/accessibility smoke——诚实冒烟，
// 不引入 axe 等新依赖。覆盖三块：
//  1. landmark/heading 检查（3 个代表视图：Workbench / Costs / Run 详情）——
//     main landmark 唯一、Primary nav 可达、heading 不跳级；
//  2. 键盘导航冒烟——Tab 从页首到达 nav 与主区 primary action，键盘焦点
//     :focus-visible 且 computed outline 可见（styles 基线的 CSS 断言）；
//  3. 视口冒烟——1280/768/390 宽度下 nav + 真实视图渲染，主壳无横向溢出
//     （documentElement.scrollWidth ≤ innerWidth；表格在容器内滚动）。
//
// 纪律与 architecture.spec.ts 同：网络层 mock，形状逐字段对齐真实契约；
// 未模拟 /api 路径一律 500 显式失败（fail loud）。

import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";

const ORG_ID = "3a1a8d1c-a63f-4bed-87d1-b67948aea7ac";
const WS_ID = "6f1c2a34-9b7e-4d0a-8f61-0c5b2d7e9a11";
const BUILDER_ID = "3383f6a7-d17b-44c2-802c-d67c3974e13a";
const CSRF = "e2e-csrf-token";
const RUN_ID = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e30";

function installApiMocks(context: BrowserContext): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  context.route("/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();

    // 会话引导（session.tsx 消费的真实契约，与 architecture.spec.ts 同款）
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
      return fulfill(route, 200, [{ run_id: RUN_ID, status: "completed", organization_id: ORG_ID }]);
    }
    if (path === `/api/v1/runs/${RUN_ID}` && method === "GET") {
      return fulfill(route, 200, {
        run_id: RUN_ID,
        status: "completed",
        organization_id: ORG_ID,
        tasks: {
          plan: { status: "completed", error: null },
          execute: { status: "completed", error: null },
        },
        template: null,
        execution_mode: "fixture",
      });
    }
    if (path === `/api/v1/runs/${RUN_ID}/evidence` && method === "GET") {
      // api/evidence.py RunEvidenceView 的 1:1 形状（最小真实载荷）
      return fulfill(route, 200, {
        run_id: RUN_ID,
        run_status: "completed",
        answer_status: null,
        answer: {},
        claims: [
          {
            claim_ref: "claim:verified-fact",
            claim_type: "Fact",
            verified: true,
            quote_text: null,
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
        ],
        verified_claims: ["claim:verified-fact"],
        failed_claims: [],
        verification: null,
        unknowns: [],
        clarification: null,
        findings: [],
        conflicts: [],
      });
    }
    if (path === `/api/v1/runs/${RUN_ID}/approvals` && method === "GET") {
      return fulfill(route, 200, []);
    }
    if (path === `/api/v1/runs/${RUN_ID}/events` && method === "GET") {
      return fulfill(route, 200, []);
    }
    if (path === "/api/v1/observability/costs" && method === "GET") {
      return fulfill(route, 200, {
        reservations: [
          {
            reservation_id: "res-a11y-run",
            run_id: RUN_ID,
            amount_usd: "0.0000042",
            price_source: "fixture",
            price_confidence: "exact",
            created_at: "2026-09-06T00:00:00+00:00",
          },
        ],
        reconciliations: [],
      });
    }
    if (path === "/api/v1/evals" && method === "GET") return fulfill(route, 200, []);
    if (path === "/api/v1/releases" && method === "GET") return fulfill(route, 200, []);
    if (path === "/api/v1/claims" && method === "GET") return fulfill(route, 200, []);

    return fulfill(route, 500, { detail: `unmocked: ${method} ${path}` });
  });
}

async function newContextWithMocks(browser: Browser): Promise<BrowserContext> {
  const context = await browser.newContext();
  installApiMocks(context);
  return context;
}

// heading 层级健全性：main 内首个 heading ≤ h2（视图以 h2 为顶），其后不跳级。
async function expectSaneHeadingLevels(page: Page): Promise<void> {
  const levels = await page.locator("main").evaluate((main) =>
    Array.from(main.querySelectorAll("h1,h2,h3,h4,h5,h6")).map((h) => Number(h.tagName[1]))
  );
  expect(levels.length).toBeGreaterThan(0);
  let previous = 1;
  for (const level of levels) {
    expect(level).toBeLessThanOrEqual(Math.max(previous + 1, 2));
    previous = level;
  }
}

async function expectShellLandmarks(page: Page): Promise<void> {
  await expect(page.locator("main")).toHaveCount(1);
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth
  );
  expect(overflow).toBeLessThanOrEqual(1);
}

test.describe("S10 fix-B — accessibility smoke", () => {
  test("landmarks and heading levels stay sane across three representative views", async ({ browser }) => {
    const context = await newContextWithMocks(browser);
    const page = await context.newPage();

    // 视图 1：Workbench（默认分区）
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
    await expectShellLandmarks(page);
    await expectSaneHeadingLevels(page);

    // 视图 2：Costs（表格密集视图）
    await page.getByRole("button", { name: "Costs" }).click();
    await expect(page.getByRole("heading", { name: "Costs" })).toBeVisible();
    await expectShellLandmarks(page);
    await expectSaneHeadingLevels(page);

    // 视图 3：Run 详情（面板结构视图）
    await page.getByRole("button", { name: "Workbench" }).click();
    await page.getByRole("row", { name: new RegExp(RUN_ID) }).getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Run", exact: true })).toBeVisible();
    await expectShellLandmarks(page);
    await expectSaneHeadingLevels(page);

    await context.close();
  });

  test("keyboard reaches the primary nav and a primary action with visible focus", async ({ browser }) => {
    const context = await newContextWithMocks(browser);
    const page = await context.newPage();
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();

    // header 的 Sign out 在 DOM 前部——tab 顺序如实经过它后进入 nav
    let focusInNav = false;
    for (let i = 0; i < 8 && !focusInNav; i++) {
      await page.keyboard.press("Tab");
      focusInNav = await page.evaluate(() => document.activeElement?.closest("nav") != null);
    }
    expect(focusInNav).toBe(true);

    let focusOnMainButton = false;
    for (let i = 0; i < 16 && !focusOnMainButton; i++) {
      await page.keyboard.press("Tab");
      focusOnMainButton = await page.evaluate(() => {
        const el = document.activeElement;
        return el?.tagName === "BUTTON" && el.closest("main") != null;
      });
    }
    expect(focusOnMainButton).toBe(true);

    // 键盘焦点可见：:focus-visible 命中 + computed outline 非默认（CSS 基线断言）
    const focus = await page.evaluate(() => {
      const el = document.activeElement;
      if (!(el instanceof Element)) return { visible: false, outline: "none" };
      return {
        visible: el.matches(":focus-visible"),
        outline: getComputedStyle(el).outlineStyle,
      };
    });
    expect(focus.visible).toBe(true);
    expect(focus.outline).toBe("solid");

    await context.close();
  });

  test("shell has no horizontal overflow at 1280/768/390 widths", async ({ browser }) => {
    const context = await newContextWithMocks(browser);
    const page = await context.newPage();

    for (const width of [1280, 768, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
      // 等表格内容渲染后再量溢出（否则会在 loading 态量到假阴性）
      await expect(page.getByRole("row", { name: new RegExp(RUN_ID) })).toBeVisible();
      await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
      await expectNoHorizontalOverflow(page);

      // 表格密集的真实视图同查（Costs）
      await page.getByRole("button", { name: "Costs" }).click();
      await expect(page.getByRole("heading", { name: "Costs" })).toBeVisible();
      await expect(page.getByText("res-a11y-run")).toBeVisible();
      await expectNoHorizontalOverflow(page);
    }

    await context.close();
  });
});

