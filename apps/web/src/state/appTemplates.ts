// 通用创建面可用的 App 模板列表——对 renderer registry 通用访问器的纯转发。
// 该文件不携带任何 App 名称字面量（那些只住在 renderers/ 注册数据里）；
// features/ 经此桥接消费，以满足冻结架构边界（features 不得直接 import
// renderers，组合根模式：通用层只见通用访问器）。
export { listCreatableTemplates } from "../renderers/registry";
