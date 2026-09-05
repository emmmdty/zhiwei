"""S9 冻结契约：Agent release 生命周期、SoD 与 rollout 语义（A 档，S9-T4）。

生命周期 draft→sandbox→evaluated→review→staged→published→deprecated/retired 的迁移矩阵、
角色分离（SoD）、manifest 不可变性、cohort 路由、rollback 仅影响新 Run、
security suspend 不受 release pin 保护——全部在本文件冻结。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from uuid import uuid4

from zhiwei.agents.release import (
    ReleaseManifest,
    ReleaseState,
    ReleaseTransitionDenied,
    require_transition_permission,
    validate_transition,
)
from zhiwei.agents.rollout import (
    Cohort,
    RollbackNotApplicable,
    RollbackPolicy,
    RolloutNotConfigured,
    RolloutPolicy,
    apply_rollback,
    route_version,
)


class TestLifecycleTransitions:
    def test_happy_path_forward(self) -> None:
        path = [
            ReleaseState.DRAFT,
            ReleaseState.SANDBOX,
            ReleaseState.EVALUATED,
            ReleaseState.REVIEW,
            ReleaseState.STAGED,
            ReleaseState.PUBLISHED,
            ReleaseState.DEPRECATED,
            ReleaseState.RETIRED,
        ]
        for current, target in zip(path, path[1:]):
            validate_transition(current, target)

    def test_skip_transition_denied(self) -> None:
        with pytest.raises(ReleaseTransitionDenied):
            validate_transition(ReleaseState.DRAFT, ReleaseState.PUBLISHED)

    def test_backward_transition_denied(self) -> None:
        with pytest.raises(ReleaseTransitionDenied):
            validate_transition(ReleaseState.PUBLISHED, ReleaseState.STAGED)

    def test_retired_is_terminal(self) -> None:
        for target in ReleaseState:
            with pytest.raises(ReleaseTransitionDenied):
                validate_transition(ReleaseState.RETIRED, target)

    def test_reactivation_from_deprecated_denied(self) -> None:
        # deprecated 只能继续退到 retired，不允许复活已下线版本。
        with pytest.raises(ReleaseTransitionDenied):
            validate_transition(ReleaseState.DEPRECATED, ReleaseState.PUBLISHED)


class TestRoleSeparation:
    def test_builder_cannot_review(self) -> None:
        with pytest.raises(ReleaseTransitionDenied):
            require_transition_permission(
                "builder", ReleaseState.EVALUATED, ReleaseState.REVIEW
            )

    def test_reviewer_reviews_evaluated(self) -> None:
        require_transition_permission(
            "reviewer", ReleaseState.EVALUATED, ReleaseState.REVIEW
        )

    def test_approver_stages_after_review(self) -> None:
        require_transition_permission(
            "approver", ReleaseState.REVIEW, ReleaseState.STAGED
        )

    def test_reviewer_cannot_publish(self) -> None:
        with pytest.raises(ReleaseTransitionDenied):
            require_transition_permission(
                "reviewer", ReleaseState.STAGED, ReleaseState.PUBLISHED
            )

    def test_release_manager_publishes(self) -> None:
        require_transition_permission(
            "release_manager", ReleaseState.STAGED, ReleaseState.PUBLISHED
        )

    def test_builder_advances_own_draft(self) -> None:
        require_transition_permission(
            "builder", ReleaseState.DRAFT, ReleaseState.SANDBOX
        )

    def test_unknown_role_refused(self) -> None:
        with pytest.raises(ReleaseTransitionDenied):
            require_transition_permission(
                "intern", ReleaseState.DRAFT, ReleaseState.SANDBOX
            )


class TestReleaseManifest:
    def _manifest(self, eval_digest: str) -> ReleaseManifest:
        return ReleaseManifest(
            agent_id=uuid4(),
            agent_version=3,
            pack_digest="sha256:" + "a" * 64,
            model_digest="sha256:" + "b" * 64,
            knowledge_digest="sha256:" + "c" * 64,
            memory_digest="sha256:" + "d" * 64,
            capability_digest="sha256:" + "e" * 64,
            policy_digest="sha256:" + "f" * 64,
            eval_digests=(eval_digest,),
            approver="alice",
            rollout=RolloutPolicy(
                default_version=3,
                cohorts=(
                    Cohort(kind="workspace", selector_id=uuid4(), version=3),
                ),
            ),
            rollback=RollbackPolicy(in_flight="complete"),
        )

    def test_manifest_is_immutable(self) -> None:
        manifest = self._manifest("sha256:" + "1" * 64)
        with pytest.raises(ValidationError):
            manifest.agent_version = 4  # type: ignore[misc]

    def test_equal_dependencies_equal_digest(self) -> None:
        first = self._manifest("sha256:" + "1" * 64)
        second = self._manifest("sha256:" + "1" * 64)
        assert first.content_digest == second.content_digest

    def test_any_dependency_change_changes_digest(self) -> None:
        baseline = self._manifest("sha256:" + "1" * 64)
        changed = self._manifest("sha256:" + "2" * 64)
        assert baseline.content_digest != changed.content_digest


class TestCohortRouting:
    def _policy(self) -> RolloutPolicy:
        workspace = uuid4()
        user = uuid4()
        return RolloutPolicy(
            default_version=1,
            cohorts=(
                Cohort(kind="workspace", selector_id=workspace, version=2),
                Cohort(kind="user", selector_id=user, version=3),
            ),
        ), workspace, user

    def test_user_cohort_wins(self) -> None:
        policy, _workspace, user = self._policy()
        assert (
            route_version(
                policy,
                workspace_id=uuid4(),
                user_id=user,
            )
            == 3
        )

    def test_workspace_cohort_applies(self) -> None:
        policy, workspace, _user = self._policy()
        assert (
            route_version(policy, workspace_id=workspace, user_id=uuid4()) == 2
        )

    def test_default_pin_applies(self) -> None:
        policy, _workspace, _user = self._policy()
        assert route_version(policy, workspace_id=uuid4(), user_id=uuid4()) == 1

    def test_unconfigured_routing_refused(self) -> None:
        policy = RolloutPolicy(default_version=None, cohorts=())
        with pytest.raises(RolloutNotConfigured):
            route_version(policy, workspace_id=uuid4(), user_id=uuid4())

    def test_security_suspend_overrides_pin(self) -> None:
        # security suspend 不受 release pin 保护：暂停中的版本必须拒绝路由。
        policy, workspace, _user = self._policy()
        with pytest.raises(RolloutNotConfigured):
            route_version(
                policy,
                workspace_id=workspace,
                user_id=uuid4(),
                suspended=True,
            )


class TestRollback:
    def test_rollback_rewrites_pins_for_new_runs(self) -> None:
        policy = RolloutPolicy(default_version=5, cohorts=())
        outcome = apply_rollback(
            policy,
            from_version=5,
            to_version=4,
            in_flight_run_ids=(uuid4(), uuid4()),
            rollback=RollbackPolicy(in_flight="complete"),
        )
        assert outcome.policy.default_version == 4
        assert outcome.applies_to == "new_runs_only"
        assert outcome.in_flight_disposition == "complete"
        assert len(outcome.in_flight_run_ids) == 2

    def test_rollback_to_same_version_refused(self) -> None:
        policy = RolloutPolicy(default_version=5, cohorts=())
        with pytest.raises(RollbackNotApplicable):
            apply_rollback(
                policy,
                from_version=5,
                to_version=5,
                in_flight_run_ids=(),
                rollback=RollbackPolicy(in_flight="complete"),
            )

    def test_rollback_ignores_cohort_versions(self) -> None:
        # rollback 不重写 cohort pin：cohort 属于 canary 计划，回滚只改默认 pin。
        policy = RolloutPolicy(
            default_version=1,
            cohorts=(Cohort(kind="workspace", selector_id=uuid4(), version=2),),
        )
        outcome = apply_rollback(
            policy,
            from_version=2,
            to_version=1,
            in_flight_run_ids=(),
            rollback=RollbackPolicy(in_flight="terminate"),
        )
        assert outcome.policy.cohorts == policy.cohorts
        assert outcome.in_flight_disposition == "terminate"

    def test_in_flight_runs_not_executed_by_domain(self) -> None:
        # 域层只声明 disposition，不执行终止：在途 Run 的完成/终止由 runtime 安全策略落地。
        policy = RolloutPolicy(default_version=5, cohorts=())
        outcome = apply_rollback(
            policy,
            from_version=5,
            to_version=4,
            in_flight_run_ids=(uuid4(),),
            rollback=RollbackPolicy(in_flight="terminate"),
        )
        assert outcome.in_flight_disposition == "terminate"
        assert outcome.executed is False
