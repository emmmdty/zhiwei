// S10-T1：Ask（solution-packs/ask，pack_id: ask-v1）的 ViewManifest 首注册。
// 本任务冻结的是机制：App 专属 input/result renderer 由后续任务交付——当前
// 如实渲染 "not built" + run 元数据（经通用 primitives），不发明 Ask 专属 UI。
// run template ↔ app 的绑定同为数据行：pack run 的 templateId 取 pack_id。

import { StateBanner } from "../../components/StateBanner";
import { registerRenderer, registerRunBinding, type ViewManifestProps } from "../registry";

function AskResultRenderer({ run }: ViewManifestProps) {
  return (
    <section aria-label="Ask app view">
      <p>App view not built (ask)</p>
      <StateBanner tone="empty" text={`run ${run.runId}: ${run.status}`} />
    </section>
  );
}

registerRunBinding({ templateId: "ask-v1", appId: "ask" });
registerRenderer({
  appId: "ask",
  ResultRenderer: AskResultRenderer,
});
