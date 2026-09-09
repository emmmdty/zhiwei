// S10-T1：composition root——挂 session provider + AppShell，并单点触发
// renderer 注册（renderers/index 副作用）。视图结构在 app/AppShell +
// routes/sections；App UI 只经 renderers/registry 进入通用面板。

import "./renderers";
import { AppShell } from "./app/AppShell";
import { SessionProvider } from "./lib/session";

export function App() {
  return (
    <SessionProvider>
      <AppShell />
    </SessionProvider>
  );
}
