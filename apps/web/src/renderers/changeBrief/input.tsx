// S10-T6：ChangeBrief（solution-packs/change-brief，pack_id: change-brief）的 input
// renderer——views/input.yaml 声明的 renderer_ref changeBrief/input。触发/repo/ref
// 摘要是 run input 投影的内容：通用 RunSummary 契约（T1）暂未下发 input 载荷，
// 这里只读渲染 run 元数据并如实声明缺席——不发明 ChangeBrief 专属端点，不猜默认。

import { StateBanner } from "../../components/StateBanner";
import type { ViewManifestProps } from "../registry";

export function ChangeBriefInputRenderer({ run }: ViewManifestProps) {
  const tasks = Object.entries(run.tasks ?? {});
  return (
    <section aria-label="ChangeBrief input view">
      <p>
        Change trigger (repository / commit-or-PR) is not projected in the run
        summary yet.
      </p>
      <ul>
        <li>run: {run.runId}</li>
        <li>status: {run.status}</li>
      </ul>
      {tasks.length === 0 ? (
        <StateBanner tone="empty" text="No task state available" />
      ) : (
        <ul>
          {tasks.map(([taskId, task]) => (
            <li key={taskId}>
              {taskId}: {task.status}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
