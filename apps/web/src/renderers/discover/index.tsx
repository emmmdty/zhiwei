// S10-T1：Discover（solution-packs/discover，pack_id: discover-v1）的
// ViewManifest 首注册——机制同 renderers/ask：renderer 后续任务交付，当前
// 如实渲染 "not built" + run 元数据，不发明 Discover 专属 UI。

import { StateBanner } from "../../components/StateBanner";
import { registerRenderer, registerRunBinding, type ViewManifestProps } from "../registry";

function DiscoverResultRenderer({ run }: ViewManifestProps) {
  return (
    <section aria-label="Discover app view">
      <p>App view not built (discover)</p>
      <StateBanner tone="empty" text={`run ${run.runId}: ${run.status}`} />
    </section>
  );
}

registerRunBinding({ templateId: "discover-v1", appId: "discover" });
registerRenderer({
  appId: "discover",
  ResultRenderer: DiscoverResultRenderer,
});
