// S1-T6 same-origin API client + S10-T1 CAS/ETag 扩展 + S10-T2 Studio 面。
//
// 本文件是 client 的规范实现本体（S10-T2 归位：实现自 lib/api.ts 迁回 prescribed
// layout src/api/；lib/api.ts 是 re-export shim。此前本体留在 lib/ 是因为既有
// mock e2e 的 `**/api/**` 拦截 glob 会把 /src/api/* 模块脚本截成 JSON——S10-T2
// 已把既有 spec 的 glob 根锚定为 "/api/**"）。
//
// 语义：
// - cookie-based session + CSRF + tenant context；同源请求 credentials include
// - mutation（POST/PUT/PATCH/DELETE）必须带非空 Idempotency-Key（server PEP 要求）
// - tenant context（X-ZhiWei-Organization / X-ZhiWei-Workspace）由 session 解析后全局注入
// - 401 → SessionExpiredError（重定向登录）；403 透传（server-driven，前端不硬判）
// - PUT 的 If-Match 经 headers 显式携带；412/428 类型化上浮为
//   CasConflictError / PreconditionRequiredError（继承 ApiError，既有 catch 不破）
// - getWithETag / putWithETag：CAS 前置「先读后写」——ETag 在响应头不在体内

export class SessionExpiredError extends Error {
  constructor() {
    super("session expired");
    this.name = "SessionExpiredError";
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string
  ) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

export class CasConflictError extends ApiError {
  constructor(detail: string) {
    super(412, detail);
    this.name = "CasConflictError";
  }
}

export class PreconditionRequiredError extends ApiError {
  constructor(detail: string) {
    super(428, detail);
    this.name = "PreconditionRequiredError";
  }
}

let _csrfToken: string | null = null;
let _tenantHeaders: Record<string, string> = {};

export function setSessionMeta(csrfToken: string | null, tenantHeaders: Record<string, string>) {
  _csrfToken = csrfToken;
  _tenantHeaders = tenantHeaders;
}

export function getCsrfToken() {
  return _csrfToken;
}

export function generateIdempotencyKey() {
  return crypto.randomUUID();
}

// 发送请求并施加跨切面错误语义（401）；响应体解析独立成步，供 getWithETag
// 读取头字段（ETag 不在响应体内）。
async function send(
  method: string,
  path: string,
  body?: unknown,
  extraHeaders?: Record<string, string>
): Promise<Response> {
  const headers: Record<string, string> = { ..._tenantHeaders, ...extraHeaders };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (method !== "GET") {
    headers["X-CSRF-Token"] = _csrfToken ?? "";
    headers["Idempotency-Key"] = generateIdempotencyKey();
  }
  const resp = await fetch(path, {
    method,
    headers,
    credentials: "include",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) {
    throw new SessionExpiredError();
  }
  return resp;
}

async function parseBody<T>(resp: Response): Promise<T> {
  if (resp.status === 204) {
    return undefined as T;
  }
  const text = await resp.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (resp.status >= 400) {
    const rawDetail =
      typeof payload === "object" && payload && "detail" in payload
        ? (payload as { detail: unknown }).detail
        : (payload ?? resp.statusText);
    // S9 拒绝面（releases/claims）的 detail 是结构化对象 {reason, message}，
    // 契约要求前端按 reason 分支——序列化保留 JSON 文本，不退化成 "[object Object]"。
    const detail = typeof rawDetail === "string" ? rawDetail : JSON.stringify(rawDetail);
    if (resp.status === 412) {
      throw new CasConflictError(detail);
    }
    if (resp.status === 428) {
      throw new PreconditionRequiredError(detail);
    }
    throw new ApiError(resp.status, detail);
  }
  return payload as T;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  extraHeaders?: Record<string, string>
): Promise<T> {
  const resp = await send(method, path, body, extraHeaders);
  return parseBody<T>(resp);
}

export interface Etagged<T> {
  data: T;
  etag: string | null;
}

// CAS 前置读取：ETag 在响应头，request() 的体解析不覆盖——单独暴露。
export async function getWithETag<T>(path: string): Promise<Etagged<T>> {
  const resp = await send("GET", path);
  return { data: await parseBody<T>(resp), etag: resp.headers.get("ETag") };
}

// CAS 写入的对称读取：保存响应携带新 ETag，调用方拿它继续下一轮编辑。
export async function putWithETag<T>(
  path: string,
  body: unknown,
  extraHeaders?: Record<string, string>
): Promise<Etagged<T>> {
  const resp = await send("PUT", path, body, extraHeaders);
  return { data: await parseBody<T>(resp), etag: resp.headers.get("ETag") };
}

export const api = {
  get: <T>(path: string, headers?: Record<string, string>) =>
    request<T>("GET", path, undefined, headers),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>("POST", path, body, headers),
  // CAS 写：If-Match 经 headers 显式携带（调用方从 getWithETag 取得）；412/428
  // 由 parseBody 类型化上浮。
  put: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>("PUT", path, body, headers),
  patch: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>("PATCH", path, body, headers),
  delete: <T>(path: string, headers?: Record<string, string>) =>
    request<T>("DELETE", path, undefined, headers),
};

// ---------------------------------------------------------------------------
// S10-T2 Studio draft 面（形状逐字段对齐 src/zhiwei/api/agents.py 投影）：
// - AgentDraft.task_graph 节点是 TaskGraphNode 的 UI 子集（parallel_safe/
//   failure_policy/merge 策略由 server 默认值承接，UI 不发明字段）；
// - validate 的 issue 按 code/task_id 渲染（冻结代码集见
//   tests/unit/agents/test_studio_validation_frozen.py）。
// ---------------------------------------------------------------------------

export type StudioPortType = "string" | "number" | "boolean" | "object" | "array" | "ref";

export interface StudioTaskNode {
  task_id: string;
  task_type: string;
  dependencies: string[];
  required_capability: string;
  budget: Record<string, number>;
  input_schema: { properties?: Record<string, { type: StudioPortType }> };
  output_schema: { properties?: Record<string, { type: StudioPortType }> };
}

export interface StudioTaskGraph {
  tasks: StudioTaskNode[];
  edges: [string, string][];
}

export interface AgentDraft {
  agent_id: string;
  name: string;
  description: string;
  instructions: string;
  capabilities: string[];
  task_graph: StudioTaskGraph | null;
  revision: number;
  lifecycle: string;
  updated_at: string;
}

export interface StudioDraftUpdate {
  name?: string;
  description?: string;
  instructions?: string;
  capabilities?: string[];
  task_graph?: StudioTaskGraph;
}

export interface StudioValidationIssue {
  code: string;
  task_id: string;
  field: string;
  detail: string;
}

export interface StudioReleaseView {
  release_id: string;
  agent_id: string;
  agent_version: number;
  state: string;
  manifest_digest: string;
  default_version: number | null;
}

// 依赖 digest 由 builder 显式提供（T3 接管真实依赖管线前的诚实最小面；
// approver 缺省由 server 落 actor principal）
export interface StudioReleaseRequest {
  pack_digest: string;
  model_digest: string;
  knowledge_digest: string;
  memory_digest: string;
  capability_digest: string;
  policy_digest: string;
  eval_digests: string[];
  approver?: string;
  rollout: { default_version: number | null; cohorts: never[] };
  rollback: { in_flight: "complete" | "terminate" };
}

export const studioApi = {
  listDrafts: () => api.get<AgentDraft[]>("/api/v1/agents"),
  createDraft: (body: {
    name: string;
    description: string;
    capabilities: string[];
  }) => api.post<AgentDraft>("/api/v1/agents", body),
  getDraft: (agentId: string) => getWithETag<AgentDraft>(`/api/v1/agents/${agentId}`),
  // CAS 保存：etag 必须来自最近一次 GET/PUT（先读后写）；412 → CasConflictError
  saveDraft: (agentId: string, etag: string, body: StudioDraftUpdate) =>
    putWithETag<AgentDraft>(`/api/v1/agents/${agentId}`, body, { "If-Match": etag }),
  validate: (agentId: string, graph: StudioTaskGraph) =>
    api.post<{ issues: StudioValidationIssue[] }>(
      `/api/v1/agents/${agentId}/validate`,
      graph
    ),
  createRelease: (agentId: string, body: StudioReleaseRequest) =>
    api.post<StudioReleaseView>(`/api/v1/agents/${agentId}/releases`, body),
};
