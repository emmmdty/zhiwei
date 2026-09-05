// S10-T1：规范布局的 client 入口（prescribed layout src/api/）。
//
// 【偏差登记】当前是 re-export shim 而非实现本体：既有 mock e2e（白名单外）
// 的 `**/api/**` 拦截 glob 会把 /src/api/* 模块脚本一并截成 JSON（Playwright
// `**` 跨 "/"），实现本体留在 lib/api.ts（URL 不含 "/api/" 段）。operator 将
// 两处 glob 根锚定后翻转本文件与 lib/api.ts 的方向即完成归位——届时 features
// 的 import 目标同步切换，调用方接口不变。

export {
  ApiError,
  CasConflictError,
  PreconditionRequiredError,
  SessionExpiredError,
  api,
  generateIdempotencyKey,
  getCsrfToken,
  getWithETag,
  setSessionMeta,
} from "../lib/api";
