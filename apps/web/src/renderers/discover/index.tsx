// S10-T4c：Discover 的 ViewManifest 注册（appId: "discover"，templateId:
// "discover-v1"，T1 冻结的绑定约定；T1 占位 stub 由本注册替换——mechanism
// unchanged）。input/result renderer 由本目录文件交付；run template ↔ app 的
// 绑定是数据行——composition root（App.tsx）只 import 本模块一次，通用层永不
// 按名字引用本 App。

import { registerRenderer, registerRunBinding } from "../registry";
import { DiscoverInputRenderer } from "./input";
import { DiscoverResultRenderer } from "./result";

registerRunBinding({ templateId: "discover-v1", appId: "discover" });
registerRenderer({
  appId: "discover",
  InputRenderer: DiscoverInputRenderer,
  ResultRenderer: DiscoverResultRenderer,
});
