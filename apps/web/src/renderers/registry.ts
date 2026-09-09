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
// pack_id（如 ask-v1）。FIX-A 起后端下发 template: string | null——null 与
// 缺席同义（无绑定输入），解析一律返回 undefined，槽位如实渲染 "No app
// binding"。
export interface RunSummary {
  runId: string;
  status: string;
  template?: string | null;
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
// creatable 声明该 App 的 run 能否从通用创建面发起（默认 false，fail closed）：
// 只有后端确实可执行的模板才置 true；registered-but-unexecutable 的模板
// （如 discover-v1 在 E3 解锁前）保持 false，避免永败控件。
export interface AppRunBinding {
  templateId: string;
  appId: string;
  creatable?: boolean;
}

const manifests = new Map<string, ViewManifest>();
const bindings = new Map<string, AppRunBinding>();

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
  bindings.set(binding.templateId, binding);
}

// run（或其 template）→ appId；template 缺席/null/未注册 → undefined
//（调用方渲染 "No app binding"，不猜）。falsy 统一挡掉——后端 null 与前端
// undefined 两条路径必须行为一致。
export function resolveAppIdForRun(
  run: Pick<RunSummary, "template">
): string | undefined {
  if (!run.template) return undefined;
  return bindings.get(run.template)?.appId;
}

// 可创建模板列表：通用创建面（Workbench）从这里消费，templateId 字面量只
// 住在本注册表（与后端 pack_templates.py 同构的「注册数据」豁免面）。
export function listCreatableTemplates(): string[] {
  return Array.from(bindings.values())
    .filter((b) => b.creatable === true)
    .map((b) => b.templateId)
    .sort();
}
