// S1-T6 same-origin API client：cookie-based session + CSRF + tenant context。
// 所有请求同源（Vite proxy / 生产同源部署），credentials: "include" 携带 cookie。
// - mutation（POST/PUT/PATCH/DELETE）必须带非空 Idempotency-Key（server PEP 要求）
// - tenant context（X-ZhiWei-Organization / X-ZhiWei-Workspace）由 session 解析后全局注入
// - 401 → SessionExpiredError（重定向登录）；403 透传（server-driven，前端不硬判）

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

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  extraHeaders?: Record<string, string>
): Promise<T> {
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
    throw new ApiError(resp.status, detail);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, headers?: Record<string, string>) =>
    request<T>("GET", path, undefined, headers),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>("POST", path, body, headers),
  put: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>("PUT", path, body, headers),
  patch: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    request<T>("PATCH", path, body, headers),
  delete: <T>(path: string, headers?: Record<string, string>) =>
    request<T>("DELETE", path, undefined, headers),
};
