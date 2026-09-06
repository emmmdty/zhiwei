// S10-T4b：Ask 的 ViewManifest 注册（appId: "ask"，templateId: "ask-v1"，
// T1 冻结的绑定约定）。input/result renderer 由本目录文件交付；run template ↔
// app 的绑定是数据行——composition root（App.tsx）只 import 本模块一次，
// 通用层永不按名字引用本 App。

import { registerRenderer, registerRunBinding } from "../registry";
import { AskInputRenderer } from "./input";
import { AskResultRenderer } from "./result";

registerRunBinding({ templateId: "ask-v1", appId: "ask" });
registerRenderer({
  appId: "ask",
  InputRenderer: AskInputRenderer,
  ResultRenderer: AskResultRenderer,
});
