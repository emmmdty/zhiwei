// S9-T7 eval/release/observability/costs e2e（specs/s9 §7 Gate → plan Task 7）。
//
// 与后端契约的映射（网络层 mock，响应形状逐字段对齐真实投影）：
//   GET  /api/v1/releases            → api/releases.py ReleaseView
//   POST  /releases/{id}/advance     → ReleaseState 状态机（agents/release.py 迁移矩阵）
//   POST  /releases/{id}/route       → agents/rollout.py route_version 语义
//                                       （suspended 一律 409 rollout_not_configured）
//   POST  /releases/{id}/rollback    → apply_rollback 结果（applies_to=new_runs_only、
//                                       executed=false、in_flight_disposition）
//   GET  /api/v1/claims              → api/claims.py ClaimView（status/evidence 元数据）
//   GET  /api/v1/observability/failures → api/observability.py FailureTaxonomy
//   GET  /api/v1/observability/costs   → api/observability.py CostSummary
//   GET/POST /api/v1/evals…          → 【后端缺口】src/zhiwei/api/evals.py 不存在；
//                                       字段名逐一对齐 evals/runs.py（RunPhase/
//                                       SampleStatus/EvalMode）+ evals/reports.py
//                                       EvalReportArtifact.canonical_mapping +
//                                       persistence/models.py EvalRunRow。路径按
//                                       specs/s9 §2 的 api/evals.py 资源名推导，
//                                       后端 router 补齐前 UI 对 404 fail loud。
//   trace/span 视图：无任何 traces 端点 → 按任务纪律跳过，不发明端点。
//
// 纪律：
// - metadata only：mock 在 outcome.result 里植入 canary 正文（prompt/result），
//   UI 只允许渲染 status/result_digest——断言 canary 全程不出现在 DOM。
// - incomplete/unknown 原样展示：未密封 run 的 sealed_at、无 report 的 scope/
//   quality、reconciliation 的 unknown 分量——不造 placeholder 0 / 假成功值。
// - 角色显隐只管 UI 入口，权限由 server PEP 强制（§4 最后一段）；mock 的
//   advance/rollback 角色拒绝语义对齐 api/releases.py 的 409 拒绝面。
// - 不发任何真实外部请求；未模拟的 /api 路径一律 500 显式失败（fail loud）。

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
const ADMIN_ID = "2b6c4d8e-91f0-4a57-b3c2-5d8e7f9a0b01";
const AUDITOR_ID = "63d7ef96-75e0-4c47-8edb-10dd834c9f64";
const CSRF = "e2e-csrf-token";

const EVAL_SEALED = "7e6a5b4c-3d2e-4f10-9a8b-1c2d3e4f5a01";
const EVAL_PARTIAL = "7e6a5b4c-3d2e-4f10-9a8b-1c2d3e4f5a02";
const RELEASE_ID = "5d4c3b2a-1f0e-4d9c-8a7b-6f5e4d3c2b01";
const RUN_A = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e01";
const MANIFEST_ID = "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c02";

const CANARY_PROMPT = "CANARY-PROMPT-7f3d9a2c";
const CANARY_RESULT = "CANARY-RESULT-2b8e41d9";

function digest(seed: string): string {
  // 固定 mock digest：sha256 前缀 + 确定性 hex（不参与真实校验）
  let hex = "";
  for (let i = 0; i < 64; i++) hex += ((seed.charCodeAt(i % seed.length) + i) % 16).toString(16);
  return `sha256:${hex}`;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// ---------------------------------------------------------------------------
// mock 域状态（字段名 = 后端投影字段名）
// ---------------------------------------------------------------------------

interface EvalOutcome {
  unit: { sample_id: string; unit_id: string };
  status: "completed" | "failed" | "refused" | "error";
  result_digest: string;
  // 真实载荷里 result 可能含 prompt/result 正文——UI 必须只渲染元数据
  result: Record<string, string>;
}

// EvalReportArtifact.canonical_mapping（evals/reports.py）的 JSON 形状
interface EvalReport {
  schema_id: string;
  schema_version: number;
  generated_from: Record<string, string>;
  scope: {
    mode: string;
    model: string;
    version: string;
    date: string;
    corpus: string;
    environment: string;
  };
  quality: {
    label: string;
    n: number;
    successes: number;
    estimate: number;
    ci_low: number;
    ci_high: number;
    denominator: {
      n_total: number;
      n_completed: number;
      n_failed: number;
      n_refused: number;
      n_error: number;
    };
  }[];
  paired_comparison: null;
}

interface EvalRun {
  eval_run_id: string;
  run_id: string | null;
  mode: string;
  status: "running" | "partial" | "sealed";
  sealed_at: string | null;
  registered_units: { sample_id: string; unit_id: string }[];
  outcomes: EvalOutcome[];
  report: EvalReport | null;
}

interface ReleaseRecord {
  release_id: string;
  agent_id: string;
  agent_version: number;
  state: string;
  manifest_digest: string;
  default_version: number | null;
}

interface MockState {
  evals: EvalRun[];
  releases: ReleaseRecord[];
  lastSeal: { headers: Record<string, string>; body: unknown } | null;
  lastAdvance: { headers: Record<string, string>; body: unknown } | null;
  lastRoute: { headers: Record<string, string>; body: unknown } | null;
  lastRollback: { headers: Record<string, string>; body: unknown } | null;
}

type Actor = "builder" | "admin" | "auditor";

// 平台角色绑定（session.tsx 经 members 列表解析；release 角色映射对齐
// api/releases.py _RELEASE_ROLE_PLATFORM_ROLES）
const ACTORS: Record<Actor, { principal: string; role_bindings: string[] }> = {
  builder: { principal: BUILDER_ID, role_bindings: ["builder"] },
  admin: { principal: ADMIN_ID, role_bindings: ["workspace_admin"] },
  auditor: { principal: AUDITOR_ID, role_bindings: ["auditor"] },
};

// release 角色 → 平台角色（api/releases.py 冻结映射的 mock 侧镜像）
function releaseRoles(actor: Actor): string[] {
  const names = ACTORS[actor].role_bindings;
  const roles: string[] = [];
  if (names.includes("agent_builder") || names.includes("builder")) roles.push("builder");
  if (names.includes("workspace_admin")) roles.push("reviewer", "approver", "release_manager");
  if (names.includes("approver")) roles.push("approver");
  return roles;
}

// agents/release.py ALLOWED_RELEASE_TRANSITIONS（状态机侧 mock 镜像）
const TRANSITION_ROLES: Record<string, string> = {
  "draft>sandbox": "builder",
  "sandbox>evaluated": "builder",
  "evaluated>review": "reviewer",
  "review>staged": "approver",
  "staged>published": "release_manager",
  "published>deprecated": "release_manager",
  "deprecated>retired": "release_manager",
};

function unit(sample: string): { sample_id: string; unit_id: string } {
  return { sample_id: sample, unit_id: "u1" };
}

function outcome(
  sample: string,
  status: EvalOutcome["status"],
  seed: string
): EvalOutcome {
  return {
    unit: unit(sample),
    status,
    result_digest: digest(seed),
    result: { prompt: CANARY_PROMPT, completion: CANARY_RESULT },
  };
}

// 密封 run：offline 模式 + 完整报告（scope 标签 + Wilson CI + 含 refused/error
// 的完整分母）。outcomes 与 report.denominator 一致（completed 2 / failed 1 /
// refused 1 / error 1）。
function sealedEvalRun(): EvalRun {
  return {
    eval_run_id: EVAL_SEALED,
    run_id: RUN_A,
    mode: "offline",
    status: "sealed",
    sealed_at: "2026-09-01T12:00:00+00:00",
    registered_units: [
      unit("s9-core-001"),
      unit("s9-core-002"),
      unit("s9-core-003"),
      unit("s9-core-004"),
      unit("s9-core-005"),
    ],
    outcomes: [
      outcome("s9-core-001", "completed", "d1"),
      outcome("s9-core-002", "completed", "d2"),
      outcome("s9-core-003", "failed", "d3"),
      outcome("s9-core-004", "refused", "d4"),
      outcome("s9-core-005", "error", "d5"),
    ],
    report: {
      schema_id: "eval.report",
      schema_version: 1,
      generated_from: {
        run_id: RUN_A,
        eval_run_id: EVAL_SEALED,
        seal_digest: digest("seal"),
        mode: "offline",
        migration_revision: "0009",
        code_digest: digest("code"),
        config_digest: digest("config"),
        schema_digest: digest("schema"),
        dataset_digest: digest("dataset"),
        dataset_manifest_id: MANIFEST_ID,
        test_report_digest: digest("test-report"),
        test_report_manifest_id: MANIFEST_ID,
      },
      scope: {
        mode: "offline",
        model: "qwen3-internal",
        version: "v3",
        date: "2026-09-01",
        corpus: "frozen-s9",
        environment: "offline-sandbox",
      },
      quality: [
        {
          label: "success_rate",
          n: 5,
          successes: 2,
          estimate: 0.4,
          ci_low: 0.1,
          ci_high: 0.74,
          denominator: {
            n_total: 5,
            n_completed: 2,
            n_failed: 1,
            n_refused: 1,
            n_error: 1,
          },
        },
      ],
      paired_comparison: null,
    },
  };
}

// 部分 run：未密封（sealed_at null）、无 report——incomplete 值必须原样展示
function partialEvalRun(): EvalRun {
  return {
    eval_run_id: EVAL_PARTIAL,
    run_id: null,
    mode: "fixture",
    status: "partial",
    sealed_at: null,
    registered_units: [unit("s9-edge-001"), unit("s9-edge-002"), unit("s9-edge-003")],
    outcomes: [
      outcome("s9-edge-001", "completed", "p1"),
      outcome("s9-edge-002", "refused", "p2"),
    ],
    report: null,
  };
}

function stagedRelease(): ReleaseRecord {
  return {
    release_id: RELEASE_ID,
    agent_id: "6c5d4e3f-2a1b-4c0d-9e8f-7a6b5c4d3e02",
    agent_version: 3,
    state: "staged",
    manifest_digest: digest("manifest"),
    default_version: 2,
  };
}

function newState(): MockState {
  return {
    evals: [sealedEvalRun(), partialEvalRun()],
    releases: [stagedRelease()],
    lastSeal: null,
    lastAdvance: null,
    lastRoute: null,
    lastRollback: null,
  };
}

function releaseView(release: ReleaseRecord) {
  return {
    release_id: release.release_id,
    agent_id: release.agent_id,
    agent_version: release.agent_version,
    state: release.state,
    manifest_digest: release.manifest_digest,
    default_version: release.default_version,
  };
}

function evalDetail(run: EvalRun) {
  return {
    eval_run_id: run.eval_run_id,
    run_id: run.run_id,
    mode: run.mode,
    status: run.status,
    sealed_at: run.sealed_at,
    registered_units: run.registered_units,
    outcomes: run.outcomes,
    report: run.report,
  };
}

// ---------------------------------------------------------------------------
// 网络层 mock：单一 catch-all 路由内部分发；未模拟路径 500 fail loud
// ---------------------------------------------------------------------------

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
        principal: { id: ACTORS[actor].principal },
        organizations: [{ id: ORG_ID, status: "active" }],
        context: { organization_id: ORG_ID, workspace_id: WS_ID },
        csrf_token: CSRF,
      });
    }
    if (path === "/api/v1/organizations" && method === "GET") {
      return fulfill(route, 200, [{ id: ORG_ID, status: "active" }]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/members` && method === "GET") {
      return fulfill(route, 200, (Object.values(ACTORS) as { principal: string; role_bindings: string[] }[]).map(
        (a) => ({
          principal_id: a.principal,
          organization_id: ORG_ID,
          role_bindings: a.role_bindings,
        })
      ));
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    if (path === `/api/v1/workspaces/${WS_ID}/groups` && method === "GET") {
      return fulfill(route, 200, []);
    }
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, []);
    }

    // ------------------------------------------------------------------
    // Eval runs（后端 router 缺口，形状对齐 evals 域层）
    // ------------------------------------------------------------------
    if (path === "/api/v1/evals" && method === "GET") {
      return fulfill(
        route,
        200,
        state.evals.map((run) => ({
          eval_run_id: run.eval_run_id,
          run_id: run.run_id,
          mode: run.mode,
          status: run.status,
          sealed_at: run.sealed_at,
          registered_units: run.registered_units.length,
        }))
      );
    }
    const evalMatch = path.match(/^\/api\/v1\/evals\/([0-9a-f-]{36})$/);
    if (evalMatch && method === "GET") {
      const run = state.evals.find((r) => r.eval_run_id === evalMatch[1]);
      if (!run) return fulfill(route, 404, { detail: "eval run not found" });
      return fulfill(route, 200, evalDetail(run));
    }
    const evalAction = path.match(/^\/api\/v1\/evals\/([0-9a-f-]{36})\/(resume|seal)$/);
    if (evalAction && method === "POST") {
      const run = state.evals.find((r) => r.eval_run_id === evalAction[1]);
      if (!run) return fulfill(route, 404, { detail: "eval run not found" });
      const action = evalAction[2];
      if (action === "seal") {
        state.lastSeal = { headers: req.headers(), body: req.postDataJSON() ?? {} };
      }
      // 状态机语义对齐 evals/runs.py：sealed 无出边；resume 仅 partial 可走
      if (run.status === "sealed") {
        return fulfill(route, 409, {
          detail: {
            reason: "eval_state_error",
            message: "sealed run allows no transitions",
          },
        });
      }
      if (action === "resume") {
        if (run.status !== "partial") {
          return fulfill(route, 409, {
            detail: { reason: "eval_state_error", message: "only partial runs can resume" },
          });
        }
        run.status = "running";
      } else {
        run.status = "sealed";
        run.sealed_at = "2026-09-06T09:00:00+00:00";
      }
      return fulfill(route, 200, evalDetail(run));
    }
    const evalReport = path.match(/^\/api\/v1\/evals\/([0-9a-f-]{36})\/report$/);
    if (evalReport && method === "GET") {
      const run = state.evals.find((r) => r.eval_run_id === evalReport[1]);
      if (!run || !run.report) {
        return fulfill(route, 404, { detail: "report not available for this eval run" });
      }
      return fulfill(route, 200, run.report);
    }

    // ------------------------------------------------------------------
    // Releases（api/releases.py 契约）
    // ------------------------------------------------------------------
    if (path === "/api/v1/releases" && method === "GET") {
      return fulfill(route, 200, state.releases.map(releaseView));
    }
    const releaseMatch = path.match(/^\/api\/v1\/releases\/([0-9a-f-]{36})$/);
    if (releaseMatch && method === "GET") {
      const release = state.releases.find((r) => r.release_id === releaseMatch[1]);
      if (!release) return fulfill(route, 404, { detail: "release not found" });
      return fulfill(route, 200, releaseView(release));
    }
    const advanceMatch = path.match(/^\/api\/v1\/releases\/([0-9a-f-]{36})\/advance$/);
    if (advanceMatch && method === "POST") {
      const release = state.releases.find((r) => r.release_id === advanceMatch[1]);
      if (!release) return fulfill(route, 404, { detail: "release not found" });
      const body = req.postDataJSON() as { target_state?: string };
      state.lastAdvance = { headers: req.headers(), body };
      const required = TRANSITION_ROLES[`${release.state}>${body.target_state}`];
      if (!required) {
        return fulfill(route, 409, {
          detail: {
            reason: "release_transition_denied",
            message: `release transition ${release.state} -> ${body.target_state} is not allowed`,
          },
        });
      }
      if (!releaseRoles(actor).includes(required)) {
        return fulfill(route, 409, {
          detail: {
            reason: "release_transition_denied",
            message: `role may not advance release ${release.state} -> ${body.target_state}`,
          },
        });
      }
      release.state = body.target_state as string;
      return fulfill(route, 200, releaseView(release));
    }
    const routeMatch = path.match(/^\/api\/v1\/releases\/([0-9a-f-]{36})\/route$/);
    if (routeMatch && method === "POST") {
      const release = state.releases.find((r) => r.release_id === routeMatch[1]);
      if (!release) return fulfill(route, 404, { detail: "release not found" });
      const body = req.postDataJSON() as { suspended?: boolean };
      state.lastRoute = { headers: req.headers(), body };
      // agents/rollout.py：security suspend 先于一切 pin 判定 → RolloutNotConfigured
      if (body.suspended) {
        return fulfill(route, 409, {
          detail: {
            reason: "rollout_not_configured",
            message: "security suspend overrides release pin",
          },
        });
      }
      if (release.default_version === null) {
        return fulfill(route, 409, {
          detail: {
            reason: "rollout_not_configured",
            message: "no cohort match and no default version pin",
          },
        });
      }
      return fulfill(route, 200, { release_id: release.release_id, version: release.default_version });
    }
    const rollbackMatch = path.match(/^\/api\/v1\/releases\/([0-9a-f-]{36})\/rollback$/);
    if (rollbackMatch && method === "POST") {
      const release = state.releases.find((r) => r.release_id === rollbackMatch[1]);
      if (!release) return fulfill(route, 404, { detail: "release not found" });
      const body = req.postDataJSON() as { to_version?: number; in_flight_run_ids?: string[] };
      state.lastRollback = { headers: req.headers(), body };
      // PEP cell agent_publish.rollback = workspace_admin（api/releases.py 冻结矩阵）
      if (!ACTORS[actor].role_bindings.includes("workspace_admin")) {
        return fulfill(route, 403, { detail: "policy denied" });
      }
      if (body.to_version === release.default_version) {
        return fulfill(route, 409, {
          detail: {
            reason: "rollback_not_applicable",
            message: "already the pinned default; nothing to roll back",
          },
        });
      }
      release.default_version = body.to_version ?? null;
      return fulfill(route, 200, {
        release_id: release.release_id,
        applies_to: "new_runs_only",
        executed: false,
        in_flight_disposition: "complete",
        in_flight_run_ids: body.in_flight_run_ids ?? [],
        default_version: release.default_version,
      });
    }

    // ------------------------------------------------------------------
    // Observability + claims + costs（api/observability.py、api/claims.py 契约）
    // ------------------------------------------------------------------
    if (path === "/api/v1/observability/failures" && method === "GET") {
      // 封闭 machine code 清单（telemetry/failures.py FailureCode，按值排序）
      const codes = [
        "APPROVAL_DENIED",
        "APPROVAL_TIMEOUT",
        "CONTEXT_OVERFLOW",
        "DELEGATION_CYCLE",
        "EVIDENCE_UNAVAILABLE",
        "MODEL_REFUSAL",
        "MODEL_TIMEOUT",
        "POLICY_DENY",
        "TOOL_DENIED",
        "TOOL_ERROR",
        "UNKNOWN",
        "WORKFLOW_ERROR",
      ];
      return fulfill(route, 200, { codes: codes.map((code) => ({ code })) });
    }
    if (path === "/api/v1/observability/costs" && method === "GET") {
      return fulfill(route, 200, {
        reservations: [
          {
            reservation_id: "9e8d7c6b-5a4f-4e3d-8b2a-1c0d9e8f7a11",
            run_id: RUN_A,
            amount_usd: "0.0123456",
            price_source: "price-card-2026-09",
            price_confidence: "exact",
            created_at: "2026-09-01T12:00:00+00:00",
          },
          {
            reservation_id: "9e8d7c6b-5a4f-4e3d-8b2a-1c0d9e8f7a22",
            run_id: "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e99",
            amount_usd: "0.0040000",
            price_source: "estimated-internal",
            price_confidence: "estimated",
            created_at: "2026-09-02T12:00:00+00:00",
          },
        ],
        reconciliations: [
          {
            reservation_id: "9e8d7c6b-5a4f-4e3d-8b2a-1c0d9e8f7a11",
            reserved_usd: "0.0123456",
            actual_usd: "0.0111456",
            variance_usd: "-0.0012",
            retry_cost_usd: "0.0000000",
            child_run_cost_usd: "0.0000000",
            tool_external_cost_usd: "0.0000000",
            created_at: "2026-09-01T13:00:00+00:00",
          },
          {
            // 未对账完成：分量 unknown 原样返回（不造 placeholder 0）
            reservation_id: "9e8d7c6b-5a4f-4e3d-8b2a-1c0d9e8f7a22",
            reserved_usd: "0.0040000",
            actual_usd: "unknown",
            variance_usd: "unknown",
            retry_cost_usd: "unknown",
            child_run_cost_usd: "unknown",
            tool_external_cost_usd: "unknown",
            created_at: "2026-09-02T13:00:00+00:00",
          },
        ],
      });
    }
    if (path === "/api/v1/claims" && method === "GET") {
      return fulfill(route, 200, [
        {
          claim_id: "s9-success-rate-offline",
          statement: "Success rate 0.4 on frozen-s9 corpus",
          scope: {
            mode: "offline",
            model: "qwen3-internal",
            version: "v3",
            date: "2026-09-01",
            corpus: "frozen-s9",
            environment: "offline-sandbox",
          },
          status: "offline_verified",
          bound_value: "0.4",
          evidence: {
            eval_run_id: EVAL_SEALED,
            seal_digest: digest("seal"),
            artifact_manifest_id: MANIFEST_ID,
            mode: "offline",
          },
        },
      ]);
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

async function openSection(page: Page, section: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByRole("button", { name: section }).click();
  await expect(page.getByRole("heading", { name: section })).toBeVisible();
}

// ---------------------------------------------------------------------------
// (a) Eval journey：列表 mode/sealed 状态；详情 scope 标签 + 含 refused/error
//     的分母 + CI；unknown 原样展示；resume/seal 动作走真实状态机
// ---------------------------------------------------------------------------

test.describe("S9 eval journey", () => {
  test("lists sealed offline run, renders scope labels and full denominators, unknown stays unknown", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();

    await openSection(page, "Evals");

    const sealedRow = page.getByRole("row", { name: new RegExp(EVAL_SEALED) });
    await expect(sealedRow).toContainText("offline");
    await expect(sealedRow).toContainText("sealed");
    // partial run 未密封：sealed_at 以 unknown 原样呈现（不造假日期/0）
    const partialRow = page.getByRole("row", { name: new RegExp(EVAL_PARTIAL) });
    await expect(partialRow).toContainText("unknown");

    await sealedRow.getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Eval run" })).toBeVisible();
    // scope 标签（来自密封报告，mode 只能来自密封载荷）；exact 避开大小写
    // 不敏感子串与详情头 "Mode: offline" 的歧义
    await expect(page.getByText("mode: offline", { exact: true })).toBeVisible();
    await expect(page.getByText("model: qwen3-internal", { exact: true })).toBeVisible();
    await expect(page.getByText("version: v3", { exact: true })).toBeVisible();
    await expect(page.getByText("date: 2026-09-01", { exact: true })).toBeVisible();
    await expect(page.getByText("corpus: frozen-s9", { exact: true })).toBeVisible();
    await expect(page.getByText("environment: offline-sandbox", { exact: true })).toBeVisible();

    // 状态分解：refused/error 都在完整分母内（reports.py 冻结口径）
    await expect(page.getByText("completed: 2")).toBeVisible();
    await expect(page.getByText("failed: 1")).toBeVisible();
    await expect(page.getByText("refused: 1")).toBeVisible();
    await expect(page.getByText("error: 1")).toBeVisible();
    await expect(page.getByText("n_total: 5")).toBeVisible();

    // 报告提供的 Wilson CI 数字
    await expect(page.getByText(/CI \[0\.1, 0\.74\]/)).toBeVisible();

    // 样本元数据：status + result_digest（正文 canary 不得渲染）
    await expect(page.getByText(/s9-core-004\/u1: refused/)).toBeVisible();

    // metadata-only：canary prompt/result 正文不得出现在 DOM 任何位置
    await expect(page.getByText(/CANARY-/)).toHaveCount(0);

    // partial run：unknown 原样展示 + resume/seal 状态机
    await page.getByRole("button", { name: "Back" }).click();
    await partialRow.getByRole("button", { name: "Open" }).click();
    await expect(page.getByText("Status: partial")).toBeVisible();
    await expect(page.getByText("Sealed at: unknown")).toBeVisible();
    // 无 report → scope/quality 均为 unknown，不造 placeholder 成功值
    await expect(page.getByText("model: unknown", { exact: true })).toBeVisible();
    await expect(page.getByText("Quality: unknown")).toBeVisible();

    await page.getByRole("button", { name: "Resume", exact: true }).click();
    await page.getByRole("button", { name: "Confirm resume" }).click();
    await expect(page.getByText("Status: running")).toBeVisible();

    await page.getByRole("button", { name: "Seal", exact: true }).click();
    await page.getByRole("button", { name: "Confirm seal" }).click();
    await expect(page.getByText("Status: sealed")).toBeVisible();
    await expect(page.getByText("Sealed at: unknown")).toHaveCount(0);

    // report 动作：无密封报告 → 404 → unknown 原样展示（错误显性化）
    await page.getByRole("button", { name: "Load report" }).click();
    await expect(page.getByText("report: unknown")).toBeVisible();

    // mutation PEP 契约：POST seal 携带 CSRF + Idempotency-Key（api.ts）
    expect(state.lastSeal).not.toBeNull();
    expect(state.lastSeal!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastSeal!.headers["idempotency-key"])).toBe(true);

    await expect(page.getByText(/CANARY-/)).toHaveCount(0);
    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (b) Release journey：生命周期 stepper；builder 只能 request（publish 禁用）；
//     admin 推进 + 回滚（new runs only + 在途处置）；suspend 指示器阻断路由
// ---------------------------------------------------------------------------

test.describe("S9 release journey", () => {
  test("builder sees publish disabled; release manager advances, rolls back, suspend blocks routing", async ({ browser }) => {
    const state = newState();

    // builder：staged → published 需要 release_manager，UI 禁用（不硬判，仅显隐）
    const builderContext = await newContextWithMocks(browser, state, "builder");
    const builderPage = await builderContext.newPage();
    await openSection(builderPage, "Releases");
    await expect(
      builderPage.getByRole("row", { name: new RegExp(RELEASE_ID) }).getByRole("button", { name: "Open" })
    ).toBeVisible();
    await builderPage
      .getByRole("row", { name: new RegExp(RELEASE_ID) })
      .getByRole("button", { name: "Open" })
      .click();
    await expect(builderPage.getByRole("heading", { name: "Release" })).toBeVisible();
    await expect(
      builderPage.getByRole("button", { name: "Advance to published" })
    ).toBeDisabled();
    await expect(builderPage.getByRole("button", { name: "Rollback" })).toBeDisabled();
    await builderContext.close();

    // workspace_admin（reviewer/approver/release_manager 并集）：推进 → 回滚 → 路由
    const context = await newContextWithMocks(browser, state, "admin");
    const page = await context.newPage();
    await openSection(page, "Releases");
    await page
      .getByRole("row", { name: new RegExp(RELEASE_ID) })
      .getByRole("button", { name: "Open" })
      .click();
    await expect(page.getByRole("heading", { name: "Release" })).toBeVisible();

    // stepper：完整生命周期 + 当前态
    const stepper = page.getByRole("list", { name: "Release lifecycle" });
    await expect(stepper).toBeVisible();
    await expect(stepper.getByText("staged (current)")).toBeVisible();
    await expect(stepper.getByText("published")).toBeVisible();
    await expect(stepper.getByText("retired")).toBeVisible();

    // manifest 元数据：digest + default pin；approver/cohorts API 未暴露 → unknown
    await expect(page.getByText(/manifest digest: sha256:/)).toBeVisible();
    await expect(page.getByText("approver: unknown")).toBeVisible();
    await expect(page.getByText("cohorts: unknown")).toBeVisible();

    await page.getByRole("button", { name: "Advance to published" }).click();
    await expect(page.getByText("State: published")).toBeVisible();
    expect(state.lastAdvance!.body).toMatchObject({ target_state: "published" });

    // 回滚：对话框明示 new runs only + 在途处置声明
    await page.getByRole("button", { name: "Rollback" }).click();
    await expect(page.getByText(/applies to new runs only/)).toBeVisible();
    await page.getByLabel("Roll back to version").fill("1");
    await page.getByLabel("In-flight run IDs (comma separated)").fill(RUN_A);
    await page.getByRole("button", { name: "Confirm rollback" }).click();
    await expect(page.getByText("applies to: new runs only")).toBeVisible();
    await expect(page.getByText("executed: false")).toBeVisible();
    await expect(page.getByText("in-flight disposition: complete")).toBeVisible();
    await expect(page.getByText(`in-flight run ids: ${RUN_A}`)).toBeVisible();
    await expect(page.getByText("default version: 1")).toBeVisible();

    // mutation PEP 契约：rollback 携带 CSRF + Idempotency-Key
    expect(state.lastRollback).not.toBeNull();
    expect(state.lastRollback!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastRollback!.headers["idempotency-key"])).toBe(true);

    // 路由解析：回滚后 default pin 已是 1 → 命中 pin 返回 version 1
    await page.getByRole("button", { name: "Resolve route" }).click();
    await expect(page.getByText("route: version 1")).toBeVisible();

    // suspend 指示器常驻 + security suspend 阻断路由（先于 pin 判定）
    await expect(
      page.getByText("Security suspend overrides all release pins when active.")
    ).toBeVisible();
    await page.getByLabel("Suspended").check();
    await page.getByRole("button", { name: "Resolve route" }).click();
    await expect(page.getByText(/security suspend overrides release pin/)).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (c) Observability + costs journey：machine code taxonomy、claim 元数据、
//     价格来源/置信度 + variance 行；正文 canary 全程不出现
// ---------------------------------------------------------------------------

test.describe("S9 observability and costs journey", () => {
  test("failure taxonomy lists machine codes; cost table shows price source, confidence and variance", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();

    await openSection(page, "Observability");
    await expect(page.getByText("MODEL_TIMEOUT")).toBeVisible();
    await expect(page.getByText("MODEL_REFUSAL")).toBeVisible();
    await expect(page.getByText("TOOL_ERROR")).toBeVisible();
    // claim 注册表（元数据）：status + seal digest
    await expect(page.getByText("s9-success-rate-offline")).toBeVisible();
    await expect(page.getByText("offline_verified")).toBeVisible();
    await expect(page.getByText(/seal: sha256:/)).toBeVisible();
    await expect(page.getByText(/CANARY-/)).toHaveCount(0);

    await page.getByRole("button", { name: "Costs" }).click();
    await expect(page.getByRole("heading", { name: "Costs" })).toBeVisible();
    await expect(page.getByText("price-card-2026-09")).toBeVisible();
    await expect(page.getByText("exact")).toBeVisible();
    await expect(page.getByText("estimated-internal", { exact: true })).toBeVisible();
    await expect(page.getByText("estimated", { exact: true })).toBeVisible();
    // variance 行（对账完成）+ unknown 分量原样展示
    await expect(page.getByText("-0.0012")).toBeVisible();
    await expect(page.getByText("actual: unknown")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (d) Auditor read-only：同页面可渲染，所有 mutation 控件禁用且零请求
// ---------------------------------------------------------------------------

test.describe("S9 auditor read-only", () => {
  test("auditor renders evidence pages with every mutation control disabled", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "auditor");
    const page = await context.newPage();

    await openSection(page, "Evals");
    await page
      .getByRole("row", { name: new RegExp(EVAL_PARTIAL) })
      .getByRole("button", { name: "Open" })
      .click();
    await expect(page.getByText("Status: partial")).toBeVisible();
    await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Seal", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Load report" })).toBeDisabled();

    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Releases" }).click();
    await page
      .getByRole("row", { name: new RegExp(RELEASE_ID) })
      .getByRole("button", { name: "Open" })
      .click();
    await expect(page.getByRole("button", { name: "Advance to published" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Rollback" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Resolve route" })).toBeDisabled();

    await page.getByRole("button", { name: "Observability" }).click();
    await expect(page.getByText("MODEL_TIMEOUT")).toBeVisible();
    await page.getByRole("button", { name: "Costs" }).click();
    await expect(page.getByText("price-card-2026-09")).toBeVisible();

    // 零 mutation 请求（auditor 会话不应发出任何写路径）
    expect(state.lastSeal).toBeNull();
    expect(state.lastAdvance).toBeNull();
    expect(state.lastRollback).toBeNull();
    expect(state.lastRoute).toBeNull();

    await context.close();
  });
});
