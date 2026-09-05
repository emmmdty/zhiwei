// S1-T6 same-origin API client + S10-T1 CAS/ETag 扩展。
//
// 【S10-T1 偏差登记】本模块保留 lib/ 物理位置（dev URL /src/lib/api.ts 不含
// "/api/" 段）。规范布局的 src/api/client.ts 已就位并 re-export 本模块；但
// 既有 mock e2e（runtime-approval / eval-release-observability，白名单外）
// 的路由拦截用 `**/api/**` glob——Playwright 的 `**` 跨 "/"，会把模块脚本
// /src/api/* 一并拦截成 JSON，页面模块图无法加载。operator 将两处 glob 改为
// 根锚定（如 "/api/**"）后，把实现迁到 src/api/client.ts、本文件改回
// re-export shim 即完成归位（单文件翻转，无调用方改动）。
//
// 语义（保持不变）：
// - cookie-based session + CSRF + tenant context；同源请求 credentials include
// - mutation（POST/PUT/PATCH/DELETE）必须带非空 Idempotency-Key（server PEP 要求）
// - tenant context（X-ZhiWei-Organization / X-ZhiWei-Workspace）由 session 解析后全局注入
// - 401 → SessionExpiredError（重定向登录）；403 透传（server-driven，前端不硬判）
//
// S10-T1 新增（Studio draft CAS 契约，tests/contract/api/test_agents_studio_frozen.py）：
// - PUT 的 If-Match 经 headers 显式携带；412/428 类型化上浮为
//   CasConflictError / PreconditionRequiredError（继承 ApiError，既有 catch 不破）
// - getWithETag：读取资源当前 ETag（CAS 前置：先读后写）

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
