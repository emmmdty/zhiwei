// S10-T1：跨视图重复的 loading/empty/error 状态原语收敛。文案保持各视图
// 既有措辞（e2e 是视觉契约），本组件只统一 aria/role 语义：
// loading → aria-busy；error → role="alert"；empty → <p>。

export type BannerTone = "loading" | "empty" | "error";

export function StateBanner({ tone, text }: { tone: BannerTone; text: string }) {
  if (tone === "loading") return <div aria-busy="true">{text}</div>;
  if (tone === "error") return <div role="alert">{text}</div>;
  return <p>{text}</p>;
}
