// S6 ask-evidence e2e（specs/s6-evidence-ask.md §5/§7 → Gate 例外条目
// docs/handoffs/s6-ask-evidence-e2e-exception.md 的解锁 spec）。
//
// 与 spec §5 Workbench 旅程的映射（以真实路由/UI 为准，mock 模式先例
// runtime-approval.spec.ts / architecture.spec.ts）：
//   spec §5 旅程                                | 本 spec 落点（真实 UI）
//   --------------------------------------------+----------------------------------
//   Ask 提问（run 创建）                        | ask-v1 run 经 API/eval 路径创建——
//                                               | UI 无 ask run 创建入口（Workbench
//                                               | 模板集非本任务面）；mock run 列表
//                                               | 供给已完成的 ask-v1 run，从
//                                               | Workbench 打开（apps/web/src/
//                                               | features/workbench → RunDetailView
//                                               | → AppRendererSlot → renderers/ask）
//   Claim/Evidence 渲染（Fact/Quote 与          | renderers/ask/result.tsx 经
//   Inference/Recommendation 的 verified        | GET /api/v1/runs/{id}/evidence
//   标注差异可见）                              | （api/evidence.py）渲染 claims
//   点击 Claim 打开 source locator/canonical    | claim 行内展开（同一 evidence
//   value/verify result                         | 载荷），不发明二次端点
//   创建 Case → Case 导航可见                   | RunDetailView 的 Create case
//                                               | （gated on run 终态）→ routes/
//                                               | sections 的 Cases 分区
//   刷新后从 projection 恢复                    | page.reload() 后 case 详情经
//                                               | GET /api/v1/cases/{id} 重取
//   partial/abstain 渲染                        | abstain run：unknowns 逐字披露、
//                                               | 无 claims（诚实空态）
//   tamper 矩阵 UI 反例                         | evidence 载荷携带 verify 状态
//                                               | 值（verified false）——篡改副本
//                                               | 的 Fact claim 渲染「verification
//                                               | failed」与 verified claim 区分
//
// 后端边界（mock 模式，operator 已授权同 runtime-approval.spec.ts）：响应形状
// 1:1 对齐 src/zhiwei/api/{evidence,cases,runs}.py 的 Pydantic 投影；不发任何
// 真实外部请求；未模拟的 /api 路径一律 500 显式失败（fail loud）。
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

// 完成的 cross-source journey：含 verified Fact/Quote、derived Inference、
// 验证失败的 tampered Fact（tamper 矩阵 UI 反例的 UI 侧断言）
const EVIDENCE_RUN = "b1a2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c50";
// abstain journey：无 claims，unknowns 逐字披露
const ABSTAIN_RUN = "b1a2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c51";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// api/evidence.py RunEvidenceView 的 1:1 mock 形状
interface ClaimMock {
  claim_ref: string;
  claim_type: string | null;
  verified: boolean | null;
  evidence_refs: Record<string, unknown>[];
  canonical_value: Record<string, unknown> | null;
  quote_text: string | null;
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

// api/cases.py CaseView 的 1:1 mock 形状
interface CaseMock {
  id: string;
  run_id: string | null;
  title: string;
  description: string;
  status: string;
  organization_id: string;
  workspace_id: string;
  created_by: string;
  answer_ids: string[];
  evidence_bundle_ids: string[];
  created_at: string;
  updated_at: string;
}

interface MockState {
  cases: CaseMock[];
  caseDetailRequests: number;
  lastCreateCase: { headers: Record<string, string>; body: unknown } | null;
}

const FACT_REF = { ref_type: "QueryReplay", reproducibility_level: "replayable" };

function evidencePayload(runId: string): EvidenceMock {
  if (runId === EVIDENCE_RUN) {
    return {
      run_id: runId,
      run_status: "completed",
      answer_status: "completed",
      answer: { status: "completed", claims: ["fact-1", "quote-1", "inference-1"] },
      claims: [
        {
          claim_ref: "fact-1: 45 好汉",
          claim_type: "Fact",
          verified: true,
          evidence_refs: [
            {
              ...FACT_REF,
              ref_id: "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b",
              source_id: "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d",
              snapshot_digest: "sha256:" + "7a".repeat(32),
              sql: "SELECT name FROM liangshan WHERE rank = ?",
              params: { positional: [7] },
            },
          ],
          canonical_value: { type: "int", value: 45 },
          quote_text: null,
        },
        {
          claim_ref: "quote-1: 逼上梁山",
          claim_type: "Quote",
          verified: true,
          evidence_refs: [
            {
              ref_type: "DocRef",
              reproducibility_level: "copy_frozen",
              document_uri: "docs/zhaoan.md",
              section_path: "chapter-1",
            },
          ],
          canonical_value: { type: "text", value: "逼上梁山" },
          quote_text: "逼上梁山",
        },
        {
          claim_ref: "inference-1: 排名趋势",
          claim_type: "Inference",
          verified: null,
          evidence_refs: [
            { ref_type: "PatternRef", reproducibility_level: "reference_only", pattern_name: "trend" },
          ],
          canonical_value: null,
          quote_text: null,
        },
        {
          claim_ref: "fact-tampered: 副本被篡改",
          claim_type: "Fact",
          verified: false,
          evidence_refs: [
            {
              ref_type: "CellRef",
              reproducibility_level: "copy_frozen",
              table: "findings",
              column: "value",
            },
          ],
          canonical_value: { type: "int", value: 46 },
          quote_text: null,
        },
      ],
      verified_claims: ["fact-1: 45 好汉", "quote-1: 逼上梁山"],
      failed_claims: ["fact-tampered: 副本被篡改"],
      verification: { verification_ok: false, exit_code: 4, check_count: 4 },
      unknowns: [],
      clarification: null,
      findings: [{ source: "documents" }, { source: "db" }],
      conflicts: [],
    };
  }
  return {
    run_id: runId,
    run_status: "completed",
    answer_status: "abstained",
    answer: { status: "abstained", claims: [] },
    claims: [],
    verified_claims: [],
    failed_claims: [],
    verification: { verification_ok: true, exit_code: 0, check_count: 0 },
    unknowns: ["unanswerable-abstain: 数据中不存在该条目"],
    clarification: null,
    findings: [],
    conflicts: [],
  };
}

function runDetail(runId: string) {
  return {
    run_id: runId,
    status: "completed",
    organization_id: ORG_ID,
    tasks: {
      plan: { status: "completed", error: null },
      retrieve: { status: "completed", error: null },
      verify: { status: "completed", error: null },
      synthesize: { status: "completed", error: null },
    },
    // RunDetail（extra=forbid）暂不投影 template；mock 显式供给以证明
    // AppRunBinding 解析路径（architecture.spec.ts 同一先例）
    template: "ask-v1",
  };
}

function newState(): MockState {
  return { cases: [], caseDetailRequests: 0, lastCreateCase: null };
}

function installApiMocks(context: BrowserContext, state: MockState): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

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

    // runtime 面（api/runs.py 契约；template 为前端扩展字段，见 runDetail 注释）
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, [
        { run_id: EVIDENCE_RUN, status: "completed", organization_id: ORG_ID },
        { run_id: ABSTAIN_RUN, status: "completed", organization_id: ORG_ID },
      ]);
    }
    const runMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})$/);
    if (runMatch && method === "GET") {
      if (runMatch[1] !== EVIDENCE_RUN && runMatch[1] !== ABSTAIN_RUN) {
        return fulfill(route, 404, { detail: "run not found" });
      }
      return fulfill(route, 200, runDetail(runMatch[1]));
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
      if (evidenceMatch[1] !== EVIDENCE_RUN && evidenceMatch[1] !== ABSTAIN_RUN) {
        return fulfill(route, 404, { detail: "run not found" });
      }
      return fulfill(route, 200, evidencePayload(evidenceMatch[1]));
    }

    // case surface（api/cases.py 契约）
    if (path === "/api/v1/cases" && method === "GET") {
      return fulfill(route, 200, state.cases);
    }
    const caseMatch = path.match(/^\/api\/v1\/cases\/([0-9a-f-]{36})$/);
    if (caseMatch && method === "GET") {
      state.caseDetailRequests += 1;
      const found = state.cases.find((c) => c.id === caseMatch[1]);
      if (!found) return fulfill(route, 404, { detail: "case not found" });
      return fulfill(route, 200, found);
    }
    const createCaseMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/cases$/);
    if (createCaseMatch && method === "POST") {
      state.lastCreateCase = { headers: req.headers(), body: req.postDataJSON() };
      const now = new Date().toISOString();
      const created: CaseMock = {
        id: crypto.randomUUID(),
        run_id: createCaseMatch[1],
        title: `Case for run ${createCaseMatch[1].slice(0, 8)}`,
        description: "",
        status: "created",
        organization_id: ORG_ID,
        workspace_id: WS_ID,
        created_by: BUILDER_ID,
        answer_ids: [],
        evidence_bundle_ids: [],
        created_at: now,
        updated_at: now,
      };
      state.cases.push(created);
      return fulfill(route, 201, created);
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
// (a) Claim/Evidence 渲染：Fact/Quote（verified-anchored）与
//     Inference（derived）标注差异可见；点击 Claim 展开 source locator/
//     canonical value/verify result；tamper 反例（verified=false）区分呈现
// ---------------------------------------------------------------------------

test.describe("S6 ask evidence — claim annotations", () => {
  test("renders verified-annotation classes, claim expansion and tamper counterexample", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRun(page, EVIDENCE_RUN);
    await expect(page.getByText("Answer status: completed")).toBeVisible();

    // verified 标注差异：Fact/Quote = verified-anchored；Inference = derived
    const factClaim = page.getByText("fact-1: 45 好汉");
    await expect(factClaim).toBeVisible();
    await expect(page.getByText("quote-1: 逼上梁山")).toBeVisible();
    await expect(
      page.getByText("inference-1: 排名趋势").locator("..").getByText("derived")
    ).toBeVisible();

    // 点击 Fact claim：source locator（SQL 重放定位）+ canonical value + verify result
    await factClaim.click();
    await expect(
      page.getByText("SELECT name FROM liangshan WHERE rank = ?")
    ).toBeVisible();
    await expect(page.getByText(/canonical value: int = 45/)).toBeVisible();

    // tamper 矩阵 UI 反例：副本被篡改的 Fact claim → verification failed
    const tampered = page.getByText("fact-tampered: 副本被篡改");
    await expect(
      tampered.locator("..").getByText("verification failed")
    ).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (b) 创建 Case → Cases 分区可见 → 详情（linked run/status verbatim）→
//     刷新后从 projection 恢复（详情重取）
// ---------------------------------------------------------------------------

test.describe("S6 ask evidence — case journeys", () => {
  test("creates a case from a completed run, lists it, and recovers detail after reload", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRun(page, EVIDENCE_RUN);
    // run 终态才出现创建入口（非终态 run 无此按钮）
    await page.getByRole("button", { name: "Create case" }).click();
    await expect(page.getByText(/Case created/)).toBeVisible();

    // mutation PEP 契约：CSRF + Idempotency-Key（api client 统一注入）
    expect(state.lastCreateCase).not.toBeNull();
    expect(state.lastCreateCase!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastCreateCase!.headers["idempotency-key"])).toBe(true);

    // Cases 分区：列表出现创建的 case
    await page.getByRole("button", { name: "Cases", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Cases" })).toBeVisible();
    await page.getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Case", exact: true })).toBeVisible();
    await expect(page.getByText("Status: created")).toBeVisible();
    await expect(page.getByText(new RegExp(EVIDENCE_RUN))).toBeVisible();

    // 刷新恢复：shell 无 URL 路由（分区状态不跨 reload 保留），刷新后经分区
    // 导航重新打开 case，详情从 server projection 重取（请求计数自证恢复）
    await page.reload();
    await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
    await page.getByRole("button", { name: "Cases", exact: true }).click();
    await page.getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Case", exact: true })).toBeVisible();
    await expect(page.getByText("Status: created")).toBeVisible();
    expect(state.caseDetailRequests).toBeGreaterThanOrEqual(2);

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (c) abstain 诚实渲染：无 claims、unknowns 逐字披露
// ---------------------------------------------------------------------------

test.describe("S6 ask evidence — abstain", () => {
  test("renders abstained answer with verbatim unknowns and no claims", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRun(page, ABSTAIN_RUN);
    await expect(page.getByText("Answer status: abstained")).toBeVisible();
    await expect(page.getByText("unanswerable-abstain: 数据中不存在该条目")).toBeVisible();
    await expect(page.getByText("No claims")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (d) run input（question）的诚实缺席：无 re-ask API → 无 re-ask 控件
// ---------------------------------------------------------------------------

test.describe("S6 ask evidence — run input honesty", () => {
  test("input view states the missing question projection and offers no re-ask control", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state);
    const page = await context.newPage();

    await openRun(page, EVIDENCE_RUN);
    const inputView = page.getByRole("region", { name: "App input view" });
    await expect(
      inputView.getByText(/not projected by the runtime REST contract/)
    ).toBeVisible();
    // 无 re-ask affordance（不存在 re-ask API，不发明按钮）
    await expect(inputView.getByRole("button")).toHaveCount(0);

    await context.close();
  });
});
