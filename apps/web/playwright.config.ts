import { defineConfig } from "@playwright/test";

// S1-T6 role-aware Web shell e2e：Playwright journey 即视觉契约（operator 授权
// 第二种情形，specs/s1-tenancy-policy.md §4 Web journey + plan Task 6 步骤为唯一
// 视觉契约来源，无独立视觉稿）。
//
// webServer 启动 Vite dev server（5173）；GREEN 阶段后端 create_app 由 Gate 环境
// 另起（uvicorn 或 compose identity profile）。RED 阶段无视图，所有 journey
// 在首次交互元素查找处超时失败——反例到真实前端行为。
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:5173",
    trace: "on-first-retry",
    actionTimeout: 5_000,
    launchOptions: { executablePath: process.env.CI ? undefined : "/usr/bin/google-chrome" },
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
