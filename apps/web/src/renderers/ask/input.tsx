// S10-T4b：Ask 的 input renderer——run input（question）的诚实呈现面。
// runtime REST 契约（api/runs.py RunDetail / canonical intake 输出）暂不投影
// 问题文本：AskIntake 只落 question_id/parsed_scope，正文不入 canonical——
// 这里如实声明缺席，绝不从 run 元数据编造问题。re-ask API 不存在 → 不提供
// re-ask 控件（不发明端点对应的 UI）。manifest 注册在本目录 index.tsx 完成。

import type { ViewManifestProps } from "../registry";

export function AskInputRenderer({ run }: ViewManifestProps) {
  return (
    <section aria-label="App input view">
      <p>
        Run input (question) is not projected by the runtime REST contract yet.
      </p>
      <ul>
        <li>run: {run.runId}</li>
        <li>status: {run.status}</li>
      </ul>
      {/* re-ask affordance 仅在 re-ask API 存在时提供——当前无此端点 */}
    </section>
  );
}
