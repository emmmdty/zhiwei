# S1-T3 RED 骨架：仅包声明与默认拒绝。规则实现属于 GREEN（feat(policy)）。
# 冻结契约见 authz_test.rego；矩阵事实源 docs/PERMISSIONS.md §3.1。
package zhiwei.authz

default allow := false
default reason := "default_deny:no_rule_matched"

hard_deny := set()
sod_deny := set()
context_deny := set()
delegation_deny := set()
