"""S9 验收缺陷修复回归：ClaimRegistryService.bind_value 的 fail-closed 绑定入口。

缺陷：bound_value 此前没有任何服务入口可写，seed 脚本用直连列 UPDATE 绕过
服务层。bind_value 是唯一绑定入口：verified 态 + 证据在场 + 证据 seal_digest
与复核 digest 一致才允许写入——绑定值不允许脱离密封证据单独落库。

单元层用最小 session 替身覆盖服务级规则（PG 集成路径由 tests/integration/release
的既有套件覆盖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.agents.claims import (
    ClaimEvidence,
    ClaimNotFound,
    ClaimRecord,
    ClaimRegistryService,
    ClaimScope,
    ClaimStatus,
    ClaimUpgradeDenied,
)
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.tenant import TenantContext
from zhiwei.release.templates import render_release_surface

SEAL_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def _scope() -> dict[str, object]:
    return ClaimScope(
        mode="offline",
        model="reference-fixture",
        version="1",
        date="2026-09-05",
        corpus="factqa-v1",
        environment="offline-fixture",
    ).model_dump(mode="json")


@dataclass
class _FakeRow:
    """bind_value 触及的 ClaimRegistryRow 投影：_load 读出、_record 投影、原地写。"""

    claim_id: str
    statement: str
    scope: dict[str, object]
    status: str
    evidence: dict[str, object] | None
    bound_value: str | None
    updated_at: datetime | None = None


@dataclass
class _FakeSession:
    row: _FakeRow | None
    flush_calls: int = field(default=0)

    async def scalar(self, _statement: object) -> _FakeRow | None:
        return self.row

    async def flush(self) -> None:
        self.flush_calls += 1


def _evidence() -> dict[str, object]:
    return ClaimEvidence(
        eval_run_id=uuid4(),
        seal_digest=SEAL_DIGEST,
        artifact_manifest_id=uuid4(),
        mode="offline",
    ).model_dump(mode="json")


def _service(row: _FakeRow | None, tmp_path: Path) -> tuple[ClaimRegistryService, _FakeSession]:
    context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
    session = _FakeSession(row)
    # session 替身只实现 bind_value 路径触达的 scalar/flush 面
    registry = ClaimRegistryService(
        cast(AsyncSession, session), context, PosixObjectStore(tmp_path / "objects")
    )
    return registry, session


def _verified_row(status: ClaimStatus = ClaimStatus.OFFLINE_VERIFIED) -> _FakeRow:
    return _FakeRow(
        claim_id="factqa-v1.accuracy",
        statement="FactQA accuracy {{accuracy}}",
        scope=_scope(),
        status=status.value,
        evidence=_evidence(),
        bound_value=None,
    )


@pytest.mark.asyncio
class TestBindValueRules:
    async def test_digest_match_binds_and_writes_row(self, tmp_path: Path) -> None:
        service, session = _service(_verified_row(), tmp_path)
        record = await service.bind_value("factqa-v1.accuracy", "0.95", SEAL_DIGEST)
        assert record.bound_value == "0.95"
        assert record.status is ClaimStatus.OFFLINE_VERIFIED
        assert session.row is not None
        assert session.row.bound_value == "0.95"
        assert session.row.updated_at is not None
        assert session.flush_calls == 1

    async def test_seal_digest_mismatch_refused_and_row_untouched(
        self, tmp_path: Path
    ) -> None:
        service, session = _service(_verified_row(), tmp_path)
        with pytest.raises(ClaimUpgradeDenied) as denied:
            await service.bind_value("factqa-v1.accuracy", "0.95", OTHER_DIGEST)
        assert getattr(denied.value, "reason", None) == "seal_digest_mismatch"
        assert session.row is not None
        assert session.row.bound_value is None

    async def test_planned_claim_refused(self, tmp_path: Path) -> None:
        service, session = _service(_verified_row(ClaimStatus.PLANNED), tmp_path)
        with pytest.raises(ClaimUpgradeDenied) as denied:
            await service.bind_value("factqa-v1.accuracy", "0.95", SEAL_DIGEST)
        assert getattr(denied.value, "reason", None) == "status_not_verified"
        assert session.row is not None
        assert session.row.bound_value is None

    async def test_implemented_claim_refused(self, tmp_path: Path) -> None:
        service, _session = _service(_verified_row(ClaimStatus.IMPLEMENTED), tmp_path)
        with pytest.raises(ClaimUpgradeDenied) as denied:
            await service.bind_value("factqa-v1.accuracy", "0.95", SEAL_DIGEST)
        assert getattr(denied.value, "reason", None) == "status_not_verified"

    async def test_retired_claim_refused(self, tmp_path: Path) -> None:
        service, _session = _service(_verified_row(ClaimStatus.RETIRED), tmp_path)
        with pytest.raises(ClaimUpgradeDenied) as denied:
            await service.bind_value("factqa-v1.accuracy", "0.95", SEAL_DIGEST)
        assert getattr(denied.value, "reason", None) == "status_not_verified"

    async def test_verified_claim_without_evidence_refused(self, tmp_path: Path) -> None:
        row = _verified_row()
        row.evidence = None
        service, _session = _service(row, tmp_path)
        with pytest.raises(ClaimUpgradeDenied) as denied:
            await service.bind_value("factqa-v1.accuracy", "0.95", SEAL_DIGEST)
        assert getattr(denied.value, "reason", None) == "evidence_missing"

    async def test_unknown_claim_refused(self, tmp_path: Path) -> None:
        service, _session = _service(None, tmp_path)
        with pytest.raises(ClaimNotFound):
            await service.bind_value("factqa-v1.accuracy", "0.95", SEAL_DIGEST)

    async def test_empty_value_refused(self, tmp_path: Path) -> None:
        service, session = _service(_verified_row(), tmp_path)
        with pytest.raises(ClaimUpgradeDenied) as denied:
            await service.bind_value("factqa-v1.accuracy", "   ", SEAL_DIGEST)
        assert getattr(denied.value, "reason", None) == "value_empty"
        assert session.row is not None
        assert session.row.bound_value is None


@pytest.mark.asyncio
class TestBoundValueFeedsReleaseSurface:
    async def test_render_release_surface_fills_after_bind_value(self, tmp_path: Path) -> None:
        service, _session = _service(_verified_row(), tmp_path)
        bound = await service.bind_value("factqa-v1.accuracy", "0.95", SEAL_DIGEST)
        registry: dict[str, ClaimRecord] = {bound.claim_id: bound}
        rendered = render_release_surface(
            "FactQA accuracy {{claim:factqa-v1.accuracy}}", registry
        )
        assert rendered == "FactQA accuracy 0.95"
