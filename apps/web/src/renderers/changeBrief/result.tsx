// S10-T6：ChangeBrief 的 result renderer——views/result.yaml 声明的 renderer_ref
// changeBrief/result。VerifiedBrief（affected symbols/dependencies/tests、related
// PRs/issues/checks、risks、unknowns 逐字、CodeRef/GitHubRef）经通用面板的 run
// 投影消费；RunSummary（T1 契约）暂无 brief artifact 载荷时如实渲染 "artifact
// pending"——绝不发明 ChangeBrief 专属端点或伪造 brief 内容。
// manifest 注册（appId/templateId 绑定）在本模块完成：composition root 只 import
// 一次，通用层永不按名字引用本 App。

import { StateBanner } from "../../components/StateBanner";
import { registerRenderer, registerRunBinding, type ViewManifestProps } from "../registry";
import { ChangeBriefInputRenderer } from "./input";

export function ChangeBriefResultRenderer({ run }: ViewManifestProps) {
  const tasks = Object.entries(run.tasks ?? {});
  return (
    <section aria-label="ChangeBrief result view">
      <StateBanner
        tone="empty"
        text={`Verified brief artifact pending (run ${run.runId}: ${run.status})`}
      />
      {tasks.length === 0 ? (
        <StateBanner tone="empty" text="No task state available" />
      ) : (
        <ul>
          {tasks.map(([taskId, task]) => (
            <li key={taskId}>
              {taskId}: {task.status}
              {task.error ? ` — ${task.error}` : ""}
            </li>
          ))}
        </ul>
      )}
      <p>
        Brief payload (affected symbols / dependencies / tests, related PRs,
        issues, checks, risks, unknowns, CodeRef/GitHubRef) renders here once
        the run artifact projection carries it.
      </p>
    </section>
  );
}

// pack run 的 templateId 取 pack_id（与 ask-v1 绑定约定一致）；schema id 指向
// pack 声明的 verified-brief。
registerRunBinding({ templateId: "change-brief", appId: "change-brief" });
registerRenderer({
  appId: "change-brief",
  inputSchemaId: "verified-brief",
  resultSchemaId: "verified-brief",
  InputRenderer: ChangeBriefInputRenderer,
  ResultRenderer: ChangeBriefResultRenderer,
});
