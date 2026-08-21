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
  body?: unknown
): Promise<T> {
  const headers: Record<string, string> = { ..._tenantHeaders };
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
    const detail =
      typeof payload === "object" && payload && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : String(payload ?? resp.statusText);
    throw new ApiError(resp.status, detail);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
};
