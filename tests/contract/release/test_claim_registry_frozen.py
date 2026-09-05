"""S9 冻结契约：Claim Registry 状态机与模板填充（A 档，S9-T4/T5）。

Claim 状态 planned/implemented/offline_verified/live_verified/retired 的升级只能由
已验证的 sealed artifact 驱动；fixture/live 口径混写必须拒绝；模板变量只能由
sealed artifact 填充（带 provenance 的 SealedValue），任何旁路填充拒绝。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from zhiwei.agents.claims import (
    ClaimEvidence,
    ClaimRecord,
    ClaimScope,
    ClaimStatus,
    ClaimUpgradeDenied,
    SealedValue,
    render_claim,
    upgrade_claim,
)

_SHA = "sha256:" + "1" * 64


def _scope(*, environment: str = "offline-fixture", date: str = "2026-09-05") -> ClaimScope:
    return ClaimScope(
        mode="offline",
        model="reference-fixture",
        version="1",
        date=date,
        corpus="factqa-v1",
        environment=environment,
    )


def _claim(
    *,
    status: ClaimStatus = ClaimStatus.PLANNED,
    environment: str = "offline-fixture",
) -> ClaimRecord:
    return ClaimRecord(
        claim_id="factqa-v1.accuracy",
        statement="FactQA accuracy {{accuracy}}（{{environment}}）",
        scope=_scope(environment=environment),
        status=status,
    )


def _evidence(*, mode: str = "offline", seal_digest: str = _SHA) -> ClaimEvidence:
    return ClaimEvidence(
        eval_run_id=uuid4(),
        seal_digest=seal_digest,
        artifact_manifest_id=uuid4(),
        mode=mode,
    )


class TestClaimUpgrades:
    def test_planned_to_implemented_without_artifact(self) -> None:
        upgraded = upgrade_claim(_claim(), None, target=ClaimStatus.IMPLEMENTED)
        assert upgraded.status == ClaimStatus.IMPLEMENTED

    def test_planned_cannot_jump_to_offline_verified(self) -> None:
        with pytest.raises(ClaimUpgradeDenied):
            upgrade_claim(
                _claim(), _evidence(), target=ClaimStatus.OFFLINE_VERIFIED
            )

    def test_implemented_to_offline_verified_with_seal(self) -> None:
        upgraded = upgrade_claim(
            _claim(status=ClaimStatus.IMPLEMENTED),
            _evidence(),
            target=ClaimStatus.OFFLINE_VERIFIED,
        )
        assert upgraded.status == ClaimStatus.OFFLINE_VERIFIED
        assert upgraded.evidence is not None

    def test_seal_digest_mismatch_refused(self) -> None:
        # 升级绑定要求 evidence.seal_digest 与复核通过的密封工件一致；不匹配即拒绝。
        with pytest.raises(ClaimUpgradeDenied):
            upgrade_claim(
                _claim(status=ClaimStatus.IMPLEMENTED),
                _evidence(seal_digest="sha256:" + "9" * 64),
                target=ClaimStatus.OFFLINE_VERIFIED,
                verified_seal_digest=_SHA,
            )

    def test_fixture_seal_cannot_verify_live_claim(self) -> None:
        # fixture/live 混写防线：live 口径的 claim 不能用 fixture/offline 密封件升级。
        with pytest.raises(ClaimUpgradeDenied):
            upgrade_claim(
                _claim(
                    status=ClaimStatus.IMPLEMENTED, environment="live-production"
                ),
                _evidence(mode="offline"),
                target=ClaimStatus.LIVE_VERIFIED,
            )

    def test_live_verified_requires_live_or_shadow_seal(self) -> None:
        with pytest.raises(ClaimUpgradeDenied):
            upgrade_claim(
                _claim(status=ClaimStatus.OFFLINE_VERIFIED),
                _evidence(mode="offline"),
                target=ClaimStatus.LIVE_VERIFIED,
            )

    def test_live_verified_with_shadow_seal(self) -> None:
        upgraded = upgrade_claim(
            _claim(status=ClaimStatus.OFFLINE_VERIFIED),
            _evidence(mode="shadow"),
            target=ClaimStatus.LIVE_VERIFIED,
        )
        assert upgraded.status == ClaimStatus.LIVE_VERIFIED

    def test_retired_is_terminal(self) -> None:
        for target in (
            ClaimStatus.IMPLEMENTED,
            ClaimStatus.OFFLINE_VERIFIED,
            ClaimStatus.LIVE_VERIFIED,
        ):
            with pytest.raises(ClaimUpgradeDenied):
                upgrade_claim(
                    _claim(status=ClaimStatus.RETIRED), None, target=target
                )

    def test_status_regression_refused(self) -> None:
        with pytest.raises(ClaimUpgradeDenied):
            upgrade_claim(
                _claim(status=ClaimStatus.OFFLINE_VERIFIED),
                None,
                target=ClaimStatus.IMPLEMENTED,
            )


class TestTemplateFilling:
    def _bound_claim(self) -> ClaimRecord:
        # 渲染校验锚定 claim 已绑定的 evidence.seal_digest：fixture 必须先绑定证据，
        # 否则 digest 不匹配不可判定（设计方 RED 修订：原夹具缺 evidence 绑定）。
        return _claim(status=ClaimStatus.OFFLINE_VERIFIED).model_copy(
            update={"evidence": _evidence()}
        )

    def _sealed_value(self, value: str = "0.95") -> SealedValue:
        return SealedValue(
            value=value, source="sealed_artifact", seal_digest=_SHA
        )

    def test_render_from_sealed_value(self) -> None:
        rendered = render_claim(
            self._bound_claim(), {"accuracy": self._sealed_value(), "environment": None}
        )
        assert "0.95" in rendered
        assert "{{accuracy}}" not in rendered

    def test_plain_string_value_refused(self) -> None:
        # 旁路填充拒绝：模板变量不接受无 provenance 的裸值。
        with pytest.raises(ClaimUpgradeDenied):
            render_claim(
                self._bound_claim(), {"accuracy": "0.95", "environment": None}
            )

    def test_wrong_source_refused(self) -> None:
        with pytest.raises(ClaimUpgradeDenied):
            render_claim(
                self._bound_claim(),
                {
                    "accuracy": SealedValue(
                        value="0.95", source="hand_written", seal_digest=_SHA
                    ),
                    "environment": None,
                },
            )

    def test_seal_digest_mismatch_refused(self) -> None:
        with pytest.raises(ClaimUpgradeDenied):
            render_claim(
                self._bound_claim(),
                {
                    "accuracy": SealedValue(
                        value="0.95",
                        source="sealed_artifact",
                        seal_digest="sha256:" + "9" * 64,
                    ),
                    "environment": None,
                },
            )

    def test_missing_variable_refused(self) -> None:
        # fail closed：statement 声明的变量缺失时不允许静默留白。
        with pytest.raises(ClaimUpgradeDenied):
            render_claim(self._bound_claim(), {"environment": None})

    def test_unbound_variable_left_as_marker(self) -> None:
        # environment 传 None 表示「未绑定」：保留 marker，交由 release checker 判定。
        rendered = render_claim(
            self._bound_claim(), {"accuracy": self._sealed_value(), "environment": None}
        )
        assert "{{environment}}" in rendered
