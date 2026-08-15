"""S1-T3 RED：policy 层冻结枚举与 schema 边界（A 档契约）。

roles.py 只提供严格类型与协议边界（枚举、资源→动作 schema、角色作用域分类），
不复制 Rego 里的角色→权限映射；角色→权限的唯一事实实现是 policies/zhiwei/authz.rego。

失败分类：本文件在 RED 阶段因 src/zhiwei/policy/roles.py 不存在而 ImportError；
GREEN 后必须全绿且不得放宽断言。
"""

from __future__ import annotations

import pytest

from zhiwei.policy.roles import (
    LEGACY_ROLE_ALIASES,
    ORG_SCOPED_ROLES,
    RESOURCE_ACTIONS,
    WORKSPACE_SCOPED_ROLES,
    Action,
    Classification,
    Purpose,
    ResourceType,
    Risk,
    Role,
    RoleScope,
    normalize_role,
)


class TestFrozenRoleVocabulary:
    """冻结矩阵（docs/PERMISSIONS.md §3.1）的角色名是唯一受认可的角色词汇。"""

    def test_role_names_match_frozen_matrix(self) -> None:
        assert set(Role) == {
            "org_owner",
            "security_admin",
            "capability_publisher",
            "workspace_admin",
            "agent_builder",
            "memory_steward",
            "approver",
            "member",
            "auditor",
        }

    def test_scope_vocabulary(self) -> None:
        assert set(RoleScope) == {"org", "workspace"}

    def test_workspace_scoped_roles_are_only_workspace_roles(self) -> None:
        # 矩阵中只有 Workspace Admin 与 Agent Builder 是 workspace 作用域角色
        assert WORKSPACE_SCOPED_ROLES == frozenset({Role.WORKSPACE_ADMIN, Role.AGENT_BUILDER})

    def test_org_scoped_roles_are_the_rest(self) -> None:
        assert ORG_SCOPED_ROLES == frozenset(set(Role) - set(WORKSPACE_SCOPED_ROLES))

    def test_scope_partition_is_exhaustive(self) -> None:
        assert ORG_SCOPED_ROLES | WORKSPACE_SCOPED_ROLES == frozenset(set(Role))
        assert ORG_SCOPED_ROLES & WORKSPACE_SCOPED_ROLES == frozenset()


class TestContextVocabulary:
    def test_classification_ladder_frozen(self) -> None:
        # docs/PERMISSIONS.md §7：PUBLIC | INTERNAL | CONFIDENTIAL | RESTRICTED
        assert set(Classification) == {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"}

    def test_risk_vocabulary_matches_matrix(self) -> None:
        # 矩阵按低中/高/关键分级（capability 行、工具 effect/risk）
        assert set(Risk) == {"low", "medium", "high", "critical"}

    def test_purpose_vocabulary_is_closed(self) -> None:
        # purpose 缺失或未知都在边界拒绝，绝不默认成 general（MC-12）
        assert set(Purpose) == {"general", "compliance", "security", "audit"}


class TestResourceActionSchema:
    """每个资源的合法动作集（矩阵行转写）；(resource, action) 组合是 input 边界校验的一部分。"""

    def test_resource_action_schema_matches_frozen_matrix(self) -> None:
        expected: dict[str, set[str]] = {
            "org": {
                "manage",
                "delegate",
                "config_security",
                "manage_workspace_members",
                "read_self",
                "read_audit",
            },
            "workspace_policy": {
                "configure",
                "configure_security_egress",
                "configure_workspace",
                "read",
                "read_memory_policy",
                "read_approval_policy",
                "read_applicable",
                "export",
            },
            "knowledge_source": {
                "delegate",
                "classify_egress",
                "suspend",
                "manage",
                "bind_draft",
                "debug",
                "read",
                "read_published",
                "read_provenance",
            },
            "capability_version": {
                "import_check_test",
                "admit_low_medium",
                "review_high_critical",
                "suspend",
                "revoke",
                "bind_workspace",
                "bind_draft",
                "browse",
                "read_admission_audit",
            },
            "connection_secret": {
                "revoke",
                "read_security_metadata",
                "define_credential_requirement",
                "create_workspace_connection",
                "rotate",
                "create_own",
                "revoke_own",
                "read_status_fingerprint",
            },
            "agent_draft": {
                "reject_suspend_security",
                "read",
                "delegate_builder",
                "create_edit_run",
                "read_version_gate",
            },
            "agent_publish": {
                "request",
                "review_publish",
                "rollback",
                "veto_hard_gate",
                "read_manifest",
            },
            "run_case_artifact": {
                "break_glass_incident",
                "manage_lifecycle",
                "run_sandbox",
                "read",
                "read_case_memory",
                "read_minimal_approval_context",
                "run_published",
                "manage_visible_cases",
                "export",
            },
            "team_memory": {
                "quarantine",
                "revoke",
                "configure_policy",
                "submit_candidate",
                "confirm",
                "correct",
                "conflict",
                "submit_own_candidate",
                "read_authorized",
                "read_provenance",
            },
            "tool_approval": {
                "emergency_revoke",
                "configure_approver_group",
                "request",
                "approve",
                "reject",
                "replace",
            },
        }
        actual = {
            resource.value: {action.value for action in actions}
            for resource, actions in RESOURCE_ACTIONS.items()
        }
        assert actual == expected

    def test_every_action_is_an_enum_member(self) -> None:
        for actions in RESOURCE_ACTIONS.values():
            for action in actions:
                assert Action(action.value) is action

    def test_action_enum_has_no_unlisted_actions(self) -> None:
        listed = {a.value for a in Action}
        schema_actions = {a.value for actions in RESOURCE_ACTIONS.values() for a in actions}
        assert listed == schema_actions


class TestLegacyAliases:
    def test_owner_aliases_org_owner(self) -> None:
        # T1 引导写入的 "owner" 即矩阵 Organization Owner；别名是唯一受认可的历史字符串
        assert LEGACY_ROLE_ALIASES["owner"] is Role.ORG_OWNER

    def test_builder_aliases_agent_builder(self) -> None:
        # docs/PERMISSIONS.md §3：产品中的 Builder 就是 Agent Builder，不是 Workspace Admin
        assert LEGACY_ROLE_ALIASES["builder"] is Role.AGENT_BUILDER

    def test_normalize_role_exact_name(self) -> None:
        assert normalize_role("auditor") is Role.AUDITOR

    def test_normalize_role_alias(self) -> None:
        assert normalize_role("owner") is Role.ORG_OWNER
        assert normalize_role("builder") is Role.AGENT_BUILDER

    def test_normalize_role_rejects_unknown(self) -> None:
        # 未知角色必须拒绝（MC-2）：superuser 等任何非冻结名一律失败，不能透传
        with pytest.raises(ValueError):
            normalize_role("superuser")
        with pytest.raises(ValueError):
            normalize_role("")
        with pytest.raises(ValueError):
            normalize_role("Owner")
