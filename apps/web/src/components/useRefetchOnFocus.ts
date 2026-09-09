// S10-T4：共享 stale 处理原语（specs/s10 §5 每页 stale/refetch）。分区内容在
// 窗口重新获得焦点时 refetch——数据可能在他处变更（服务器驱动状态，前端不缓存
// 业务副本）。调用方传入稳定的 load 回调（视图自行保证其身份稳定性）。

import { useEffect } from "react";

export function useRefetchOnFocus(refetch: () => void): void {
  useEffect(() => {
    const onFocus = () => refetch();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refetch]);
}
