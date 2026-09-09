"""S6 Security: Evidence 可见性的 ADR-006 时态语义（服务侧等价物）。

事实源：specs/s6-evidence-ask.md §3/§6、ADR-006。

ADR-006 核心语义在 Evidence 域的服务级契约：
- 可复算性与可见性解耦：Evidence 永远可被系统复算（审计/eval 通道）；
- 对用户的可见性按**当前** ACL 重新校验并 fail closed；
- 失权呈现：查询返回 evidence_access_revoked 标记而非内容，不静默移除；
- Auditor 走独立审计通道可见；
- reference_only 不得支撑 Fact 类 claim——服务级拒绝。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from zhiwei.evidence.access import (
    EvidencePrincipal,
    PrincipalKind,
    resolve_evidence_views,
    service_rejects_reference_only_fact,
)
from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.canonical_values import ReproducibilityLevel, make_canonical_int
from zhiwei.evidence.claims import FactClaim
from zhiwei.evidence.errors import ClaimLevelViolationError
from zhiwei.evidence.refs import DocRef, QueryReplayRef
from zhiwei.knowledge.acl import ACLContext
from zhiwei.knowledge.contracts import ACLSnapshot

_FROZEN_AT = datetime(2026, 9, 1, tzinfo=UTC)


def _bundle() -> EvidenceBundle:
    ref_a = QueryReplayRef(
        ref_id=uuid4(),
        reproducibility_level=ReproducibilityLevel.REPLAYABLE,
        source_id=uuid4(),
        created_at=_FROZEN_AT,
        sql="SELECT name FROM liangshan WHERE rank = ?",
        params={"positional": [7]},
    )
    ref_b = QueryReplayRef(
        ref_id=uuid4(),
        reproducibility_level=ReproducibilityLevel.REPLAYABLE,
        source_id=uuid4(),
        created_at=_FROZEN_AT,
        sql="SELECT merit_count FROM liangshan WHERE rank = ?",
        params={"positional": [7]},
    )
    doc_ref = DocRef(
        ref_id=uuid4(),
        reproducibility_level=ReproducibilityLevel.REFERENCE_ONLY,
        source_id=uuid4(),
        created_at=_FROZEN_AT,
        document_uri="docs/zhaoan.md",
    )
    claim = FactClaim(
        claim_id=uuid4(),
        answer_id=uuid4(),
        evidence_refs=(ref_a,),
        answer_digest="sha256:" + "a" * 64,
        canonical_value=make_canonical_int(45),
        created_at=_FROZEN_AT,
        updated_at=_FROZEN_AT,
    )
    return EvidenceBundle(
        bundle_id=uuid4(),
        answer_id=claim.answer_id,
        evidence_refs=(ref_a, ref_b, doc_ref),
        claims=(claim,),
        created_at=_FROZEN_AT,
    )


def _user_principal() -> EvidencePrincipal:
    return EvidencePrincipal(
        kind=PrincipalKind.USER,
        acl_context=ACLContext(
            principal_id=uuid4(),
            organization_id=uuid4(),
            workspace_id=uuid4(),
            allowed_groups=frozenset({"workspace-members"}),
        ),
    )


def _granted() -> ACLSnapshot:
    return ACLSnapshot(allowed_groups=("workspace-members",))


def _revoked_to_others() -> ACLSnapshot:
    """ACL 仍在，但授予对象已不含该用户（撤权后的当前 ACL 形态）。"""
    return ACLSnapshot(allowed_principals=("other-principal",))


class TestRevokedEvidenceVisibility:
    def test_revoked_ref_renders_placeholder_not_content(self) -> None:
        bundle = _bundle()
        ref_a, ref_b = bundle.evidence_refs[0], bundle.evidence_refs[1]

        def current_acl(ref: object) -> ACLSnapshot | None:
            # 撤权后：ref_a 的来源 ACL 已改授他人（当前 ACL 不再含该用户）
            return _revoked_to_others() if ref is ref_a else _granted()  # type: ignore[arg-type]

        views = resolve_evidence_views(bundle, _user_principal(), current_acl=current_acl)
        by_ref = {view.ref_id: view for view in views}
        revoked = by_ref[ref_a.ref_id]
        assert revoked.status == "evidence_access_revoked"
        assert revoked.reason == "not_in_acl"
        # 占位不携带任何内容（SQL/digest/locator 一概不可见）
        payload = revoked.as_dict()
        assert "sql" not in payload and "snapshot_digest" not in payload
        visible = by_ref[ref_b.ref_id]
        assert visible.status == "visible"
        assert visible.as_dict()["ref_type"] == "QueryReplay"

    def test_unknown_acl_fails_closed_to_placeholder(self) -> None:
        bundle = _bundle()
        ref_a, ref_b = bundle.evidence_refs[0], bundle.evidence_refs[1]

        def current_acl(ref: object) -> ACLSnapshot | None:
            return None if ref is ref_a else _granted()  # type: ignore[arg-type]

        views = resolve_evidence_views(bundle, _user_principal(), current_acl=current_acl)
        by_ref = {view.ref_id: view for view in views}
        assert by_ref[ref_a.ref_id].status == "evidence_access_revoked"
        assert by_ref[ref_a.ref_id].reason == "acl_unknown"
        assert by_ref[ref_b.ref_id].status == "visible"

    def test_no_silent_removal(self) -> None:
        """失权 Evidence 仍在结果集中占位——条目数不因撤权而减少。"""
        bundle = _bundle()

        def current_acl(ref: object) -> ACLSnapshot | None:
            return ACLSnapshot()  # type: ignore[arg-type]

        views = resolve_evidence_views(bundle, _user_principal(), current_acl=current_acl)
        assert len(views) == len(bundle.evidence_refs)
        assert all(v.status == "evidence_access_revoked" for v in views)


class TestAuditAndEvalChannels:
    def test_auditor_sees_revoked_evidence(self) -> None:
        bundle = _bundle()

        def current_acl(ref: object) -> ACLSnapshot | None:
            return ACLSnapshot()  # type: ignore[arg-type]

        auditor = EvidencePrincipal(
            kind=PrincipalKind.AUDITOR,
            acl_context=_user_principal().acl_context,
        )
        views = resolve_evidence_views(bundle, auditor, current_acl=current_acl)
        assert all(v.status == "visible" for v in views)
        # 内容载荷真实下发（ref 元数据 + 绑定字段），且条目数不变
        assert len(views) == len(bundle.evidence_refs)
        assert all(v.as_dict().get("ref_type") for v in views)
        assert views[0].as_dict().get("sql")

    def test_eval_recompute_channel_always_available(self) -> None:
        """ADR-006：系统复算通道与用户可见性解耦——ACL 全撤销也必须可用。"""
        bundle = _bundle()

        def current_acl(ref: object) -> ACLSnapshot | None:
            return None  # unknown → 用户侧 fail closed

        evaluator = EvidencePrincipal(
            kind=PrincipalKind.EVAL_RECOMPUTE,
            acl_context=_user_principal().acl_context,
        )
        views = resolve_evidence_views(bundle, evaluator, current_acl=current_acl)
        assert all(v.status == "visible" for v in views)


class TestReferenceOnlyServiceRejection:
    def test_wire_bundle_with_reference_only_fact_is_rejected(self) -> None:
        """服务级拒绝：Fact claim 绑定 reference_only ref 的 wire bundle
        不得进入 final 落账（ADR-003）。"""
        bundle = _bundle()
        raw = bundle.model_dump(mode="json")
        # wire 层把 reference_only 的 DocRef 挂到 Fact claim 上（模型层不可构造，
        # 只能经 wire 注入——正是服务级检查要挡的输入）
        raw["claims"][0]["evidence_refs"].append(raw["evidence_refs"][2])
        with pytest.raises(ClaimLevelViolationError):
            service_rejects_reference_only_fact(raw)

    def test_valid_bundle_passes_service_rejection(self) -> None:
        bundle = _bundle()
        assert service_rejects_reference_only_fact(bundle.model_dump(mode="json")) is True

    def test_reference_only_inference_claim_is_allowed(self) -> None:
        """reference_only 只支撑 Inference/Recommendation——服务级放行。"""
        bundle = _bundle()
        raw = bundle.model_dump(mode="json")
        doc_ref = raw["evidence_refs"][2]
        raw["claims"][0] = {
            "claim_type": "Inference",
            "claim_id": raw["claims"][0]["claim_id"],
            "answer_id": raw["claims"][0]["answer_id"],
            "created_at": raw["claims"][0]["created_at"],
            "updated_at": raw["claims"][0]["updated_at"],
            "evidence_refs": [],
            "supporting_inputs": [doc_ref],
            "contradicting_inputs": [],
        }
        assert service_rejects_reference_only_fact(raw) is True
