// S10-T1：通用 App renderer 槽位——features 消费 App UI 的唯一合法路径
//（features 不得直接 import renderers/，架构契约）。App 归属解析与 renderer
// 解析全部经 registry 数据完成，本组件不含任何 App 名称条件：
// - run 无 template / template 未注册绑定 → "No app binding"（诚实缺席，
//   生产后端投影暂无 template 字段时即此态）
// - 绑定指向未注册 appId → "Unknown app: <appId>"（fail-closed，不猜默认）
// - 命中注册 → 渲染该 App 的 ResultRenderer

import { resolveAppIdForRun, resolveRenderer, type RunSummary } from "../renderers/registry";
import { StateBanner } from "./StateBanner";

// 绑定存在性探针（components 是 features 与 renderers/registry 之间的唯一合法
// 桥——features 不得 import renderers/）。供通用面板做「绑定的 App 自有 evidence
// 视图 vs 通用面板接管」的结构性分流：只看绑定数据，不含任何 App 名称条件。
export function hasAppBinding(run: RunSummary): boolean {
  return resolveAppIdForRun(run) !== undefined;
}

export function AppRendererSlot({ run }: { run: RunSummary }) {
  const appId = resolveAppIdForRun(run);
  const manifest = appId === undefined ? undefined : resolveRenderer(appId);
  return (
    <section aria-label="App panel">
      <h3>App</h3>
      {appId === undefined ? (
        <StateBanner tone="empty" text="No app binding" />
      ) : manifest ? (
        <manifest.ResultRenderer run={run} />
      ) : (
        <StateBanner tone="empty" text={`Unknown app: ${appId}`} />
      )}
    </section>
  );
}
