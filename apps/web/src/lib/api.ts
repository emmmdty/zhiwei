// S1-T6 same-origin API client 的 re-export shim（S10-T2 归位）。
//
// 实现本体在规范布局 src/api/client.ts（S10-T2 自本文件迁回——归位前本体留在
// lib/ 是因为既有 mock e2e 的 `**/api/**` 拦截 glob 会把 /src/api/* 模块脚本截成
// JSON；该 glob 已根锚定为 "/api/**"）。既有调用方 import 路径不变；Studio 等
// 新代码直接 import "../api/client"。

export * from "../api/client";
