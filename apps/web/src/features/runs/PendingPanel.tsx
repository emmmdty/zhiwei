// S10 fix-B（specs/s10 §2 面板结构 + §5）：无后端投影的数据面诚实占位。
// 占位面板必须与真实数据面板可区分（data-panel-state），且只声明「投影缺席 +
// 将来会展示什么」，绝不渲染伪造数据或不可用控件（§5：无后端 action 的控件
// 不得出现）。

export function PendingPanel({ title, wouldShow }: { title: string; wouldShow: string }) {
  return (
    <section aria-label={title} data-panel-state="pending">
      <h3>{title}</h3>
      <p>
        {title} — panel pending backend projection. Would show: {wouldShow}
      </p>
    </section>
  );
}
