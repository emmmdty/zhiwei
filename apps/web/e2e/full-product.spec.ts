// S10-T4 full-product e2e（specs/s10 §5/§6，plan Task 4）。
//
// 与后端契约的映射（网络层 mock，响应形状逐字段对齐真实投影）：
//   /api/v1/capabilities/**  → api/capabilities.py（ProviderRecord /
//                               CapabilityVersionRecord / VersionDiffRecord /
//                               BindingRecord）
//   /api/v1/connections/**   → api/connections.py（ConnectionRecord /
//                               ConnectionStatusRecord）
//   /api/v1/knowledge/**     → api/knowledge.py（SourceRecord / SyncResultRecord /
//                               SourceStatusRecord / SourceVersionRecord）
//   /api/v1/memory/**        → api/memory.py（MemoryRecordResponse /
//                               ConflictResponse / ExportResponse /
//                               MemoryStatsResponse）
//   Admin 分区               → 复用既有面板（AuditLogPanel / CostsView /
//                               MembersPanel），不新增后端调用
//
// 五角色 journey（§5）：Admin（org/ws/members/policy/audit/cost）、Publisher
// （capability import→inspect→bind→suspend + connections）、Builder（knowledge
// connect/sync + bind capability）、Member（workbench runs；ask/discover 的
// renderer journey 归 T4b spec，这里只断言分区入口存在）、Approver/Auditor
// （approvals read + audit + read-only everywhere）。
//
// 共享状态类别（§5 每类至少一个代表性断言）：
//   loading/empty  → knowledge 空源空态、各表 empty 态
//   error/offline  → GET sources route.abort 一次 → error 态 → Retry 恢复
//   403            → member 的 knowledge list 被 PEP 拒（403 → 结构化错误面）
//   conflict       → 409 机器可读拒绝面上浮（memory confirm 重复 / bind 未发布
//                    版本；四个 router 无 412/CAS 路径——api 客户端的
//                    CasConflictError 语义由 studio-draft.spec.ts 覆盖，见
//                    route-coverage.ts 清单注释）
//   stale          → 窗口 focus 后分区 refetch（GET sources 计数 +1）
//
// 纪律：
// - 角色显隐只管 UI 入口，权限由 server PEP 强制（§4 最后一段）；mock 的拒绝
//   语义对齐真实 router 的 403/409 形状。
// - 检视元数据逐字渲染 API 真实字段；不发明 SBOM/漏洞数据。
// - 不发任何真实外部请求；未模拟的 /api 路径一律 500 显式失败（fail loud）。

import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Page,
  type Route,
} from "@playwright/test";
import {
  INVENTORY,
  inventedApiCalls,
  uncoveredBackendEndpoints,
} from "./route-coverage";

// 固定标识（本 spec 内 mock 域；不依赖 compose 种子数据）
const ORG_ID = "3a1a8d1c-a63f-4bed-87d1-b67948aea7ac";
const WS_ID = "6f1c2a34-9b7e-4d0a-8f61-0c5b2d7e9a11";
const BUILDER_ID = "3383f6a7-d17b-44c2-802c-d67c3974e13a";
const ADMIN_ID = "2b6c4d8e-91f0-4a57-b3c2-5d8e7f9a0b01";
const PUBLISHER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
const MEMBER_ID = "f740acc5-03c3-486e-8384-2a9335fd4285";
const APPROVER_ID = "4a3e5ad8-f81e-431d-937f-55b98def2bf2";
const AUDITOR_ID = "63d7ef96-75e0-4c47-8edb-10dd834c9f64";
const CSRF = "e2e-csrf-token";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// mock 域确定性 id（provider 池序号分配，见 newSeqId）
const RUN_ID = "c0d1e2f3-a4b5-4c6d-8e7f-0a1b2c3d4e01";
const APPROVAL_ID = "9d100000-0000-4000-8000-000000000001";
const MEMORY_CANDIDATE = "f1000000-0000-4000-8000-000000000001";
const MEMORY_CONFIRMED = "f1000000-0000-4000-8000-000000000002";
const MEMORY_CONFLICT = "f1000000-0000-4000-8000-000000000003";

function newSeqId(pool: number, seq: number): string {
  return `00000000-${String(pool).padStart(4, "0")}-4000-8000-${String(seq).padStart(12, "0")}`;
}

function digest(seed: string): string {
  let hex = "";
  for (let i = 0; i < 64; i++) hex += ((seed.charCodeAt(i % seed.length) + i) % 16).toString(16);
  return `sha256:${hex}`;
}

type Actor = "admin" | "publisher" | "builder" | "member" | "approver" | "auditor";

// 平台角色绑定（policy/roles.py Role 冻结词汇；session.tsx 经 members 列表解析）。
// publisher 兼持 security_admin：S4 旅程的 suspend/revoke 是 security-admin 门禁，
// 本 spec 的发布者 persona 同时承担发布与安全停用（org 级绑定可并存）。
// admin 兼持 memory_steward：S7 旅程的团队记忆确认入口（server 侧
// _STEWARD_ROLE_NAMES 门禁的 mock 侧镜像）。
const ACTORS: Record<Actor, { principal: string; role_bindings: string[] }> = {
  admin: { principal: ADMIN_ID, role_bindings: ["org_owner", "workspace_admin", "memory_steward"] },
  publisher: { principal: PUBLISHER_ID, role_bindings: ["capability_publisher", "security_admin"] },
  builder: { principal: BUILDER_ID, role_bindings: ["agent_builder"] },
  member: { principal: MEMBER_ID, role_bindings: ["member"] },
  approver: { principal: APPROVER_ID, role_bindings: ["approver"] },
  auditor: { principal: AUDITOR_ID, role_bindings: ["auditor"] },
};

// ---------------------------------------------------------------------------
// mock 域状态（字段名 = 后端投影字段名）
// ---------------------------------------------------------------------------

interface ProviderRecord {
  id: string;
  provider_id: string;
  name: string;
  version: number;
  description: string;
  status: string;
  classification: string;
  source_url: string | null;
  risk_level: string;
  content_digest: string;
}

interface CapabilityVersionRecord {
  id: string;
  capability_type: string;
  name: string;
  version: number;
  status: string;
  risk_level: string;
  content_digest: string;
  test_digest: string | null;
  parent_id: string | null;
  metadata: Record<string, string>;
}

interface BindingRecord {
  id: string;
  organization_id: string;
  workspace_id: string;
  agent_definition_id: string;
  agent_version_id: string;
  capability_version_id: string;
  status: string;
}

interface ConnectionRecord {
  id: string;
  organization_id: string;
  workspace_id: string;
  provider_version_id: string;
  subject_mode: string;
  status: string;
  principal_id: string | null;
  version: number;
  fingerprint: string;
}

interface KnowledgeSourceRecord {
  id: string;
  source_type: string;
  connector: string;
  uri: string;
  classification: string;
  status: string;
  version_count: number;
  latest_version_seq: number | null;
  latest_content_digest: string | null;
  acl_allowed_principals: string[];
  acl_denied_principals: string[];
  acl_allowed_groups: string[];
}

interface KnowledgeVersionRow {
  id: string;
  source_object_id: string;
  version_seq: number;
  connector: string;
  uri: string;
  content_digest: string;
  state: string;
  classification: string;
  observed_at: string;
  valid_at: string;
  connector_version: string;
  parser_version: string;
  index_version: string;
}

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
  providers: ProviderRecord[];
  capVersions: CapabilityVersionRecord[];
  bindings: BindingRecord[];
  connections: ConnectionRecord[];
  knowledgeSources: KnowledgeSourceRecord[];
  knowledgeVersions: Record<string, KnowledgeVersionRow[]>;
  memoryRecords: MemoryRecord[];
  memoryConflicts: MemoryConflict[];
  runs: { run_id: string; status: string; organization_id: string }[];
  seq: { provider: number; capVersion: number; binding: number; connection: number; source: number; sourceVersion: number; run: number };
  sourcesGetCount: number;
  offlineMode: boolean;
  lastBind: { body: Record<string, string> } | null;
  lastMutation: string | null;
}

function memoryRecord(
  id: string,
  overrides: Partial<MemoryRecord>
): MemoryRecord {
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
    author_ref: BUILDER_ID,
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
    providers: [],
    capVersions: [],
    bindings: [],
    connections: [],
    knowledgeSources: [],
    knowledgeVersions: {},
    memoryRecords: [
      memoryRecord(MEMORY_CANDIDATE, { status: "candidate" }),
      memoryRecord(MEMORY_CONFIRMED, {
        scope: "user",
        scope_subject_id: ADMIN_ID,
        status: "confirmed",
        approver_ref: ADMIN_ID,
        canonical_value: "Staging resets nightly at 01:00 UTC",
      }),
    ],
    memoryConflicts: [
      {
        conflict_id: MEMORY_CONFLICT,
        kind: "contradiction",
        record_a_id: MEMORY_CANDIDATE,
        record_b_id: MEMORY_CONFIRMED,
        detected_at: "2026-09-02T08:00:00+00:00",
        resolved: false,
        resolved_by: null,
        resolved_at: null,
      },
    ],
    runs: [{ run_id: RUN_ID, status: "completed", organization_id: ORG_ID }],
    seq: { provider: 0, capVersion: 0, binding: 0, connection: 0, source: 0, sourceVersion: 0, run: 0 },
    sourcesGetCount: 0,
    offlineMode: false,
    lastBind: null,
    lastMutation: null,
  };
}

// ---------------------------------------------------------------------------
// 网络层 mock：单一 catch-all 路由内部分发；未模拟路径 500 fail loud
// ---------------------------------------------------------------------------

function installApiMocks(context: BrowserContext, state: MockState, actor: Actor): void {
  const fulfill = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  context.route("/api/**", async (route) => {
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
      return fulfill(route, 200, Object.values(ACTORS).map((a) => ({
        principal_id: a.principal,
        organization_id: ORG_ID,
        role_bindings: a.role_bindings,
      })));
    }
    if (path === `/api/v1/organizations/${ORG_ID}/workspaces` && method === "GET") {
      return fulfill(route, 200, [{ id: WS_ID, name: "Engineering" }]);
    }
    if (path === `/api/v1/workspaces/${WS_ID}/groups` && method === "GET") {
      return fulfill(route, 200, []);
    }

    // ------------------------------------------------------------------
    // Workbench runs（api/runs.py 投影）
    // ------------------------------------------------------------------
    if (path === "/api/v1/runs" && method === "GET") {
      return fulfill(route, 200, state.runs);
    }
    if (path === "/api/v1/runs" && method === "POST") {
      state.seq.run += 1;
      state.lastMutation = "POST /api/v1/runs";
      const run = {
        run_id: newSeqId(6, state.seq.run),
        status: "queued",
        organization_id: ORG_ID,
      };
      state.runs.push(run);
      return fulfill(route, 201, run);
    }
    const runMatch = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})$/);
    if (runMatch && method === "GET") {
      const run = state.runs.find((r) => r.run_id === runMatch[1]);
      if (!run) return fulfill(route, 404, { detail: "run not found" });
      return fulfill(route, 200, {
        run_id: run.run_id,
        status: run.status,
        organization_id: run.organization_id,
        tasks: {},
      });
    }
    const runEvents = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/events$/);
    if (runEvents && method === "GET") {
      return fulfill(route, 200, []);
    }
    const runApprovals = path.match(/^\/api\/v1\/runs\/([0-9a-f-]{36})\/approvals$/);
    if (runApprovals && method === "GET") {
      // 仅种子 run 携带 pending approval（approver journey 的 read 面）
      if (runApprovals[1] === RUN_ID) {
        return fulfill(route, 200, [
          {
            request_id: APPROVAL_ID,
            run_id: RUN_ID,
            task_id: "task-a",
            status: "pending",
            requester: BUILDER_ID,
          },
        ]);
      }
      return fulfill(route, 200, []);
    }

    // ------------------------------------------------------------------
    // Capabilities（api/capabilities.py 契约）
    // ------------------------------------------------------------------
    if (path === "/api/v1/capabilities/providers" && method === "GET") {
      return fulfill(route, 200, state.providers);
    }
    if (path === "/api/v1/capabilities/providers" && method === "POST") {
      state.lastMutation = "POST /api/v1/capabilities/providers";
      const body = req.postDataJSON() as {
        name: string;
        description?: string;
        source_url?: string;
        classification?: string;
        risk_level?: string;
      };
      state.seq.provider += 1;
      state.seq.capVersion += 1;
      const providerId = newSeqId(1, state.seq.provider);
      const capVersionId = newSeqId(11, state.seq.capVersion);
      const provider: ProviderRecord = {
        id: providerId,
        provider_id: newSeqId(2, state.seq.provider),
        name: body.name,
        version: 1,
        description: body.description ?? "",
        status: "discovered",
        classification: body.classification ?? "PUBLIC",
        source_url: body.source_url ?? null,
        risk_level: body.risk_level ?? "low",
        content_digest: digest(`provider-${state.seq.provider}`),
      };
      state.providers.push(provider);
      state.capVersions.push({
        id: capVersionId,
        capability_type: "provider",
        name: body.name,
        version: 1,
        status: "discovered",
        risk_level: provider.risk_level,
        content_digest: provider.content_digest,
        test_digest: null,
        parent_id: null,
        metadata: { provider_version_id: providerId },
      });
      return fulfill(route, 201, provider);
    }
    const providerMatch = path.match(/^\/api\/v1\/capabilities\/providers\/([0-9a-f-]{36})$/);
    if (providerMatch && method === "GET") {
      const provider = state.providers.find((p) => p.id === providerMatch[1]);
      if (!provider) return fulfill(route, 404, { detail: "provider not found" });
      return fulfill(route, 200, provider);
    }
    const providerAction = path.match(/^\/api\/v1\/capabilities\/providers\/([0-9a-f-]{36})\/actions$/);
    if (providerAction && method === "POST") {
      const provider = state.providers.find((p) => p.id === providerAction[1]);
      if (!provider) return fulfill(route, 404, { detail: "provider not found" });
      const body = req.postDataJSON() as { action?: string };
      state.lastMutation = `POST actions:${body.action}`;
      const transitions: Record<string, string> = {
        quarantine: "quarantined",
        inspect: "inspected",
        test: "tested",
        admit: "approved",
        publish: "published",
        suspend: "suspended",
        revoke: "revoked",
      };
      if (!body.action || !(body.action in transitions)) {
        return fulfill(route, 422, { detail: `unknown action: ${body.action ?? ""}` });
      }
      provider.status = transitions[body.action];
      // capability version status follows provider（api/capabilities.py 语义）
      for (const cv of state.capVersions) {
        if (cv.metadata.provider_version_id === provider.id) cv.status = provider.status;
      }
      return fulfill(route, 200, provider);
    }
    if (path === "/api/v1/capabilities/versions" && method === "GET") {
      return fulfill(route, 200, state.capVersions);
    }
    const versionMatch = path.match(/^\/api\/v1\/capabilities\/versions\/([0-9a-f-]{36})$/);
    if (versionMatch && method === "GET") {
      const version = state.capVersions.find((v) => v.id === versionMatch[1]);
      if (!version) return fulfill(route, 404, { detail: "capability version not found" });
      return fulfill(route, 200, version);
    }
    const diffMatch = path.match(/^\/api\/v1\/capabilities\/versions\/([0-9a-f-]{36})\/diff$/);
    if (diffMatch && method === "GET") {
      const version = state.capVersions.find((v) => v.id === diffMatch[1]);
      if (!version) return fulfill(route, 404, { detail: "capability version not found" });
      // v1 无前版本：from 0，三标志 true（api/capabilities.py 语义）
      return fulfill(route, 200, {
        from_version: 0,
        to_version: version.version,
        content_changed: true,
        risk_changed: true,
        status_changed: true,
      });
    }
    if (path === "/api/v1/capabilities/bindings" && method === "GET") {
      return fulfill(route, 200, state.bindings);
    }
    if (path === "/api/v1/capabilities/bindings" && method === "POST") {
      const body = req.postDataJSON() as {
        agent_definition_id: string;
        agent_version_id: string;
        capability_version_id: string;
      };
      state.lastBind = { body };
      const capVersion = state.capVersions.find((v) => v.id === body.capability_version_id);
      if (!capVersion) return fulfill(route, 404, { detail: "capability version not found" });
      if (capVersion.status !== "published") {
        // 机器可读拒绝面（api/capabilities.py create_binding 409 原文）
        return fulfill(route, 409, {
          detail: "can only bind published capability versions",
        });
      }
      state.seq.binding += 1;
      state.lastMutation = "POST /api/v1/capabilities/bindings";
      const binding: BindingRecord = {
        id: newSeqId(3, state.seq.binding),
        organization_id: ORG_ID,
        workspace_id: WS_ID,
        agent_definition_id: body.agent_definition_id,
        agent_version_id: body.agent_version_id,
        capability_version_id: body.capability_version_id,
        status: "active",
      };
      state.bindings.push(binding);
      return fulfill(route, 201, binding);
    }
    const bindingMatch = path.match(/^\/api\/v1\/capabilities\/bindings\/([0-9a-f-]{36})$/);
    if (bindingMatch && method === "DELETE") {
      state.lastMutation = "DELETE binding";
      const before = state.bindings.length;
      state.bindings = state.bindings.filter((b) => b.id !== bindingMatch[1]);
      if (state.bindings.length === before) {
        return fulfill(route, 404, { detail: "binding not found" });
      }
      return fulfill(route, 204, null);
    }

    // ------------------------------------------------------------------
    // Connections（api/connections.py 契约）
    // ------------------------------------------------------------------
    if (path === "/api/v1/connections" && method === "GET") {
      return fulfill(route, 200, state.connections);
    }
    if (path === "/api/v1/connections" && method === "POST") {
      const body = req.postDataJSON() as {
        provider_version_id: string;
        subject_mode?: string;
      };
      state.seq.connection += 1;
      state.lastMutation = "POST /api/v1/connections";
      const connection: ConnectionRecord = {
        id: newSeqId(4, state.seq.connection),
        organization_id: ORG_ID,
        workspace_id: WS_ID,
        provider_version_id: body.provider_version_id,
        subject_mode: body.subject_mode ?? "workspace_service",
        status: "active",
        principal_id: null,
        version: 1,
        fingerprint: digest(`connection-${state.seq.connection}`),
      };
      state.connections.push(connection);
      return fulfill(route, 201, connection);
    }
    const connMatch = path.match(/^\/api\/v1\/connections\/([0-9a-f-]{36})$/);
    if (connMatch && method === "GET") {
      const connection = state.connections.find((c) => c.id === connMatch[1]);
      if (!connection) return fulfill(route, 404, { detail: "connection not found" });
      return fulfill(route, 200, connection);
    }
    const connStatus = path.match(/^\/api\/v1\/connections\/([0-9a-f-]{36})\/status$/);
    if (connStatus && method === "GET") {
      const connection = state.connections.find((c) => c.id === connStatus[1]);
      if (!connection) return fulfill(route, 404, { detail: "connection not found" });
      return fulfill(route, 200, {
        connection_id: connection.id,
        status: connection.status,
        fingerprint: connection.fingerprint,
        credential_status: connection.status === "active" ? "active" : connection.status,
      });
    }
    const connAction = path.match(/^\/api\/v1\/connections\/([0-9a-f-]{36})\/actions$/);
    if (connAction && method === "POST") {
      const connection = state.connections.find((c) => c.id === connAction[1]);
      if (!connection) return fulfill(route, 404, { detail: "connection not found" });
      const body = req.postDataJSON() as { action?: string };
      state.lastMutation = `POST connection actions:${body.action}`;
      const transitions: Record<string, string> = { suspend: "suspended", revoke: "revoked" };
      if (!body.action || !(body.action in transitions)) {
        return fulfill(route, 422, { detail: `unknown action: ${body.action ?? ""}` });
      }
      if (connection.status === "revoked") {
        return fulfill(route, 409, { detail: "cannot act on revoked connection" });
      }
      connection.status = transitions[body.action];
      connection.version += 1;
      return fulfill(route, 200, connection);
    }

    // ------------------------------------------------------------------
    // Knowledge（api/knowledge.py 契约）。member 的 list 被 PEP 拒——403 形状
    // 对齐真实策略拒绝（server-driven，前端不硬判）。
    // ------------------------------------------------------------------
    if (path === "/api/v1/knowledge/sources" && method === "GET") {
      state.sourcesGetCount += 1;
      if (state.offlineMode) {
        // offline 模拟：连接层失败（abort ≠ HTTP 错误，fetch 直接抛错）
        return route.abort();
      }
      if (actor === "member") {
        return fulfill(route, 403, { detail: "policy denied" });
      }
      return fulfill(route, 200, state.knowledgeSources);
    }
    if (path === "/api/v1/knowledge/sources" && method === "POST") {
      const body = req.postDataJSON() as {
        source_type: string;
        connector: string;
        uri: string;
        classification?: string;
      };
      state.seq.source += 1;
      state.lastMutation = "POST /api/v1/knowledge/sources";
      const source: KnowledgeSourceRecord = {
        id: newSeqId(5, state.seq.source),
        source_type: body.source_type,
        connector: "",
        uri: "",
        classification: body.classification ?? "PUBLIC",
        status: "active",
        version_count: 0,
        latest_version_seq: null,
        latest_content_digest: null,
        acl_allowed_principals: [],
        acl_denied_principals: [],
        acl_allowed_groups: [],
      };
      state.knowledgeSources.push(source);
      state.knowledgeVersions[source.id] = [];
      return fulfill(route, 201, source);
    }
    const connectMatch = path.match(/^\/api\/v1\/knowledge\/sources\/([0-9a-f-]{36})\/connect$/);
    if (connectMatch && method === "POST") {
      const source = state.knowledgeSources.find((s) => s.id === connectMatch[1]);
      if (!source) return fulfill(route, 404, { detail: "source not found" });
      source.status = "active";
      return fulfill(route, 200, source);
    }
    const syncMatch = path.match(/^\/api\/v1\/knowledge\/sources\/([0-9a-f-]{36})\/sync$/);
    if (syncMatch && method === "POST") {
      const source = state.knowledgeSources.find((s) => s.id === syncMatch[1]);
      if (!source) return fulfill(route, 404, { detail: "source not found" });
      if (source.status === "disabled") {
        return fulfill(route, 403, { detail: "source is disabled" });
      }
      state.seq.sourceVersion += 1;
      const versions = state.knowledgeVersions[source.id] ?? [];
      const stale = versions.filter((v) => v.state === "active").length;
      for (const v of versions) v.state = "stale";
      const version: KnowledgeVersionRow = {
        id: newSeqId(7, state.seq.sourceVersion),
        source_object_id: source.id,
        version_seq: versions.length + 1,
        connector: source.source_type,
        uri: `source://${source.id}`,
        content_digest: digest(`source-${source.id}-${versions.length + 1}`),
        state: "active",
        classification: source.classification,
        observed_at: "2026-09-06T09:00:00+00:00",
        valid_at: "2026-09-06T09:00:00+00:00",
        connector_version: "1.0.0",
        parser_version: "1.0.0",
        index_version: "1.0.0",
      };
      versions.push(version);
      state.knowledgeVersions[source.id] = versions;
      source.version_count = versions.length;
      source.latest_version_seq = version.version_seq;
      source.latest_content_digest = version.content_digest;
      source.connector = version.connector;
      source.uri = version.uri;
      state.lastMutation = "POST sync";
      return fulfill(route, 200, {
        source_id: source.id,
        sync_status: "completed",
        versions_created: 1,
        versions_marked_stale: stale,
        connector: source.source_type,
        sync_watermark: version.id,
      });
    }
    const statusMatch = path.match(/^\/api\/v1\/knowledge\/sources\/([0-9a-f-]{36})\/status$/);
    if (statusMatch && method === "GET") {
      const source = state.knowledgeSources.find((s) => s.id === statusMatch[1]);
      if (!source) return fulfill(route, 404, { detail: "source not found" });
      const versions = state.knowledgeVersions[source.id] ?? [];
      const latest = versions[versions.length - 1] ?? null;
      // 语义对齐 api/knowledge.py source_status：无版本 → expired/no_version；
      // ACL 空 → unknown；命中 allowed → allowed（score 同步翻转）
      if (!latest) {
        return fulfill(route, 200, {
          source_id: source.id,
          status: source.status,
          version_seq: null,
          content_digest: null,
          locator_connector: null,
          locator_uri: null,
          freshness_state: "expired",
          acl_allowed: false,
          acl_reason: "no_version",
          classification: source.classification,
          score_breakdown: {},
        });
      }
      const principal = ACTORS[actor].principal;
      const allowed =
        source.acl_allowed_principals.length === 0 &&
        source.acl_allowed_groups.length === 0
          ? false
          : source.acl_allowed_principals.includes(principal);
      const aclReason = source.acl_denied_principals.includes(principal)
        ? "denied_principal"
        : source.acl_allowed_principals.includes(principal)
          ? "allowed"
          : source.acl_allowed_principals.length === 0 && source.acl_allowed_groups.length === 0
            ? "unknown"
            : "not_in_acl";
      return fulfill(route, 200, {
        source_id: source.id,
        status: source.status,
        version_seq: latest.version_seq,
        content_digest: latest.content_digest,
        locator_connector: latest.connector,
        locator_uri: latest.uri,
        freshness_state: "aging",
        acl_allowed: allowed,
        acl_reason: aclReason,
        classification: source.classification,
        score_breakdown: { acl_score: allowed ? 1 : 0, freshness_score: 0.7 },
      });
    }
    const kVersions = path.match(/^\/api\/v1\/knowledge\/sources\/([0-9a-f-]{36})\/versions$/);
    if (kVersions && method === "GET") {
      const source = state.knowledgeSources.find((s) => s.id === kVersions[1]);
      if (!source) return fulfill(route, 404, { detail: "source not found" });
      return fulfill(route, 200, state.knowledgeVersions[source.id] ?? []);
    }
    const aclMatch = path.match(/^\/api\/v1\/knowledge\/sources\/([0-9a-f-]{36})\/acl$/);
    if (aclMatch && method === "PUT") {
      const source = state.knowledgeSources.find((s) => s.id === aclMatch[1]);
      if (!source) return fulfill(route, 404, { detail: "source not found" });
      const body = req.postDataJSON() as {
        allowed_principals?: string[];
        denied_principals?: string[];
        allowed_groups?: string[];
      };
      source.acl_allowed_principals = body.allowed_principals ?? [];
      source.acl_denied_principals = body.denied_principals ?? [];
      source.acl_allowed_groups = body.allowed_groups ?? [];
      state.lastMutation = "PUT acl";
      return fulfill(route, 200, source);
    }
    const disableMatch = path.match(/^\/api\/v1\/knowledge\/sources\/([0-9a-f-]{36})\/disable$/);
    if (disableMatch && method === "POST") {
      const source = state.knowledgeSources.find((s) => s.id === disableMatch[1]);
      if (!source) return fulfill(route, 404, { detail: "source not found" });
      source.status = "disabled";
      state.lastMutation = "POST disable";
      return fulfill(route, 200, source);
    }

    // ------------------------------------------------------------------
    // Memory（api/memory.py 契约）
    // ------------------------------------------------------------------
    if (path === "/api/v1/memory/records" && method === "GET") {
      const params = new URL(req.url()).searchParams;
      const records = state.memoryRecords.filter((r) => {
        if (r.scope === "user" && r.scope_subject_id !== ACTORS[actor].principal) return false;
        if (params.get("scope") && r.scope !== params.get("scope")) return false;
        if (params.get("type") && r.type !== params.get("type")) return false;
        if (params.get("status") && r.status !== params.get("status")) return false;
        if (params.get("source") && !r.source_refs.some((sr) => sr.source_type === params.get("source"))) return false;
        return true;
      });
      return fulfill(route, 200, records);
    }
    const recordMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})$/);
    if (recordMatch && method === "GET") {
      const record = state.memoryRecords.find((r) => r.id === recordMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      return fulfill(route, 200, record);
    }
    const confirmMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/confirm$/);
    if (confirmMatch && method === "POST") {
      const record = state.memoryRecords.find((r) => r.id === confirmMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      state.lastMutation = "POST confirm";
      if (record.scope === "team" && !ACTORS[actor].role_bindings.includes("memory_steward")) {
        return fulfill(route, 403, { detail: "only Memory Steward can confirm team records" });
      }
      if (record.status !== "candidate") {
        return fulfill(route, 409, {
          detail: `record status is ${record.status}, expected candidate`,
        });
      }
      record.status = "confirmed";
      record.approver_ref = ACTORS[actor].principal;
      record.updated_at = "2026-09-06T10:00:00+00:00";
      return fulfill(route, 200, record);
    }
    const correctMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/correct$/);
    if (correctMatch && method === "POST") {
      const original = state.memoryRecords.find((r) => r.id === correctMatch[1]);
      if (!original) return fulfill(route, 404, { detail: "memory record not found" });
      const body = req.postDataJSON() as { canonical_value: string; subject?: string };
      state.lastMutation = "POST correct";
      original.status = "superseded";
      original.updated_at = "2026-09-06T10:00:00+00:00";
      const corrected: MemoryRecord = {
        ...original,
        id: newSeqId(8, state.memoryRecords.length + 1),
        version: original.version + 1,
        canonical_value: body.canonical_value,
        subject: body.subject ?? original.subject,
        status: "confirmed",
        approver_ref: ACTORS[actor].principal,
        created_at: "2026-09-06T10:00:00+00:00",
        updated_at: "2026-09-06T10:00:00+00:00",
      };
      state.memoryRecords.push(corrected);
      return fulfill(route, 200, corrected);
    }
    const revokeMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/revoke$/);
    if (revokeMatch && method === "POST") {
      const record = state.memoryRecords.find((r) => r.id === revokeMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      state.lastMutation = "POST revoke";
      record.status = "revoked";
      record.tombstone = true;
      record.updated_at = "2026-09-06T10:00:00+00:00";
      return fulfill(route, 200, record);
    }
    const deleteMatch = path.match(/^\/api\/v1\/memory\/records\/([0-9a-f-]{36})\/delete$/);
    if (deleteMatch && method === "POST") {
      const record = state.memoryRecords.find((r) => r.id === deleteMatch[1]);
      if (!record) return fulfill(route, 404, { detail: "memory record not found" });
      state.lastMutation = "POST delete";
      record.status = "revoked";
      record.tombstone = true;
      record.updated_at = "2026-09-06T10:00:00+00:00";
      return fulfill(route, 204, null);
    }
    if (path === "/api/v1/memory/conflicts" && method === "GET") {
      return fulfill(route, 200, state.memoryConflicts.filter((c) => !c.resolved));
    }
    if (path === "/api/v1/memory/conflicts/resolve" && method === "POST") {
      const body = req.postDataJSON() as { conflict_id: string };
      const conflict = state.memoryConflicts.find(
        (c) => c.conflict_id === body.conflict_id && !c.resolved
      );
      if (!conflict) {
        return fulfill(route, 404, { detail: "conflict not found or already resolved" });
      }
      conflict.resolved = true;
      conflict.resolved_by = ACTORS[actor].principal;
      conflict.resolved_at = "2026-09-06T10:00:00+00:00";
      state.lastMutation = "POST resolve";
      return fulfill(route, 200, conflict);
    }
    if (path === "/api/v1/memory/export" && method === "POST") {
      state.lastMutation = "POST export";
      const visible = state.memoryRecords.filter(
        (r) => r.scope !== "user" || r.scope_subject_id === ACTORS[actor].principal
      );
      return fulfill(route, 200, { records: visible, count: visible.length });
    }
    if (path === "/api/v1/memory/stats" && method === "GET") {
      const visible = state.memoryRecords.filter(
        (r) => r.scope !== "user" || r.scope_subject_id === ACTORS[actor].principal
      );
      const byStatus: Record<string, number> = {};
      for (const r of visible) byStatus[r.status] = (byStatus[r.status] ?? 0) + 1;
      return fulfill(route, 200, {
        total_records: visible.length,
        by_status: byStatus,
        by_scope: { team: visible.filter((r) => r.scope === "team").length },
        by_type: { fact: visible.filter((r) => r.type === "fact").length },
        unresolved_conflicts: state.memoryConflicts.filter((c) => !c.resolved).length,
      });
    }

    // ------------------------------------------------------------------
    // Admin 分区复用面（observability costs；audit log 无端点如实空态）
    // ------------------------------------------------------------------
    if (path === "/api/v1/observability/costs" && method === "GET") {
      return fulfill(route, 200, {
        reservations: [
          {
            reservation_id: "9e8d7c6b-5a4f-4e3d-8b2a-1c0d9e8f7a11",
            run_id: RUN_ID,
            amount_usd: "0.0123456",
            price_source: "price-card-2026-09",
            price_confidence: "exact",
            created_at: "2026-09-01T12:00:00+00:00",
          },
        ],
        reconciliations: [
          {
            reservation_id: "9e8d7c6b-5a4f-4e3d-8b2a-1c0d9e8f7a11",
            reserved_usd: "0.0123456",
            actual_usd: "0.0123456",
            variance_usd: "0.0000000",
            retry_cost_usd: "0.0000000",
            child_run_cost_usd: "0.0000000",
            tool_external_cost_usd: "0.0000000",
            created_at: "2026-09-01T13:00:00+00:00",
          },
        ],
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

async function openSection(page: Page, section: string): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeVisible();
  await page.getByRole("button", { name: section, exact: true }).click();
  await expect(page.getByRole("heading", { name: section, exact: true })).toBeVisible();
}

// ---------------------------------------------------------------------------
// (1) Admin journey：governance home（members/policy/audit/cost health）
// ---------------------------------------------------------------------------

test.describe("Admin governance home", () => {
  test("admin sees members/policy, audit log and cost health in one section", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "admin");
    const page = await context.newPage();

    await openSection(page, "Admin");

    // members + policy surface（MembersPanel：org 角色绑定即 policy 面）。
    // OrganizationsPanel 常驻渲染同型面板——断言锚定 Admin region 内的实例。
    const adminRegion = page.getByRole("region", { name: "Admin" });
    await expect(
      adminRegion.getByRole("heading", { name: "Members", exact: true })
    ).toBeVisible();
    await expect(adminRegion.getByText(PUBLISHER_ID)).toBeVisible();
    await expect(adminRegion.getByText(BUILDER_ID)).toBeVisible();

    // audit log（无端点 → 如实空态，不造假事件）
    await expect(adminRegion.getByRole("heading", { name: "Audit log" })).toBeVisible();
    await expect(adminRegion.getByText("No audit events")).toBeVisible();

    // cost health（CostsView 复用，非复制）
    await expect(adminRegion.getByRole("heading", { name: "Costs" })).toBeVisible();
    await expect(adminRegion.getByText("price-card-2026-09")).toBeVisible();
    await expect(adminRegion.getByText("exact")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (2) Publisher journey：import → inspect → admit/publish → bind → suspend
//     + connection create/status/suspend；409 机器可读拒绝面（未发布版本绑定）
// ---------------------------------------------------------------------------

test.describe("Publisher journey", () => {
  test("imports, inspects, publishes, binds, suspends capabilities and manages connections", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "publisher");
    const page = await context.newPage();

    await openSection(page, "Capabilities");

    // import（POST providers → 201）
    await page.getByRole("button", { name: "Import provider" }).click();
    await page.getByLabel("Provider name").fill("github-mcp");
    await page.getByLabel("Source URL").fill("https://registry.example/github-mcp");
    await page.getByRole("button", { name: "Register provider" }).click();
    const providerId = newSeqId(1, 1);
    const capVersionA = newSeqId(11, 1);
    const providerRow = page.getByRole("table", { name: "Providers" }).getByRole("row", { name: new RegExp(providerId) });
    await expect(providerRow).toContainText("github-mcp");
    await expect(providerRow).toContainText("discovered");

    // inspect：检视元数据逐字（classification/risk/digest/source url verbatim）
    await providerRow.getByRole("button", { name: "Open" }).click();
    await expect(page.getByRole("heading", { name: "Provider" })).toBeVisible();
    await expect(page.getByText("classification: PUBLIC")).toBeVisible();
    await expect(page.getByText("risk level: low")).toBeVisible();
    await expect(page.getByText(/content digest: sha256:/)).toBeVisible();
    await expect(page.getByText("source url: https://registry.example/github-mcp")).toBeVisible();

    // admit → publish（能力版本状态跟随 provider）
    await page.getByRole("button", { name: "Admit" }).click();
    await expect(page.getByText("status: approved")).toBeVisible();
    await page.getByRole("button", { name: "Publish" }).click();
    await expect(page.getByText("status: published")).toBeVisible();

    // version inspect + diff（从 versions 表进入）
    await page.getByRole("button", { name: "Back" }).click();
    await page
      .getByRole("table", { name: "Capability versions" })
      .getByRole("row", { name: new RegExp(capVersionA) })
      .getByRole("button", { name: "Inspect" })
      .click();
    await expect(page.getByRole("heading", { name: "Capability version" })).toBeVisible();
    await expect(page.getByText("capability type: provider")).toBeVisible();
    await expect(page.getByText("test digest: unknown")).toBeVisible();
    await page.getByRole("button", { name: "Show diff" }).click();
    await expect(page.getByText("diff: v0 → v1")).toBeVisible();
    await expect(page.getByText("content changed: true")).toBeVisible();
    await expect(page.getByText("risk changed: true")).toBeVisible();
    await expect(page.getByText("status changed: true")).toBeVisible();
    // 无 SBOM/漏洞数据即不渲染（清单纪律：不发明检查项）
    await expect(page.getByText(/sbom/i)).toHaveCount(0);
    await expect(page.getByText(/vulnerability/i)).toHaveCount(0);
    await page.getByRole("button", { name: "Back" }).click();

    // bind published 版本 → 201
    await page.getByRole("button", { name: "Bind capability" }).click();
    await page.getByLabel("Capability version id").fill(capVersionA);
    await page.getByLabel("Agent definition id").fill(newSeqId(9, 1));
    await page.getByLabel("Agent version id").fill(newSeqId(10, 1));
    await page.getByRole("button", { name: "Create binding" }).click();
    await expect(
      page.getByRole("table", { name: "Bindings" }).getByRole("row", { name: new RegExp(capVersionA) })
    ).toBeVisible();
    expect(state.lastBind).not.toBeNull();
    expect(state.lastBind!.body.capability_version_id).toBe(capVersionA);

    // bind 未发布版本 → 409 机器可读拒绝面（S4 契约：未发布绑定被拒）
    const capVersionB = newSeqId(11, 2);
    await page.getByRole("button", { name: "Import provider" }).click();
    await page.getByLabel("Provider name").fill("jira-mcp");
    await page.getByRole("button", { name: "Register provider" }).click();
    await page.getByRole("button", { name: "Bind capability" }).click();
    await page.getByLabel("Capability version id").fill(capVersionB);
    await page.getByLabel("Agent definition id").fill(newSeqId(9, 2));
    await page.getByLabel("Agent version id").fill(newSeqId(10, 2));
    await page.getByRole("button", { name: "Create binding" }).click();
    await expect(page.getByText("can only bind published capability versions")).toBeVisible();

    // suspend（security-admin 门禁）→ provider + 能力版本同时 suspended
    await page
      .getByRole("table", { name: "Providers" })
      .getByRole("row", { name: new RegExp(providerId) })
      .getByRole("button", { name: "Open" })
      .click();
    await page.getByRole("button", { name: "Suspend" }).click();
    await expect(page.getByText("status: suspended")).toBeVisible();

    // connections：create → status → suspend → revoke
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Create connection" }).click();
    await page.getByLabel("Provider version id").fill(providerId);
    await page.getByRole("button", { name: "Confirm connection" }).click();
    const connId = newSeqId(4, 1);
    const connRow = page.getByRole("table", { name: "Connections" }).getByRole("row", { name: new RegExp(connId) });
    await expect(connRow).toContainText("workspace_service");
    await connRow.getByRole("button", { name: "Status" }).click();
    await expect(page.getByText(`fingerprint: ${digest("connection-1")}`)).toBeVisible();
    await expect(page.getByText("credential: active")).toBeVisible();
    await connRow.getByRole("button", { name: "Suspend" }).click();
    await expect(connRow).toContainText("suspended");
    await connRow.getByRole("button", { name: "Revoke" }).click();
    await expect(connRow).toContainText("revoked");

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (3) Builder journey：knowledge connect/sync/status/acl/disable + bind capability；
//     stale（focus refetch）与 offline reconnect（abort→Retry）代表性断言
// ---------------------------------------------------------------------------

test.describe("Builder journey", () => {
  test("connects, syncs, inspects status, saves ACL, disables source; binds published capability", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();

    await openSection(page, "Knowledge");

    // empty 态 → add source
    await expect(page.getByText("No sources")).toBeVisible();
    await page.getByRole("button", { name: "Add source" }).click();
    await page.getByLabel("Source type").fill("web");
    await page.getByLabel("Connector").fill("web-crawler");
    await page.getByLabel("URI").fill("https://docs.example/runbook");
    await page.getByRole("button", { name: "Save source" }).click();
    const sourceId = newSeqId(5, 1);
    const sourceRow = page.getByRole("table", { name: "Sources" }).getByRole("row", { name: new RegExp(sourceId) });
    await expect(sourceRow).toContainText("active");

    // connect（幂等置 active）+ sync（真实 SyncResultRecord 投影；locator
    // connector=source_type，uri=source://{id}，对齐 api/knowledge.py）
    await sourceRow.getByRole("button", { name: "Connect" }).click();
    await sourceRow.getByRole("button", { name: "Sync" }).click();
    await expect(page.getByText("sync: completed")).toBeVisible();
    await expect(page.getByText("versions created: 1")).toBeVisible();
    await expect(sourceRow).toContainText(`source://${sourceId}`);

    // status：真实 SourceStatusRecord 投影（freshness/acl/score verbatim）
    await sourceRow.getByRole("button", { name: "Status" }).click();
    await expect(page.getByText("freshness: aging")).toBeVisible();
    await expect(page.getByText("acl reason: unknown")).toBeVisible();
    await expect(page.getByText("classification: PUBLIC")).toBeVisible();
    await expect(page.getByText("version: 1")).toBeVisible();

    // versions：真实 SourceVersionRecord 投影（版本行携带 source ref + digest）
    await sourceRow.getByRole("button", { name: "Versions" }).click();
    await expect(
      page
        .getByRole("table", { name: "Source versions" })
        .getByRole("row", { name: new RegExp(sourceId) })
    ).toBeVisible();

    // ACL 保存 → status 投影随之翻转（allowed/acl_score）
    await page.getByRole("button", { name: "ACL" }).click();
    await page.getByLabel("Allowed principals").fill(BUILDER_ID);
    await page.getByRole("button", { name: "Save ACL" }).click();
    await expect(page.getByText("acl allowed: true")).toBeVisible();
    await expect(page.getByText("acl reason: allowed")).toBeVisible();

    // stale：窗口 focus 后分区 refetch（GET sources 计数 +1）
    const getsBefore = state.sourcesGetCount;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect.poll(() => state.sourcesGetCount).toBeGreaterThan(getsBefore);

    // disable：confirm-gated
    await sourceRow.getByRole("button", { name: "Disable" }).click();
    await sourceRow.getByRole("button", { name: "Confirm disable" }).click();
    await expect(sourceRow).toContainText("disabled");

    // builder 绑定 published 能力；无 lifecycle/suspend 控件（角色显隐）。
    // 版本种子必须先于分区挂载——CapabilitiesView 挂载时拉取版本列表。
    const capVersionA = newSeqId(11, 1);
    state.capVersions.push({
      id: capVersionA,
      capability_type: "provider",
      name: "github-mcp",
      version: 1,
      status: "published",
      risk_level: "low",
      content_digest: digest("provider"),
      test_digest: null,
      parent_id: null,
      metadata: { provider_version_id: newSeqId(1, 1) },
    });
    await openSection(page, "Capabilities");
    await expect(page.getByRole("button", { name: "Import provider" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Suspend" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Revoke" })).toHaveCount(0);
    await page.getByRole("button", { name: "Bind capability" }).click();
    await page.getByLabel("Capability version id").fill(capVersionA);
    await page.getByLabel("Agent definition id").fill(newSeqId(9, 1));
    await page.getByLabel("Agent version id").fill(newSeqId(10, 1));
    await page.getByRole("button", { name: "Create binding" }).click();
    await expect(
      page.getByRole("table", { name: "Bindings" }).getByRole("row", { name: new RegExp(capVersionA) })
    ).toBeVisible();

    await context.close();
  });

  test("offline reconnect: aborted load surfaces error, Retry recovers after reconnect", async ({ browser }) => {
    const state = newState();
    // offline 起始：全部 sources GET 走连接层失败（focus refetch 也失败——
    // offline 期间不误报恢复）
    state.offlineMode = true;
    const context = await newContextWithMocks(browser, state, "builder");
    const page = await context.newPage();

    await page.goto("/");
    await page.getByRole("button", { name: "Knowledge", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText(/failed|fetch/i);

    // reconnect：恢复连接后 Retry 重放同一 GET（offline 恢复路径）
    state.offlineMode = false;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByText("No sources")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (4) Member journey：workbench run + 分区入口存在；knowledge 403 结构化错误面
// ---------------------------------------------------------------------------

test.describe("Member journey", () => {
  test("runs workbench template, sees section entries, knowledge list refuses with 403", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "member");
    const page = await context.newPage();

    // workbench run（Member 的 build/use 面只读消费）。创建后 Workbench 自动
    // 下钻 run 详情——断言详情后返回列表。
    await page.goto("/");
    await page.getByLabel("Template").selectOption("approval-chain");
    await page.getByRole("button", { name: "New run" }).click();
    await expect(page.getByText(/Status: queued/)).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(page.getByRole("row", { name: /queued/ })).toBeVisible();

    // ask/discover 的 renderer journey 归 T4b spec——这里只断言分区入口存在
    await expect(page.getByRole("button", { name: "Knowledge", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Capabilities", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Memory", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Admin", exact: true })).toBeVisible();

    // 403 面：PEP 拒绝 → 结构化错误（非空白页、非静默）
    await page.getByRole("button", { name: "Knowledge", exact: true }).click();
    await expect(page.getByText("Not authorized (403)")).toBeVisible();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (5) Approver + Auditor journey：approvals read + audit + read-only everywhere
// ---------------------------------------------------------------------------

test.describe("Approver and auditor journey", () => {
  test("approver reads approval queue; auditor sees audit and zero enabled mutations", async ({ browser }) => {
    const state = newState();
    state.knowledgeSources.push({
      id: newSeqId(5, 1),
      source_type: "web",
      connector: "web-crawler",
      uri: "source://seed",
      classification: "PUBLIC",
      status: "active",
      version_count: 1,
      latest_version_seq: 1,
      latest_content_digest: digest("seed"),
      acl_allowed_principals: [],
      acl_denied_principals: [],
      acl_allowed_groups: [],
    });

    // approver：approval queue read（dual control 只读路径）
    const approverContext = await newContextWithMocks(browser, state, "approver");
    const approverPage = await approverContext.newPage();
    await approverPage.goto("/");
    await approverPage
      .getByRole("row", { name: new RegExp(RUN_ID) })
      .getByRole("button", { name: "Open" })
      .click();
    await expect(approverPage.getByRole("heading", { name: "Run" })).toBeVisible();
    await expect(approverPage.getByText(`task task-a (pending, requester ${BUILDER_ID})`)).toBeVisible();
    await approverContext.close();

    // auditor：read-only everywhere + audit 面
    const auditorContext = await newContextWithMocks(browser, state, "auditor");
    const page = await auditorContext.newPage();

    await openSection(page, "Knowledge");
    const sourceRow = page
      .getByRole("table", { name: "Sources" })
      .getByRole("row", { name: new RegExp(newSeqId(5, 1)) });
    await expect(sourceRow).toBeVisible();
    await expect(sourceRow.getByRole("button", { name: "Sync" })).toBeDisabled();
    await expect(sourceRow.getByRole("button", { name: "Confirm disable" })).toHaveCount(0);

    await openSection(page, "Capabilities");
    await expect(page.getByRole("button", { name: "Import provider" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Suspend" })).toHaveCount(0);

    await openSection(page, "Memory");
    // confirm 入口对无 steward 角色隐藏（server-driven 显隐契约）
    await expect(page.getByRole("button", { name: "Confirm", exact: true })).toHaveCount(0);

    await openSection(page, "Admin");
    const adminRegion = page.getByRole("region", { name: "Admin" });
    // AuditLogPanel 在 WorkspacesPanel（auditor 可见）与 Admin 分区各有一处——
    // 断言锚定 Admin region 内的实例
    await expect(adminRegion.getByText("No audit events")).toBeVisible();

    // 零 mutation 请求（auditor 会话不发写路径）
    expect(state.lastMutation).toBeNull();

    await auditorContext.close();
  });
});

// ---------------------------------------------------------------------------
// (6) Memory journey：candidate/confirmed 词汇 verbatim、steward confirm（409
//     冲突面）、revoke/correct/delete（tombstone/cascade 投影）、conflicts、
//     export、stats
// ---------------------------------------------------------------------------

test.describe("Memory journey", () => {
  test("confirms with steward gating, surfaces 409 conflict, revokes, corrects, deletes, resolves, exports, stats", async ({ browser }) => {
    const state = newState();
    const context = await newContextWithMocks(browser, state, "admin");
    const page = await context.newPage();

    await openSection(page, "Memory");

    // 状态词汇 verbatim（candidate/confirmed 对齐 DATA_MODEL）
    const recordsTable = page.getByRole("table", { name: "Records" });
    await expect(
      recordsTable.getByRole("row", { name: new RegExp(MEMORY_CANDIDATE) })
    ).toContainText("candidate");
    await expect(
      recordsTable.getByRole("row", { name: new RegExp(MEMORY_CONFIRMED) })
    ).toContainText("confirmed");

    // filter：status=candidate → 只剩 candidate 行（server 过滤）
    await page.getByLabel("Status").selectOption("candidate");
    await expect(recordsTable.getByRole("row", { name: new RegExp(MEMORY_CONFIRMED) })).toHaveCount(0);
    await page.getByLabel("Status").selectOption("");

    // steward confirm → confirmed；重复 confirm → 409 机器可读冲突面
    const candidateRow = recordsTable.getByRole("row", { name: new RegExp(MEMORY_CANDIDATE) });
    await candidateRow.getByRole("button", { name: "Open" }).click();
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page.getByText("status: confirmed")).toBeVisible();
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page.getByText("record status is confirmed, expected candidate")).toBeVisible();

    // revoke → revoked + tombstone true（S7 状态投影）
    await page.getByRole("button", { name: "Revoke" }).click();
    await page.getByRole("button", { name: "Confirm revoke" }).click();
    await expect(page.getByText("status: revoked")).toBeVisible();
    await expect(page.getByText("tombstone: true")).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();

    // correct → 原 record superseded + 新 confirmed 行
    const confirmedRow = recordsTable.getByRole("row", { name: new RegExp(MEMORY_CONFIRMED) });
    await confirmedRow.getByRole("button", { name: "Open" }).click();
    await page.getByLabel("Corrected value").fill("Staging resets nightly at 00:30 UTC");
    await page.getByRole("button", { name: "Submit correction" }).click();
    await expect(page.getByText("status: confirmed")).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();
    await expect(
      recordsTable.getByRole("row", { name: new RegExp(MEMORY_CONFIRMED) })
    ).toContainText("superseded");

    // delete → 204 后按 refetch 的记录呈现 cascade 边界（revoked + tombstone）
    const supersededRow = recordsTable.getByRole("row", { name: new RegExp(MEMORY_CONFIRMED) });
    await supersededRow.getByRole("button", { name: "Open" }).click();
    await page.getByRole("button", { name: "Delete" }).click();
    await page.getByRole("button", { name: "Confirm delete" }).click();
    await expect(page.getByText("status: revoked")).toBeVisible();
    await expect(page.getByText("tombstone: true")).toBeVisible();
    await page.getByRole("button", { name: "Back" }).click();

    // conflicts：未解决冲突 → resolve（冲突消失）
    await page.getByRole("button", { name: "Conflicts" }).click();
    const conflictsTable = page.getByRole("table", { name: "Conflicts" });
    await expect(conflictsTable.getByRole("row", { name: new RegExp(MEMORY_CONFLICT) })).toContainText("contradiction");
    await conflictsTable.getByRole("row", { name: new RegExp(MEMORY_CONFLICT) }).getByRole("button", { name: "Resolve" }).click();
    await expect(page.getByText("No unresolved conflicts")).toBeVisible();

    // export：真实 ExportResponse 投影
    await page.getByRole("button", { name: "Export" }).click();
    await expect(page.getByText("exported records: 3")).toBeVisible();

    // stats：真实 MemoryStatsResponse 投影
    await page.getByRole("button", { name: "Stats" }).click();
    await expect(page.getByText("total records: 3")).toBeVisible();
    await expect(page.getByText("unresolved conflicts: 0")).toBeVisible();

    // mutation PEP 契约：confirm 携带 CSRF + Idempotency-Key（api.ts 语义在
    // eval-release-observability.spec.ts 已冻结，这里抽查 memory 面）
    expect(state.lastMutation).not.toBeNull();

    await context.close();
  });
});

// ---------------------------------------------------------------------------
// (7) route→API contract coverage 清单断言（specs/s10 §6「no dead page/action」）
// ---------------------------------------------------------------------------

test.describe("route coverage inventory", () => {
  test("inventory covers every mounted endpoint of the four product routers and invents nothing", () => {
    const uncovered = uncoveredBackendEndpoints();
    expect(
      uncovered.map((e) => `${e.method} ${e.path}`),
      "every mounted endpoint must have a UI control or an explicit no-ui entry"
    ).toEqual([]);
    expect(
      inventedApiCalls().map((m) => `${m.view}: ${m.control}`),
      "ui mappings must not reference endpoints that no router mounts"
    ).toEqual([]);
    for (const entry of INVENTORY.noUiEndpoints) {
      expect(
        entry.note.length,
        `no-ui endpoint ${entry.endpoint.method} ${entry.endpoint.path} must carry an honest note`
      ).toBeGreaterThan(0);
    }
  });
});
