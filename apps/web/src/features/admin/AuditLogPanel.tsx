// S1-T6 审计日志（auditor 角色入口，自 App.tsx 内联组件迁移，S10-T1）。
// S1 未交付 audit-events 端点——空态占位如实呈现，不造假事件。

export function AuditLogPanel() {
  return (
    <section>
      <h3>Audit log</h3>
      <p>No audit events</p>
    </section>
  );
}
