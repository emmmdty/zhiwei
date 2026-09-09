"""S7 Security: MemoryActivityInput.principal_kind 必填（fail-open 修复契约）。

S7 spec §3「ServiceAccount 不可读 personal memory」依赖 principal_kind 被显式
声明：带默认值 USER 时，调用方漏传即静默获得 USER 语义。必填让类型系统迫使
组合根显式声明——漏传在构造期即 TypeError，而非运行期静默放宽。

SERVICE_ACCOUNT 主体的来源：S8 DiscoveryTriggerService.background_run_context
返回的 BackgroundRunContext（service identity，无创建者身份），经
principal_kind_for_background_run 显式推导，未知取值 fail closed。
"""

from __future__ import annotations

from uuid import UUID

import pytest

from zhiwei.identity.domain import PrincipalKind
from zhiwei.runtime.triggers.discovery import BackgroundRunContext
from zhiwei.workflows.activities.memory import (
    MemoryActivity,
    MemoryActivityInput,
    principal_kind_for_background_run,
)

_ORG_ID = UUID("11111111-1111-4111-8111-111111111111")
_WS_ID = UUID("22222222-2222-4222-8222-222222222222")
_USER_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_SERVICE_ACCOUNT = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

_MISSING = object()


def _base_input_kwargs() -> dict[str, object]:
    return {
        "run_id": "run-principal-kind",
        "task_id": "task-principal-kind",
        "attempt_no": 1,
        "organization_id": str(_ORG_ID),
        "workspace_id": str(_WS_ID),
        "principal_id": str(_USER_A),
        "action": "retrieve",
        "query": {"text": "editor", "top_k": 10},
    }


def _make_input(**overrides: object) -> MemoryActivityInput:
    kwargs: dict[str, object] = {**_base_input_kwargs(), **overrides}
    # principal_kind 不在 kwargs 中时不传（构造期必缺省）——不能显式传 None
    principal_kind = kwargs.pop("principal_kind", _MISSING)
    args: dict[str, object] = {
        "run_id": str(kwargs["run_id"]),
        "task_id": str(kwargs["task_id"]),
        "attempt_no": kwargs["attempt_no"],
        "organization_id": str(kwargs["organization_id"]),
        "workspace_id": str(kwargs["workspace_id"]),
        "principal_id": str(kwargs["principal_id"]),
        "action": str(kwargs["action"]),
        "query": kwargs.get("query"),
    }
    if principal_kind is not _MISSING:
        args["principal_kind"] = principal_kind
    return MemoryActivityInput(**args)  # type: ignore[arg-type]


class TestPrincipalKindRequired:
    def test_missing_principal_kind_is_not_constructible(self) -> None:
        """不传 principal_kind 必须 TypeError——类型层面禁止静默 USER 语义。"""
        with pytest.raises(TypeError):
            _make_input()

    @pytest.mark.asyncio
    async def test_explicit_user_kind_preserves_retrieval(self) -> None:
        """显式 USER 声明时行为不变（personal memory 对 USER 主体可见）。"""
        activity = MemoryActivity()
        result = await activity.execute(_make_input(principal_kind=PrincipalKind.USER))
        assert result.status == "completed"
        assert result.personal_memory_excluded is False


class TestBackgroundRunPrincipalKindDerivation:
    def test_service_account_derived_from_background_run_context(self) -> None:
        """后台 run 主体声明（BackgroundRunContext）显式推导为 SERVICE_ACCOUNT。"""
        context = BackgroundRunContext(
            principal_kind="service_account",
            principal_id=str(_SERVICE_ACCOUNT),
            personal_memory_access=False,
        )
        assert principal_kind_for_background_run(context) is PrincipalKind.SERVICE_ACCOUNT

    def test_unknown_run_principal_kind_fails_closed(self) -> None:
        """未知 principal_kind 取值拒绝推导——绝不回退为 USER。"""
        context = BackgroundRunContext(
            principal_kind="root",
            principal_id=str(_SERVICE_ACCOUNT),
            personal_memory_access=False,
        )
        with pytest.raises(ValueError):
            principal_kind_for_background_run(context)


class TestServiceAccountPersonalMemoryStillRefused:
    @pytest.mark.asyncio
    async def test_explicit_service_account_targeting_personal_scope_refused(
        self,
    ) -> None:
        """必填化后既有 fail-closed 语义保持：SERVICE_ACCOUNT 查 personal scope 仍拒绝。"""
        activity = MemoryActivity()
        result = await activity.execute(
            MemoryActivityInput(
                run_id="run-principal-kind",
                task_id="task-principal-kind",
                attempt_no=1,
                organization_id=str(_ORG_ID),
                workspace_id=str(_WS_ID),
                principal_id=str(_SERVICE_ACCOUNT),
                principal_kind=PrincipalKind.SERVICE_ACCOUNT,
                action="retrieve",
                query={"text": "editor", "top_k": 10},
                filters={"scope": "user", "scope_subject_id": str(_USER_A)},
            )
        )
        assert result.status == "refused"
        assert result.refusal_reason is not None
        assert "personal memory" in result.refusal_reason
