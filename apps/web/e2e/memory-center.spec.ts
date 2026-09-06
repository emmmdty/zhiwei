// S7 memory-center e2e（specs/s7-memory.md §5 → Gate 例外条目
// docs/handoffs/s7-memory-center-e2e-exception.md 的解锁 spec）。
//
// 与 S7 §5 Memory Center journey 的映射（以真实路由/UI 为准，mock 模式先例
// runtime-approval.spec.ts / full-product.spec.ts）：
//   spec §5 旅程                                  | 本 spec 落点（真实 UI）
//   ----------------------------------------------+----------------------------------
//   查看本人和可见团队/Case memory                | MemoryView 列表：本人 user
//                                                 | scope + 可见 team/case 行；
//                                                 | 他人 user scope 行不可见
//                                                 | （server 可见性，mock 镜像
//                                                 | api/memory.py list_for_tenant）
//   按来源/类型/状态筛选                          | Source/Type/Status 控件 →
//                                                 | GET /records?source|type|status
//                                                 | （server 过滤）
//   confirm（团队确认仅 Steward，server-driven）  | RecordDetailView Confirm：
//                                                 | team 记录 steward-only 入口；
//                                                 | 非 team 记录本人即可确认
//                                                 | （api/memory.py confirm_record
//                                                 | 仅对 team scope 做
//                                                 | _STEWARD_ROLE_NAMES 门禁，
//                                                 | tests/contract/memory
//                                                 | test_confirm_own_candidate 冻结）
//   revoke                                        | 两段式 Revoke → status
//                                                 | revoked + tombstone true
//   状态投影（DATA_MODEL 词汇）                   | candidate/confirmed/superseded/
//                                                 | revoked/expired 逐字呈现
//                                                 |（种子 + journey 产物）
//   删除显示 cascade/tombstone 边界               | Delete（204）→ refetch 后
//                                                 | status=revoked + tombstone=
//                                                 | true；API 未返回的 cascade
//                                                 | 细节不渲染（诚实缺席）
//   correct/resolve/export（扩展）                | Correct（supersede + 新版本）、
//                                                 | Conflicts resolve、Export
//                                                 | count、Stats 投影
//
// 后端边界（mock 模式，operator 已授权同 runtime-approval.spec.ts）：响应形状
// 1:1 对齐 src/zhiwei/api/memory.py 的 Pydantic 投影；不发任何真实外部请求；
// 未模拟的 /api 路径一律 500 显式失败（fail loud）。

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
const MEMBER_ID = "f740acc5-03c3-486e-8384-2a9335fd4285";
const STEWARD_ID = "9b1e4c7d-52a8-4f63-b0e2-6d8a9c1f7e55";
const OTHER_ID = "c4a8d2e6-71b9-4c05-8f3a-2e9b7d5c1a66";
const CASE_ID = "d5b9e3f7-82ca-4d16-9a4b-3fac8e6d2b77";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// mock 域确定性 id（池序号分配，同 full-product.spec.ts 先例）
function newSeqId(pool: number, seq: number): string {
  return `00000000-${String(pool).padStart(4, "0")}-4000-8000-${String(seq).padStart(12, "0")}`;
}

type Actor = "member" | "steward";

// 平台角色绑定（policy/roles.py Role 冻结词汇）。member 无 steward 角色——
// team 记录确认入口对其隐藏（UI 镜像 server 门禁），本人 user scope 记录
// 确认不受 steward 门禁（api/memory.py 仅 team scope 门禁）。
const ACTORS: Record<Actor, { principal: string; role_bindings: string[] }> = {
  member: { principal: MEMBER_ID, role_bindings: ["member"] },
  steward: { principal: STEWARD_ID, role_bindings: ["memory_steward"] },
};

// ---------------------------------------------------------------------------
// mock 域状态（字段名 = api/memory.py MemoryRecordResponse / ConflictResponse /
// ExportResponse / MemoryStatsResponse 投影字段名）
// ---------------------------------------------------------------------------

interface MemoryRecord {
  id: string;
  version: number;
  organization_id: string;
  workspace_id: string;
  scope: string;
  scope_subject_id: string;
  type: string;
  subject: string;
  key: string;
  canonical_value: string;
  source_refs: { source_id: string; source_type: string; description: string }[];
  observed_at: string;
  confidence: number;
  sensitivity: string;
  status: string;
  author_ref: string;
  approver_ref: string | null;
  conflict_refs: string[];
  created_at: string;
  updated_at: string;
  tombstone: boolean;
}

interface MemoryConflict {
  conflict_id: string;
  kind: string;
  record_a_id: string;
  record_b_id: string;
  detected_at: string;
  resolved: boolean;
  resolved_by: string | null;
  resolved_at: string | null;
}

interface MockState {
  records: MemoryRecord[];
  conflicts: MemoryConflict[];
  seq: { corrected: number };
  lastMutation: { method: string; path: string; headers: Record<string, string> } | null;
}

// 记录种子（MemoryRecordResponse 全字段；状态词汇覆盖 DATA_MODEL 五态中的
// candidate/confirmed/expired，superseded/revoked 由 journey 产物呈现）
const OWN_CANDIDATE = "f1000000-0000-4000-8000-000000000001";
const TEAM_CANDIDATE = "f1000000-0000-4000-8000-000000000002";
const CASE_RECORD = "f1000000-0000-4000-8000-000000000003";
const EXPIRED_RECORD = "f1000000-0000-4000-8000-000000000004";
const OTHER_USER_RECORD = "f1000000-0000-4000-8000-000000000005";
const TEAM_CONFIRMED = "f1000000-0000-4000-8000-000000000006";
const CONFLICT_ID = "f1000000-0000-4000-8000-000000000007";

function memoryRecord(id: string, overrides: Partial<MemoryRecord>): MemoryRecord {
  return {
    id,
    version: 1,
    organization_id: ORG_ID,
    workspace_id: WS_ID,
    scope: "team",
    scope_subject_id: WS_ID,
    type: "fact",
    subject: "deploy-window",
    key: "deploy-window",
    canonical_value: "Deploy window is Tuesday 02:00 UTC",
    source_refs: [
      { source_id: "runbook-1", source_type: "knowledge", description: "ops runbook" },
    ],
    observed_at: "2026-09-01T08:00:00+00:00",
    confidence: 0.8,
    sensitivity: "low",
    status: "candidate",
    author_ref: MEMBER_ID,
    approver_ref: null,
    conflict_refs: [],
    created_at: "2026-09-01T08:00:00+00:00",
    updated_at: "2026-09-01T08:00:00+00:00",
    tombstone: false,
    ...overrides,
  };
}

function newState(): MockState {
  return {
    records: [
      // 本人 user scope candidate（member 的 confirm journey 主角）
      memoryRecord(OWN_CANDIDATE, {
        scope: "user",
        scope_subject_id: MEMBER_ID,
        type: "preference",
        subject: "report-style",
        key: "report-style",
        canonical_value: "Prefers concise weekly reports",
        source_refs: [
          { source_id: "ask-1", source_type: "ask_answer", description: "ask run" },
        ],
        confidence: 0.9,
      }),
      // 可见 team candidate（steward confirm + revoke journey 主角）
      memoryRecord(TEAM_CANDIDATE, { status: "candidate" }),
      // 可见 Case memory
      memoryRecord(CASE_RECORD, {
        scope: "case",
        scope_subject_id: CASE_ID,
        type: "episode",
        subject: "incident-42",
        key: "incident-42",
        canonical_value: "Incident 42 traced to stale cache",
        source_refs: [
          { source_id: "case-42", source_type: "runbook", description: "case notes" },
        ],
      }),
      // TTL 过期（expired + tombstone——spec §5「自动转 expired 并留 tombstone」）
      memoryRecord(EXPIRED_RECORD, {
        type: "decision",
        status: "expired",
        tombstone: true,
        canonical_value: "Legacy queue retired",
      }),
      // 他人 user scope（对 member/steward 均不可见——server 可见性）
      memoryRecord(OTHER_USER_RECORD, {
        scope: "user",
        scope_subject_id: OTHER_ID,
        subject: "other-private",
        key: "other-private",
        canonical_value: "Another user's private note",
      }),
      // team confirmed（correct/delete journey 主角）
      memoryRecord(TEAM_CONFIRMED, {
        status: "confirmed",
        approver_ref: STEWARD_ID,
      }),
    ],
    conflicts: [
      {
        conflict_id: CONFLICT_ID,
        kind: "contradiction",
        record_a_id: TEAM_CANDIDATE,
        record_b_id: TEAM_CONFIRMED,
        detected_at: "2026-09-02T08:00:00+00:00",
        resolved: false,
        resolved_by: null,
        resolved_at: null,
      },
    ],
    seq: { corrected: 0 },
    lastMutation: null,
  };
}

// ---------------------------------------------------------------------------
// 网络层 mock：单一 catch-all 路由内部分发；未模拟路径 500 fail loud
// ---------------------------------------------------------------------------

function installApiMocks(context: BrowserContext, state: MockState, actor: Actor): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const principal = () => ACTORS[actor].principal;
  const isSteward = () => ACTORS[actor].role_bindings.includes("memory_steward");
  // api/memory.py list_for_tenant 的可见性镜像：user scope 仅本人
  const visible = (r: MemoryRecord) =>
    r.scope !== "user" || r.scope_subject_id === principal();

  context.route("/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();

    // 会话引导（session.tsx 消费的真实契约）
    if (path === "/api/v1/me" && method === "GET") {
      return fulfill(route, 200, {
        principal: { id: principal() },
        organizations: [{ id: ORG_ID, status: "active" }],
        context: { organization_id: ORG_ID, workspace_id: WS_ID },
        csrf_token: CSRF,
      });
    }
    if (path === "/api/v1/organizations" && method === "GET") {
      return fulfill(route, 200, [{ id: ORG_ID, status: "active" }]);
    }
    if (path === `/api/v1/organizations/${ORG_ID}/members` && method === "GET") {
      return fulfill(route, 200, Object.values(ACTORS).map((a) => ({
        principal_id: a.principal,
        organization_id: ORG_ID,
        role_bindings: a.role_bindings,
      })));
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    // Workbench 默认分区挂载拉取（本 spec 不消费 run 面）
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, []);
    }

    // ------------------------------------------------------------------
    // Memory（api/memory.py 契约，逐字段对齐 Pydantic 投影）
    // ------------------------------------------------------------------
    if (path === "/api/v1/memory/records" && method === "GET") {
      const params = new URL(req.url()).searchParams;
      const rows = state.records.filter((r) => {
        if (!visible(r)) return false;
        if (params.get("type") && r.type !== params.get("type")) return false;
        if (params.get("status") && r.status !== params.get("status")) return false;
        if (
          params.get("source") &&
          !r.source_refs.some((sr) => sr.source_type === params.get("source"))
        ) {
          return false;
        }
        return true;
      });
      return fulfill(route, 200, rows);
    }
    const recordMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})$/);
    if (recordMatch && method === "GET") {
      const record = state.records.find((r) => r.id === recordMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      return fulfill(route, 200, record);
    }
    const confirmMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/confirm$/);
    if (confirmMatch && method === "POST") {
      const record = state.records.find((r) => r.id === confirmMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      state.lastMutation = { method, path, headers: req.headers() };
      // 团队记忆确认仅 Steward（api/memory.py confirm_record 403 原文）
      if (record.scope === "team" && !isSteward()) {
        return fulfill(route, 403, {
          detail: "only Memory Steward can confirm team records",
        });
      }
      if (record.status !== "candidate") {
        return fulfill(route, 409, {
          detail: `record status is ${record.status}, expected candidate`,
        });
      }
      record.status = "confirmed";
      record.approver_ref = principal();
      record.updated_at = "2026-09-06T10:00:00+00:00";
      return fulfill(route, 200, record);
    }
    const correctMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/correct$/);
    if (correctMatch && method === "POST") {
      const original = state.records.find((r) => r.id === correctMatch[1]);
      if (!original) return fulfill(route, 404, { detail: "memory record not found" });
      const body = req.postDataJSON() as { canonical_value: string; subject?: string };
      state.lastMutation = { method, path, headers: req.headers() };
      // 原 record 转 superseded，纠正版本新 id + version+1（api/memory.py 语义）
      original.status = "superseded";
      original.updated_at = "2026-09-06T10:00:00+00:00";
      state.seq.corrected += 1;
      const corrected: MemoryRecord = {
        ...original,
        id: newSeqId(8, state.seq.corrected),
        version: original.version + 1,
        canonical_value: body.canonical_value,
        subject: body.subject ?? original.subject,
        status: "confirmed",
        approver_ref: principal(),
        created_at: "2026-09-06T10:00:00+00:00",
        updated_at: "2026-09-06T10:00:00+00:00",
      };
      state.records.push(corrected);
      return fulfill(route, 200, corrected);
    }
    const revokeMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/revoke$/);
    if (revokeMatch && method === "POST") {
      const record = state.records.find((r) => r.id === revokeMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      state.lastMutation = { method, path, headers: req.headers() };
      if (["superseded", "revoked", "expired"].includes(record.status)) {
        return fulfill(route, 409, {
          detail: `record is already in terminal status: ${record.status}`,
        });
      }
      record.status = "revoked";
      record.tombstone = true;
      record.updated_at = "2026-09-06T10:00:00+00:00";
      return fulfill(route, 200, record);
    }
    const deleteMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/delete$/);
    if (deleteMatch && method === "POST") {
      const record = state.records.find((r) => r.id === deleteMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      state.lastMutation = { method, path, headers: req.headers() };
      // 软删除：revoked + tombstone（api/memory.py delete_record 语义）
      record.status = "revoked";
      record.tombstone = true;
      record.updated_at = "2026-09-06T10:00:00+00:00";
      return fulfill(route, 204, null);
    }
    if (path === "/api/v1/memory/conflicts" && method === "GET") {
      return fulfill(route, 200, state.conflicts.filter((c) => !c.resolved));
    }
    if (path === "/api/v1/memory/conflicts/resolve" && method === "POST") {
      const body = req.postDataJSON() as { conflict_id: string };
      const conflict = state.conflicts.find(
        (c) => c.conflict_id === body.conflict_id && !c.resolved
      );
      if (!conflict) {
        return fulfill(route, 404, { detail: "conflict not found or already resolved" });
      }
      state.lastMutation = { method, path, headers: req.headers() };
      conflict.resolved = true;
      conflict.resolved_by = principal();
      conflict.resolved_at = "2026-09-06T10:00:00+00:00";
      return fulfill(route, 200, conflict);
    }
    if (path === "/api/v1/memory/export" && method === "POST") {
      state.lastMutation = { method, path, headers: req.headers() };
      const rows = state.records.filter(visible);
      return fulfill(route, 200, { records: rows, count: rows.length });
    }
    if (path === "/api/v1/memory/stats" && method === "GET") {
      const rows = state.records.filter(visible);
      const byStatus: Record<string, number> = {};
      const byScope: Record<string, number> = {};
      const byType: Record<string, number> = {};
      for (const r of rows) {
        byStatus[r.status] = (byStatus[r.status] ?? 0) + 1;
        byScope[r.scope] = (byScope[r.scope] ?? 0) + 1;
        byType[r.type] = (byType[r.type] ?? 0) + 1;
      }
      return fulfill(route, 200, {
        total_records: rows.length,
        by_status: byStatus,
        by_scope: byScope,
        by_type: byType,
        unresolved_conflicts: state.conflicts.filter((c) => !c.resolved).length,
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

async function openMemory(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByRole("button", { name: "Memory", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Memory", exact: true })).toBeVisible();
}

function recordRow(page: Page, recordId: string) {
  return page
    .getByRole("table", { name: "Records" })
    .getByRole("row", { name: new RegExp(recordId) });
}

// ---------------------------------------------------------------------------
// (1) Member journey：本人 + 可见 team/Case memory；来源/类型/状态筛选；
//     team 记录确认入口 steward-only（无该角色不渲染）；本人 user scope 记录
//     确认不受 steward 门禁（api/memory.py 仅 team scope 门禁——
//     tests/contract/memory test_confirm_own_candidate 冻结语义）
// ---------------------------------------------------------------------------

test.describe("S7 member journey", () => {
  test("lists own and visible team/case memory, filters, confirms own user-scope record", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "member");
    const page = await context.newPage();

    await openMemory(page);
    const recordsTable = page.getByRole("table", { name: "Records" });

    // 可见性矩阵：本人 user + team + case 可见；他人 user scope 不可见
    for (const id of [OWN_CANDIDATE, TEAM_CANDIDATE, CASE_RECORD, EXPIRED_RECORD, TEAM_CONFIRMED]) {
      await expect(recordsTable.getByRole("row", { name: new RegExp(id) })).toBeVisible();
    }
    await expect(recordsTable.getByRole("row", { name: new RegExp(OTHER_USER_RECORD) })).toHaveCount(0);
    await expect(recordRow(page, CASE_RECORD)).toContainText("case");

    // 类型筛选（server 过滤）：preference → 仅本人 user scope 记录
    await page.getByLabel("Type").selectOption("preference");
    await expect(recordRow(page, OWN_CANDIDATE)).toBeVisible();
    await expect(recordRow(page, TEAM_CANDIDATE)).toHaveCount(0);
    await page.getByLabel("Type").selectOption("");

    // 状态筛选：expired → 仅 TTL 过期记录（tombstone 留存）
    await page.getByLabel("Status").selectOption("expired");
    await expect(recordRow(page, EXPIRED_RECORD)).toBeVisible();
    await expect(recordRow(page, OWN_CANDIDATE)).toHaveCount(0);
    await recordRow(page, EXPIRED_RECORD).getByRole("button", { name: "Open" }).click();
    await expect(page.getByText("status: expired")).toBeVisible();
    await expect(page.getByText("tombstone: true")).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByLabel("Status").selectOption("");

    // 来源筛选：knowledge source_type → 仅 team candidate
    await page.getByLabel("Source").fill("knowledge");
    await expect(recordRow(page, TEAM_CANDIDATE)).toBeVisible();
    await expect(recordRow(page, CASE_RECORD)).toHaveCount(0);
    await page.getByLabel("Source").fill("");

    // team 记录确认入口 steward-only：member 无 Confirm 控件（server 403 兜底
    // 不该被触发——入口先行隐藏）
    await recordRow(page, TEAM_CANDIDATE).getByRole("button", { name: "Open" }).click();
    await expect(page.getByText("scope: team")).toBeVisible();
    await expect(page.getByRole("button", { name: "Confirm", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Back" }).click();

    // 本人 user scope candidate：确认入口可见且可用（非 team scope 不做
    // steward 门禁）→ confirmed + approver 本人
    await recordRow(page, OWN_CANDIDATE).getByRole("button", { name: "Open" }).click();
    await expect(page.getByText("status: candidate")).toBeVisible();
    await expect(page.getByText("tombstone: false")).toBeVisible();
    const confirmButton = page.getByRole("button", { name: "Confirm", exact: true });
    await expect(confirmButton).toBeVisible();
    await confirmButton.click();
    await expect(page.getByText("status: confirmed")).toBeVisible();
    await expect(page.getByText(`approver: ${MEMBER_ID}`)).toBeVisible();
    // mutation PEP 契约：CSRF + Idempotency-Key（api client 统一注入）
    expect(state.lastMutation).not.toBeNull();
    expect(state.lastMutation!.headers["x-csrf-token"]).toBe(CSRF);
    expect(UUID_RE.test(state.lastMutation!.headers["idempotency-key"])).toBe(true);
    await page.getByRole("button", { name: "Back" }).click();
    await expect(recordRow(page, OWN_CANDIDATE)).toContainText("confirmed");

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (2) Steward journey：team 记录确认（server-driven 门禁的正向面）→ confirmed
//     + approver 投影；revoke → revoked + tombstone（状态投影）
// ---------------------------------------------------------------------------

test.describe("S7 steward journey", () => {
  test("confirms team candidate as steward, then revokes with tombstone projection", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "steward");
    const page = await context.newPage();

    await openMemory(page);
    const recordsTable = page.getByRole("table", { name: "Records" });

    // steward 可见 team/case 面；member 的 user scope 记录不可见
    await expect(recordRow(page, TEAM_CANDIDATE)).toBeVisible();
    await expect(recordRow(page, OWN_CANDIDATE)).toHaveCount(0);

    // team candidate：steward 确认入口可见 → confirmed + approver 投影
    await recordRow(page, TEAM_CANDIDATE).getByRole("button", { name: "Open" }).click();
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page.getByText("status: confirmed")).toBeVisible();
    await expect(page.getByText(`approver: ${STEWARD_ID}`)).toBeVisible();

    // revoke（两段式）→ revoked + tombstone true（S7 状态投影）
    await page.getByRole("button", { name: "Revoke" }).click();
    await page.getByRole("button", { name: "Confirm revoke" }).click();
    await expect(page.getByText("status: revoked")).toBeVisible();
    await expect(page.getByText("tombstone: true")).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(recordRow(page, TEAM_CANDIDATE)).toContainText("revoked");

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (3) 扩展 journey：correct（supersede + 新版本）→ delete（cascade/tombstone
//     边界）→ conflicts resolve → export → stats
// ---------------------------------------------------------------------------

test.describe("S7 correct, delete boundary, resolve, export, stats", () => {
  test("corrects with supersede projection, deletes at tombstone boundary, resolves, exports, stats", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "steward");
    const page = await context.newPage();

    await openMemory(page);
    const recordsTable = page.getByRole("table", { name: "Records" });

    // correct → 原 record superseded + 纠正版本 confirmed（version+1）
    await recordRow(page, TEAM_CONFIRMED).getByRole("button", { name: "Open" }).click();
    await expect(page.getByText("version: 1")).toBeVisible();
    await page.getByLabel("Corrected value").fill("Deploy window is Tuesday 03:00 UTC");
    await page.getByRole("button", { name: "Submit correction" }).click();
    await expect(page.getByText("status: confirmed")).toBeVisible();
    await expect(page.getByText("version: 2")).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(recordRow(page, TEAM_CONFIRMED)).toContainText("superseded");

    // delete（204）→ refetch 后按记录呈现 cascade/tombstone 边界：revoked +
    // tombstone true；API 未返回的 index/cache cascade 细节不渲染（诚实缺席）
    await recordRow(page, TEAM_CONFIRMED).getByRole("button", { name: "Open" }).click();
    await page.getByRole("button", { name: "Delete" }).click();
    await page.getByRole("button", { name: "Confirm delete" }).click();
    await expect(page.getByText("status: revoked")).toBeVisible();
    await expect(page.getByText("tombstone: true")).toBeVisible();
    await expect(page.getByText(/index cascade|cache cascade/i)).toHaveCount(0);

    // conflicts：未解决冲突 → resolve → 空态
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Conflicts" }).click();
    const conflictsTable = page.getByRole("table", { name: "Conflicts" });
    await expect(conflictsTable.getByRole("row", { name: new RegExp(CONFLICT_ID) })).toContainText(
      "contradiction"
    );
    await conflictsTable
      .getByRole("row", { name: new RegExp(CONFLICT_ID) })
      .getByRole("button", { name: "Resolve" })
      .click();
    await expect(page.getByText("No unresolved conflicts")).toBeVisible();

    // export：真实 ExportResponse count 投影
    await page.getByRole("button", { name: "Export" }).click();
    await expect(page.getByText("exported records: 5")).toBeVisible();

    // stats：真实 MemoryStatsResponse 投影
    await page.getByRole("button", { name: "Stats" }).click();
    await expect(page.getByText("total records: 5")).toBeVisible();
    await expect(page.getByText("unresolved conflicts: 0")).toBeVisible();

    await context.close();
  });
});
