"""S9-T1 RED: 执行模式绑定契约——跨模式身份不变量、live 操作员门禁、稳定 manifest digest。

六种 EvalMode 只替换 Model/Source/Tool 绑定；身份漂移在收集处 fail closed；
live 只能经 for_live 的显式 operator token 构造，token 绝不进入 manifest。
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from zhiwei.contracts.canonical import digest
from zhiwei.evals.bindings import (
    AgentIdentity,
    BindingSet,
    BindingSpec,
    ModelBinding,
    SourceBinding,
    ToolBinding,
    assert_identity_invariant,
    ensure_live_gate,
)
from zhiwei.evals.domain import EvalMode

IDENTITY = AgentIdentity(
    agent_version_id=UUID("11111111-1111-4111-8111-111111111111"),
    task_graph_digest="sha256:" + "1" * 64,
    runtime_digest="sha256:" + "2" * 64,
    policy_digest="sha256:" + "3" * 64,
    evidence_digest="sha256:" + "4" * 64,
)


def _model(mode: EvalMode) -> ModelBinding:
    return ModelBinding(endpoint_ref=f"endpoints/{mode.value}", model=f"model-{mode.value}")


def _spec(mode: EvalMode) -> BindingSpec:
    if mode is EvalMode.LIVE:
        return BindingSpec.for_live(
            identity=IDENTITY,
            model=_model(mode),
            sources=(SourceBinding(source_id="knowledge-core", snapshot_ref="snapshots/replay"),),
            tools=(ToolBinding(tool_id="judge", implementation_ref="tools/human-rubric"),),
            operator_token="operator-approval-token",
        )
    return BindingSpec(
        mode=mode,
        identity=IDENTITY,
        model=_model(mode),
        sources=(SourceBinding(source_id="knowledge-core", snapshot_ref="snapshots/replay"),),
        tools=(ToolBinding(tool_id="judge", implementation_ref="tools/human-rubric"),),
    )


def test_all_six_modes_bind_substitutions_onto_one_shared_identity() -> None:
    specs = [_spec(mode) for mode in EvalMode]
    assert {spec.mode for spec in specs} == set(EvalMode)
    for spec in specs:
        assert spec.identity == IDENTITY


def test_implicit_live_construction_is_refused() -> None:
    with pytest.raises(ValueError, match="for_live"):
        BindingSpec(mode=EvalMode.LIVE, identity=IDENTITY, model=_model(EvalMode.LIVE))
    # 字符串原始输入与枚举输入走同一个 before 校验器，同样不得隐式构造 live。
    with pytest.raises(ValueError, match="for_live"):
        BindingSpec.model_validate(
            {"mode": "live", "identity": IDENTITY, "model": _model(EvalMode.LIVE)}
        )


def test_implicit_live_refusal_does_not_mutate_the_caller_payload() -> None:
    # 校验器弹出哨兵必须在副本上进行：调用方传入的 dict 是引用，原地 pop 是
    # 被拒绝方也能观察到的副作用（可掩盖伪造门禁痕迹）。
    payload: dict[str, object] = {
        "mode": EvalMode.LIVE,
        "identity": IDENTITY,
        "_operator_gate": "forged-sentinel",
    }
    with pytest.raises(ValueError, match="for_live"):
        BindingSpec.model_validate(payload)
    assert payload["_operator_gate"] == "forged-sentinel"


def test_model_copy_cannot_bypass_the_live_gate_at_consumption() -> None:
    # model_copy 绕过全部校验器：validator 级门禁在此失效，门禁必须在消费点
    # （BindingSet 组装 / manifest 密封 / 身份不变量断言）路径完备地重查。
    ungated_live = _spec(EvalMode.FIXTURE).model_copy(update={"mode": EvalMode.LIVE})
    with pytest.raises(ValidationError, match="operator gate"):
        BindingSet(identity=IDENTITY, specs=(ungated_live,))
    with pytest.raises(ValueError, match="operator gate"):
        ungated_live.manifest
    with pytest.raises(ValueError, match="operator gate"):
        ungated_live.manifest_digest
    with pytest.raises(ValueError, match="operator gate"):
        assert_identity_invariant([ungated_live])
    with pytest.raises(ValueError, match="operator gate"):
        ensure_live_gate(ungated_live)


def test_model_construct_cannot_bypass_the_live_gate_at_consumption() -> None:
    ungated_live = BindingSpec.model_construct(
        mode=EvalMode.LIVE, identity=IDENTITY, model=_model(EvalMode.LIVE)
    )
    with pytest.raises(ValidationError, match="operator gate"):
        BindingSet(identity=IDENTITY, specs=(ungated_live,))
    with pytest.raises(ValueError, match="operator gate"):
        ungated_live.manifest
    with pytest.raises(ValueError, match="operator gate"):
        ungated_live.manifest_digest
    with pytest.raises(ValueError, match="operator gate"):
        assert_identity_invariant([ungated_live])
    with pytest.raises(ValueError, match="operator gate"):
        ensure_live_gate(ungated_live)


def test_gated_live_spec_passes_every_consumption_point() -> None:
    # for_live 产出的 spec 携带私有门禁哨兵：组装、manifest 与不变量断言全放行。
    spec = _spec(EvalMode.LIVE)
    ensure_live_gate(spec)
    assert spec.manifest["mode"] == "live"
    assert spec.manifest_digest.startswith("sha256:")
    binding_set = BindingSet(identity=IDENTITY, specs=(spec,))
    assert {s.mode for s in binding_set.specs} == {EvalMode.LIVE}
    assert_identity_invariant([spec])


def test_for_live_requires_an_explicit_non_empty_operator_token() -> None:
    kwargs: dict[str, object] = {"identity": IDENTITY, "model": _model(EvalMode.LIVE)}
    with pytest.raises(ValueError, match="operator"):
        BindingSpec.for_live(**kwargs, operator_token="")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="operator"):
        BindingSpec.for_live(**kwargs, operator_token="   ")  # type: ignore[arg-type]
    spec = BindingSpec.for_live(**kwargs, operator_token="operator-approval-token")  # type: ignore[arg-type]
    assert spec.mode is EvalMode.LIVE


def test_operator_token_never_enters_the_binding_manifest() -> None:
    token = "operator-secret-token-do-not-leak"
    spec = BindingSpec.for_live(
        identity=IDENTITY,
        model=_model(EvalMode.LIVE),
        operator_token=token,
    )
    assert token not in json.dumps(spec.manifest)


def test_identity_drift_across_modes_is_refused() -> None:
    specs = [_spec(mode) for mode in EvalMode]
    assert_identity_invariant(specs)

    drifted = [_spec(mode) for mode in EvalMode]
    drifted[3] = BindingSpec.for_live(
        identity=AgentIdentity(
            agent_version_id=IDENTITY.agent_version_id,
            task_graph_digest=IDENTITY.task_graph_digest,
            runtime_digest=IDENTITY.runtime_digest,
            policy_digest="sha256:" + "f" * 64,
            evidence_digest=IDENTITY.evidence_digest,
        ),
        model=_model(EvalMode.LIVE),
        operator_token="operator-approval-token",
    )
    with pytest.raises(ValueError, match="identity"):
        assert_identity_invariant(drifted)


def test_binding_set_rejects_identity_drift_and_duplicate_modes() -> None:
    binding_set = BindingSet(identity=IDENTITY, specs=tuple(_spec(mode) for mode in EvalMode))
    assert {spec.mode for spec in binding_set.specs} == set(EvalMode)

    with pytest.raises(ValidationError, match="identity"):
        BindingSet(
            identity=IDENTITY,
            specs=(
                _spec(EvalMode.FIXTURE),
                BindingSpec(
                    mode=EvalMode.REPLAY,
                    identity=AgentIdentity(
                        agent_version_id=UUID("99999999-9999-4999-8999-999999999999"),
                        task_graph_digest=IDENTITY.task_graph_digest,
                        runtime_digest=IDENTITY.runtime_digest,
                        policy_digest=IDENTITY.policy_digest,
                        evidence_digest=IDENTITY.evidence_digest,
                    ),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="mode"):
        BindingSet(
            identity=IDENTITY,
            specs=(_spec(EvalMode.FIXTURE), _spec(EvalMode.FIXTURE)),
        )


def test_identity_fields_are_frozen_and_digest_shaped() -> None:
    with pytest.raises(ValidationError):
        IDENTITY.policy_digest = "sha256:" + "f" * 64  # type: ignore[misc]
    with pytest.raises(ValidationError, match="digest"):
        AgentIdentity(
            agent_version_id=IDENTITY.agent_version_id,
            task_graph_digest="not-a-digest",
            runtime_digest=IDENTITY.runtime_digest,
            policy_digest=IDENTITY.policy_digest,
            evidence_digest=IDENTITY.evidence_digest,
        )


def test_binding_manifests_are_stable_canonical_and_tamper_evident() -> None:
    spec = _spec(EvalMode.FIXTURE)
    assert set(spec.manifest) == {"mode", "identity", "model", "sources", "tools"}
    assert spec.manifest_digest == digest(spec.manifest)
    assert spec.manifest_digest.startswith("sha256:")
    assert _spec(EvalMode.FIXTURE).manifest_digest == spec.manifest_digest
    assert _spec(EvalMode.REPLAY).manifest_digest != spec.manifest_digest
    changed = BindingSpec(
        mode=EvalMode.FIXTURE,
        identity=IDENTITY,
        model=ModelBinding(endpoint_ref="endpoints/fixture", model="model-other"),
    )
    assert changed.manifest_digest != spec.manifest_digest
