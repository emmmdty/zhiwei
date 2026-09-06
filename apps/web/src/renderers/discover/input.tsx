// S10-T4c：Discover 的 input renderer——trigger/program context 的诚实呈现面。
// runtime REST 契约（api/runs.py RunDetail）暂不投影 DiscoveryProgram/trigger
// 字段：这里如实声明缺席，绝不从 run 元数据编造 trigger/program。feed/triage
// 面由 result.tsx 承担（workbench journey 的入口）；manifest 注册在本目录
// index.tsx 完成。

import type { ViewManifestProps } from "../registry";

export function DiscoverInputRenderer({ run }: ViewManifestProps) {
  return (
    <section aria-label="App input view">
      <p>
        Trigger and program context are not projected by the runtime REST contract
        yet.
      </p>
      <ul>
        <li>run: {run.runId}</li>
        <li>status: {run.status}</li>
      </ul>
      {/* trigger 重放/program 编辑控件仅在对应投影端点存在时提供——当前无此端点 */}
    </section>
  );
}
