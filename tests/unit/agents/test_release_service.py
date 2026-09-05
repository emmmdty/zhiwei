"""S9-T4 RED：release/claim 应用服务层契约（域层状态机由冻结契约另行覆盖）。

单元层只覆盖不依赖 PG 的服务级规则：
- manifest 载荷的持久化往返必须保持 content_digest（manifest 列写什么读回什么，
  破坏 payload 即破坏 manifest 不可变性）；
- release 状态字符串 ↔ ReleaseState 往返（存储值域封闭，未知值 fail closed）；
- claim 升级由「真实密封件复算」驱动：build_sealed_artifact → verify_sealed_artifact
  的复算 digest 作为 verified_seal_digest，升级被接受；复算值不一致一律拒绝——
  这是 ClaimRegistryService 不信任调用方验证结论的域层基础；
- 服务必须显式 workspace 上下文（不编造租户作用域）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from zhiwei.agents.claims import (
    ClaimEvidence,
    ClaimRecord,
    ClaimRegistryService,
    ClaimScope,
    ClaimStatus,
    ClaimUpgradeDenied,
    upgrade_claim,
)
from zhiwei.agents.release import ReleaseManifest, ReleaseService, ReleaseState
from zhiwei.agents.rollout import Cohort, RollbackPolicy, RolloutPolicy

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import EvalMode, EvalRunState, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.sealing import build_sealed_artifact, verify_sealed_artifact
from zhiwei.persistence.tenant import TenantContextRequired


def _manifest(**overrides: object) -> ReleaseManifest:
    fields: dict[str, object] = {
        "agent_id": uuid4(),
        "agent_version": 3,
        "pack_digest": "sha256:" + "a" * 64,
        "model_digest": "sha256:" + "b" * 64,
        "knowledge_digest": "sha256:" + "c" * 64,
        "memory_digest": "sha256:" + "d" * 64,
        "capability_digest": "sha256:" + "e" * 64,
        "policy_digest": "sha256:" + "f" * 64,
        "eval_digests": ("sha256:" + "1" * 64,),
        "approver": "alice",
        "rollout": RolloutPolicy(
            default_version=3,
            cohorts=(Cohort(kind="workspace", selector_id=uuid4(), version=3),),
        ),
        "rollback": RollbackPolicy(in_flight="complete"),
    }
    fields.update(overrides)
    return ReleaseManifest(**fields)  # type: ignore[arg-type]


class TestManifestPersistenceRoundtrip:
    def test_payload_roundtrip_preserves_content_digest(self) -> None:
        manifest = _manifest()
        payload = manifest.model_dump(mode="json")
        restored = ReleaseManifest.model_validate(payload)
        assert restored == manifest
        assert restored.content_digest == manifest.content_digest

    def test_payload_tampering_changes_content_digest(self) -> None:
        payload = _manifest().model_dump(mode="json")
        payload["agent_version"] = 4
        assert ReleaseManifest.model_validate(payload).content_digest != _manifest().content_digest


class TestReleaseStatePersistence:
    def test_state_string_roundtrip_covers_all_states(self) -> None:
        for state in ReleaseState:
            assert ReleaseState(state.value) is state


class TestClaimUpgradeThroughRealSealVerify:
    def _verified_seal(self) -> tuple[ClaimEvidence, str]:
        unit = RegisteredUnit(sample_id="s-1", unit_id="u-1")
        state = EvalRunState.create(
            mode=EvalMode.OFFLINE,
            registered_units=(unit,),
            code_digest=digest_bytes(b"code"),
            config_digest=digest_bytes(b"config"),
            schema_digest=digest_bytes(b"schema"),
        )
        sealed_state = state.record(
            SampleOutcome(unit=unit, status=SampleStatus.COMPLETED, result={"answer": "42"})
        ).seal()
        artifact, seal_digest = build_sealed_artifact(
            run_id=uuid4(),
            eval_run_id=uuid4(),
            state=sealed_state,
            dataset_digest=digest_bytes(b"dataset"),
            dataset_manifest_id=uuid4(),
            suite_digest=digest_bytes(b"suite"),
            migration_revision="0014_cost_ledger",
            test_report_digest=digest_bytes(b"report"),
            test_report_manifest_id=uuid4(),
        )
        verified = verify_sealed_artifact(artifact.canonical_mapping(), seal_digest)
        evidence = ClaimEvidence(
            eval_run_id=artifact.eval_run_id,
            seal_digest=seal_digest,
            artifact_manifest_id=uuid4(),
            mode=verified.mode,
        )
        return evidence, seal_digest

    def _implemented_claim(self) -> ClaimRecord:
        return ClaimRecord(
            claim_id="factqa-v1.accuracy",
            statement="FactQA accuracy {{accuracy}}（{{environment}}）",
            scope=ClaimScope(
                mode="offline",
                model="reference-fixture",
                version="1",
                date="2026-09-05",
                corpus="factqa-v1",
                environment="offline-fixture",
            ),
            status=ClaimStatus.IMPLEMENTED,
        )

    def test_upgrade_accepts_recomputed_seal_digest(self) -> None:
        evidence, seal_digest = self._verified_seal()
        upgraded = upgrade_claim(
            self._implemented_claim(),
            evidence,
            target=ClaimStatus.OFFLINE_VERIFIED,
            verified_seal_digest=seal_digest,
        )
        assert upgraded.status is ClaimStatus.OFFLINE_VERIFIED
        assert upgraded.evidence is not None
        assert upgraded.evidence.seal_digest == seal_digest
        assert upgraded.evidence.mode == "offline"

    def test_upgrade_refuses_digest_outside_verified_seal(self) -> None:
        evidence, _seal_digest = self._verified_seal()
        with pytest.raises(ClaimUpgradeDenied):
            upgrade_claim(
                self._implemented_claim(),
                evidence,
                target=ClaimStatus.OFFLINE_VERIFIED,
                verified_seal_digest="sha256:" + "9" * 64,
            )


class TestServiceContextDiscipline:
    def test_release_service_requires_workspace_context(self) -> None:
        with pytest.raises(TenantContextRequired):
            ReleaseService(None, None)  # type: ignore[arg-type]

    def test_claim_registry_service_requires_workspace_context(self) -> None:
        with pytest.raises(TenantContextRequired):
            ClaimRegistryService(None, None, None)  # type: ignore[arg-type]
