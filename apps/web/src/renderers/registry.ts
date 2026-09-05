// S10-T1 冻结机制（specs/s10 §2 + tests/architecture/test_app_boundaries.py）：
// App UI 只经 ViewManifest 注册进入通用面板；Core UI 不写任何 App 名称条件。
// 通用层（app/routes/state/components/features）永远 import 本 registry 的
// 接口，而不是具体 App——App 的增删不影响通用层（删除 ChangeBrief 不影响 Core）。
//
// fail-closed：resolveRenderer 对未注册 appId 返回 undefined，调用方渲染
// honest unknown 状态——绝不猜默认 renderer，也不静默渲染成通用面板。

import type { ComponentType } from "react";

// 通用面板消费的 run 投影（api/runs.py RunDetail 的最小形状）。template 是
// run 的规划意图标识（CreateRunRequest.template）；pack run 的取值约定为
// pack_id（如 ask-v1）。后端投影暂未下发该字段——缺席时绑定解析返回
// undefined，通用槽位如实渲染 "No app binding"。
export interface RunSummary {
  runId: string;
  status: string;
  template?: string;
  tasks?: Record<string, { status: string; error: string | null }>;
}

export interface ViewManifestProps {
  run: RunSummary;
}

// ViewManifest：App 在通用 UI 的全部声明面。Input/Result renderer 消费 run
// 元数据与 schema id；schema id 指向 pack 声明的 schema（缺席 = 尚未定义）。
export interface ViewManifest {
  appId: string;
  inputSchemaId?: string;
  resultSchemaId?: string;
  InputRenderer?: ComponentType<ViewManifestProps>;
  ResultRenderer: ComponentType<ViewManifestProps>;
}

// AppRunBinding：run 到 App 的映射是数据（templateId → appId 的注册表），
// 不是通用面板里的名称条件——新 App 接入只加注册行，不改任何通用代码。
export interface AppRunBinding {
  templateId: string;
  appId: string;
}

const manifests = new Map<string, ViewManifest>();
const bindings = new Map<string, string>();

export function registerRenderer(manifest: ViewManifest): void {
  manifests.set(manifest.appId, manifest);
}

export function listRenderers(): ViewManifest[] {
  return Array.from(manifests.values());
}

export function resolveRenderer(appId: string): ViewManifest | undefined {
  return manifests.get(appId);
}

export function registerRunBinding(binding: AppRunBinding): void {
  bindings.set(binding.templateId, binding.appId);
}

// run（或其 template）→ appId；无 template 或未注册的 template → undefined
//（调用方渲染 "No app binding"，不猜）。
export function resolveAppIdForRun(
  run: Pick<RunSummary, "template">
): string | undefined {
  if (run.template === undefined) return undefined;
  return bindings.get(run.template);
}
