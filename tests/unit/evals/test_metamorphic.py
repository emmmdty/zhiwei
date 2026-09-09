"""S9-T3 RED: metamorphic suite 注册 API 与 numeric-risk-v1 派生。

事实源：specs/s9 §4（internal frozen + blind holdout + external diagnostic +
metamorphic/fault injection）、S9 plan Task 3。

契约要点：

- 派生完全确定性：metamorphic 单位 id 是源单位 id 的纯函数（不用 RNG/seed）。
  源 suite 冻结 → 同一派生逐字节可复现；任何随机性都会破坏「同一冻结 suite
  派生同一套 metamorphic 单位」的可复算契约。
- scope 标签 `metamorphic:*`、claim_status `diagnostic-only`：metamorphic 结果
  只是稳健性诊断，永不升级公开质量 claim（specs/s9 §4 naked diagnostic 只披露）。
- 派生源是冻结 suite 的 registered units（numeric-risk-v1 经 CHECKSUMS 校验后
  读取），evals/ 冻结资产本身零修改。
"""

from __future__ import annotations

import pytest

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.evals.external.metamorphic import (
    LABEL_FLIP,
    METAMORPHIC_SUITE_NAMES,
    NUMERIC_RISK_METAMORPHIC_V1,
    PERTURB,
    derive_metamorphic_units,
    register_metamorphic_suite,
    resolve_metamorphic_suite,
)
from zhiwei.evals.risk_suites import NUMERIC_RISK_V1, resolve_risk_suite


def _source_units() -> tuple[RegisteredUnit, ...]:
    return (
        RegisteredUnit(sample_id="planted:P1-001", unit_id="trend:云梯-企业版"),
        RegisteredUnit(sample_id="distractor:D1", unit_id="baseline_deviation:C1"),
    )


class TestDerivation:
    def test_derive_is_deterministic(self) -> None:
        first = derive_metamorphic_units(_source_units(), (LABEL_FLIP, PERTURB))
        second = derive_metamorphic_units(_source_units(), (LABEL_FLIP, PERTURB))
        assert canonical_json([u.model_dump() for u in first]) == canonical_json(
            [u.model_dump() for u in second]
        )
        assert digest_bytes(canonical_json([u.model_dump() for u in first])) == digest_bytes(
            canonical_json([u.model_dump() for u in second])
        )

    def test_derive_applies_every_transform_per_unit(self) -> None:
        units = derive_metamorphic_units(_source_units()[:1], (LABEL_FLIP, PERTURB))
        assert [u.sample_id for u in units] == [
            "metamorphic:label-flip:planted:P1-001",
            "metamorphic:perturb:planted:P1-001",
        ]
        assert [u.unit_id for u in units] == [
            "label-flip:trend:云梯-企业版",
            "perturb:trend:云梯-企业版",
        ]

    def test_derive_keys_off_unit_identity(self) -> None:
        # 同一源单位 → 同一派生单位；不同源单位 → 不同派生单位（id 纯函数）。
        flipped = derive_metamorphic_units(_source_units(), (LABEL_FLIP,))
        assert flipped[0] == derive_metamorphic_units(_source_units()[:1], (LABEL_FLIP,))[0]
        assert flipped[0].sample_id != flipped[1].sample_id

    def test_derived_units_are_registered_domain_units(self) -> None:
        for unit in derive_metamorphic_units(_source_units(), (LABEL_FLIP, PERTURB)):
            assert isinstance(unit, RegisteredUnit)
            assert unit.sample_id and unit.unit_id


class TestRegistry:
    def test_numeric_risk_metamorphic_is_registered(self) -> None:
        assert NUMERIC_RISK_METAMORPHIC_V1 in METAMORPHIC_SUITE_NAMES

    def test_resolve_unknown_suite_fails_closed(self) -> None:
        with pytest.raises(LookupError, match="未知 metamorphic suite"):
            resolve_metamorphic_suite("numeric-risk-v2-metamorphic")

    def test_duplicate_registration_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="重复注册"):
            register_metamorphic_suite(
                NUMERIC_RISK_METAMORPHIC_V1,
                NUMERIC_RISK_V1,
                (LABEL_FLIP,),
                source_units=_source_units(),
            )

    def test_registered_suite_is_derived_from_frozen_suite(self) -> None:
        suite = resolve_metamorphic_suite(NUMERIC_RISK_METAMORPHIC_V1)
        source = resolve_risk_suite(NUMERIC_RISK_V1)
        assert suite.source_suite == NUMERIC_RISK_V1
        assert suite.transforms == ("label-flip", "perturb")
        assert suite.source_digest == source.asset_digest
        assert len(suite.units) == 2 * len(source.units)
        derived_keys = {(u.sample_id, u.unit_id) for u in suite.units}
        for unit in source.units:
            assert (f"metamorphic:label-flip:{unit.sample_id}", f"label-flip:{unit.unit_id}") in derived_keys
            assert (f"metamorphic:perturb:{unit.sample_id}", f"perturb:{unit.unit_id}") in derived_keys

    def test_scope_label_is_metamorphic(self) -> None:
        suite = resolve_metamorphic_suite(NUMERIC_RISK_METAMORPHIC_V1)
        assert suite.scope == f"metamorphic:{NUMERIC_RISK_METAMORPHIC_V1}"

    def test_metamorphic_results_never_upgrade_public_claims(self) -> None:
        suite = resolve_metamorphic_suite(NUMERIC_RISK_METAMORPHIC_V1)
        assert suite.claim_status == "diagnostic-only"
