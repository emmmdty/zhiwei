// 认证态截图（产品化窗口 Phase 2b）：真实 local-product 栈 + 真实 Keycloak OIDC 登录。
//
// 前提（见 docs/operations/install.md §4 + deploy/seed_identity_e2e.py）：
//   1. compose 全栈 healthy（uv run zhiwei dev up）
//   2. journey principal 已种子到产品库：
//      ZHIWEI_E2E_ADMIN_DSN=postgresql://zhiwei_migrator:<pw>@127.0.0.1:55433/zhiwei \
//        ZHIWEI_OIDC_ISSUER=http://keycloak.local:8081/realms/zhiwei \
//        uv run python deploy/seed_identity_e2e.py
//   3. hosts 条目由 --host-resolver-rules 注入（无需 sudo 改 /etc/hosts）
//
// 运行（不进 playwright test runner——避免走 webServer/mock 面板）：
//   cd apps/web && npx tsx e2e/capture-screens.ts
// 产物：assets/screens/*.png（README 嵌入 + vision 视觉验收输入）

import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// 会话 cookie 是 __Host- 前缀 + Secure（auth.py 冻结契约）——浏览器旅程必须走
// proxy 的 TLS 监听（compose 8443，dev 自签名证书 → ignoreHTTPSErrors）。
// proxy 对 /（web 静态）、/api/、/realms/、/auth/ 同站代理，全程单一 origin。
const BASE = "https://keycloak.local:8443";
const KEYCLOAK_USER = process.env.ZHIWEI_LOCAL_USER ?? "owner-oidc";
const KEYCLOAK_PASSWORD = process.env.ZHIWEI_LOCAL_PASSWORD ?? "zhiwei-local-user-only";
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "assets", "screens");

const PAGES: Array<[string, string]> = [
  ["home", "Workbench"],
  ["evals", "Evals"],
  ["releases", "Releases"],
  ["observability", "Observability"],
  ["costs", "Costs"],
  ["studio", "Agent Studio"],
  ["knowledge", "Knowledge"],
  ["capabilities", "Capabilities"],
  ["memory", "Memory"],
  ["admin", "Admin"],
  ["cases", "Cases"],
];

async function main() {
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({
    // host-resolver-rules：hosts 条目注入（无需 sudo）。
    // proxy：operator 终端带 clash 类代理（127.0.0.1:7890），Playwright 会把它传给
    // Chromium——代理无法解析 keycloak.local，OIDC 旅程会变成空体 502，因此对
    // keycloak.local 显式 bypass（配合 host-resolver-rules 直连 127.0.0.1:8081）；
    // 其余流量仍经代理（WSL 环境下 direct:// 对 127.0.0.1 发布端口反而超时）。
    args: ["--host-resolver-rules=MAP keycloak.local 127.0.0.1"],
    proxy: {
      server: process.env.HTTPS_PROXY ?? process.env.HTTP_PROXY ?? "http://127.0.0.1:7890",
      bypass: "keycloak.local",
    },
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    ignoreHTTPSErrors: true, // dev 自签名证书；真实部署由 operator 换发正式证书
  });
  const page = await context.newPage();

  // 登录：SPA 登出态仅渲染 /auth/login 入口 → 服务端 302 到 Keycloak → 提交 → 回调
  await page.goto(BASE + "/auth/login", { waitUntil: "domcontentloaded" });
  await page.waitForURL(/keycloak\.local/, { timeout: 20000 });
  console.log("[capture] keycloak →", page.url());
  await page.waitForSelector("#username", { timeout: 15000 });
  await page.fill("#username", KEYCLOAK_USER);
  await page.fill("#password", KEYCLOAK_PASSWORD);
  await page.click("#kc-login");
  await page.waitForLoadState("domcontentloaded");
  await page.waitForTimeout(2000);
  console.log("[capture] after login →", page.url());
  // 回调后停在产品内；home 截图取默认分区（Workbench）
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded", timeout: 20000 });
  await page.waitForTimeout(1500);

  // 选中 workspace（S11 followup-2 任务二修复的实测路径）：/me 无头回显 ws null
  // → 点击 demo → 写本地选择 → refresh() 由 /me 带头回显确认（aria-pressed=true）。
  // 不选中则全部分区被 workspace_id 门控（死环未修时的 403 面即此病）。
  await page.getByRole("heading", { name: "Workspaces" }).waitFor({ timeout: 15000 });
  await page.getByRole("button", { name: "knowledge-ops", exact: true }).click();
  await expectVisiblePressed(page);
  console.log("[capture] workspace knowledge-ops selected");

  // 播种真实数据面（全部走产品路径，fixture only——零 live 模型请求）：
  // Workbench 模板发起 run → 终态后 Create case → Studio 创建 draft。
  // README 截图须呈现真实数据面而非空态（任务三验收口径）。
  const runRespPromise = page.waitForResponse(
    (r) => r.url().includes("/api/v1/runs") && r.request().method() === "POST",
  );
  await page.getByLabel("Template").selectOption("single-fixture");
  await page.getByRole("button", { name: "New run" }).click();
  // capture 记下本次创建的 run_id：列表定位用 hasText(run_id) 精确开行——
  // 固定开第一行会让多轮截图对同一个最老 run 反复建 case（f716465c 双 case 根因）
  const createdRun = (await (await runRespPromise).json()) as { run_id: string };
  console.log("[capture] run created, waiting terminal…", createdRun.run_id);
  // 创建 → 详情存在两个 eventual-consistency 窗口：①run 刚创建时 canonical
  // events 未落，详情走防枚举 404 分支（UI 渲染错误横幅、无 Back——经导航
  // 回列表重开）；②projection 投影不自动刷新，终态需重开拉取（runtime-
  // approval journey 的 Back/Open 同款）。Create case 入口 gated on 终态。
  let caseCreated = false;
  for (let attempt = 0; attempt < 20 && !caseCreated; attempt++) {
    try {
      await page.getByRole("button", { name: "Workbench", exact: true }).click();
      const openButton = page
        .locator("table tbody tr")
        .filter({ hasText: createdRun.run_id })
        .getByRole("button", { name: "Open" });
      await openButton.waitFor({ timeout: 5000 });
      await openButton.click();
      await page.getByRole("heading", { name: "Run", exact: true }).waitFor({ timeout: 5000 });
      await page.getByRole("button", { name: "Create case" }).click({ timeout: 5000 });
      // 点击成功 ≠ 创建成功：POST 结果异步上屏（403/409 都是错误横幅），
      // 以「Case created」正文为成功判据
      await page.getByText(/Case created/).waitFor({ timeout: 5000 });
      caseCreated = true;
    } catch {
      // 跨分区往返强制重挂载 workbench：run detail 404 竞态（创建后 canonical
      // events 未落，防枚举 404）会让区内 selected 卡在错误横幅（无 Back、
      // 同区 nav 点击不重挂载）；round-trip 后列表可见（list 不过滤终态），
      // 重开即拿到已落账投影（runtime-approval journey 的 Back/Open 同款）
      await page.getByRole("button", { name: "Evals", exact: true }).click();
      await page.getByRole("button", { name: "Workbench", exact: true }).click();
      await page.waitForTimeout(2000);
    }
  }
  if (!caseCreated) throw new Error("run 未在重试窗口内到达终态（Create case 未出现）");
  console.log("[capture] case created from run");

  await page.getByRole("button", { name: "Agent Studio", exact: true }).click();
  // draft 名带会话时间戳：每轮 capture 建「一个」新 agent——固定同名会让列表
  // 变成 N 行重名（f716465c 双 case 同根因）；存量重复由发布流程清库
  const session = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 12);
  const draftName = `demo-knowledge-agent-${session}`;
  await page.getByLabel("Name").fill(draftName);
  await page.getByLabel("Description").fill("Knowledge retrieval agent (demo)");
  await page.getByLabel("Declared capabilities").fill("knowledge.retrieve@1");
  await page.getByRole("button", { name: "Create draft" }).click();
  // draft 编辑器出现 = 创建成功（studio-draft journey 的既有契约锚点）
  await page.getByRole("heading", { name: "Instructions" }).waitFor({ timeout: 15000 });
  await page.waitForLoadState("networkidle");
  console.log("[capture] studio draft created");

  await page.getByRole("button", { name: "Workbench", exact: true }).click();
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(600);
  await page.screenshot({ path: resolve(OUT, "home.png"), fullPage: false });
  console.log("[capture] home.png ←", page.url());

  await page.waitForTimeout(1000);
  for (const [name, label] of PAGES.slice(1)) {
    try {
      // 导航是按钮驱动（useState section，F-R7-01 前的既有形态）——goto 不会切换
      await page.getByRole("button", { name: label, exact: true }).click();
      // 分区数据面就绪再截：networkidle + 固定缓冲（studio.png 上一轮
      // 「Loading agent drafts…」即截图时机过早——等待网络空闲而非盲等）
      await page.waitForLoadState("networkidle", { timeout: 15000 });
      await page.waitForTimeout(600);
      await page.screenshot({ path: resolve(OUT, `${name}.png`), fullPage: false });
      console.log(`[capture] ${name}.png`);
    } catch (e) {
      console.warn(`[capture] ${name} 失败（继续）:`, (e as Error).message.split("\n")[0]);
    }
  }

  await browser.close();
}

async function expectVisiblePressed(page: import("@playwright/test").Page): Promise<void> {
  // aria-pressed 只跟随 /me 回显（server 校验后的 user.workspace_id），回显
  // 成功即修复路径全链路（写选择 → 带头 /me → session meta 注入）被实测。
  await page
    .getByRole("button", { name: "knowledge-ops", exact: true })
    .and(page.locator('[aria-pressed="true"]'))
    .waitFor({ timeout: 15000 });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
