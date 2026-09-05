"""S9-T1 RED: 执行模式绑定契约——跨模式身份不变量、live 操作员门禁、稳定 manifest digest。

六种 EvalMode 只替换 Model/Source/Tool 绑定；身份漂移在收集处 fail closed；
live 只能经 for_live 的显式 operator token 构造，token 绝不进入 manifest。
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError
from zhiwei.evals.bindings import (
    AgentIdentity,
    BindingSet,
    BindingSpec,
    ModelBinding,
    SourceBinding,
    ToolBinding,
    assert_identity_invariant,
)

from zhiwei.contracts.canonical import digest
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
    with pytest.raises(ValueError, match="for_live"):
        BindingSpec(mode="live", identity=IDENTITY, model=_model(EvalMode.LIVE))


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
