"""R2-A（NEW-1）：claim_id 字符集契约——空格/数字边界字符不得走私伪数字。

红队 cross-check 发现：含空格等字符的 claim_id 可把 release 表面外的伪造数字
marker 走私过 checker。冻结 charset：^[a-z0-9][a-z0-9.-]{0,62}[a-z0-9]$——
小写字母数字开头结尾，内部仅点/连字符/字母数字，最长 64，禁止空格。
三层同规：ClaimRecord 模型校验、ClaimRegistryService.register 服务拒绝
（机器码 reason=invalid_claim_id）、register 端点 422（见
tests/contract/api/test_claims_api.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.agents.claims import ClaimRecord, ClaimScope, ClaimStatus
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.tenant import TenantContext

_SCOPE = ClaimScope(
    mode="offline",
    model="reference-fixture",
    version="1",
    date="2026-09-05",
    corpus="factqa-v1",
    environment="offline-fixture",
)

_VALID_IDS = (
    "factqa-v1.accuracy",
    "a1",
    "0.42-bench",
    "a" * 64,
    "x-y.z.9",
)

_INVALID_IDS = (
    "fact qa v1",  # 空格——marker 走私向量本身
    "FactQA-v1",
    ".leading-dot",
    "trailing-dot.",
    "-leading-dash",
    "trailing-dash-",
    "under_score",
    "a",  # 单字符：charset 要求首尾都是字母数字，最短 2
    "a" * 65,
    "",
)


def _record(claim_id: str) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        statement="FactQA accuracy {{accuracy}}",
        scope=_SCOPE,
        status=ClaimStatus.PLANNED,
    )


class TestModelCharset:
    """ClaimRecord 是域层投影：无论来源（API/服务层/行反序列化）charset 一致。"""

    @pytest.mark.parametrize("claim_id", _VALID_IDS)
    def test_legitimate_ids_accepted(self, claim_id: str) -> None:
        assert _record(claim_id).claim_id == claim_id

    @pytest.mark.parametrize("claim_id", _INVALID_IDS)
    def test_illegal_ids_refused(self, claim_id: str) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            _record(claim_id)


@dataclass
class _RegisterSession:
    """register 路径触达的 session 面：scalar（重复查重）/add/flush。"""

    existing: object = None
    added: list[object] = field(default_factory=list)
    flush_calls: int = 0

    async def scalar(self, _statement: object) -> object:
        return self.existing

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flush_calls += 1


def _service(session: _RegisterSession, tmp_path) -> object:
    from zhiwei.agents.claims import ClaimRegistryService

    context = TenantContext(organization_id=uuid4(), workspace_id=uuid4())
    return ClaimRegistryService(
        cast(AsyncSession, session), context, PosixObjectStore(tmp_path / "objects")
    )


class TestServiceRegisterCharset:
    @pytest.mark.asyncio
    async def test_space_bearing_id_refused_with_machine_reason(
        self, tmp_path
    ) -> None:
        from zhiwei.agents.claims import ClaimIdInvalid

        session = _RegisterSession()
        service = _service(session, tmp_path)
        with pytest.raises(ClaimIdInvalid) as refused:
            await service.register(  # type: ignore[attr-defined]
                claim_id="fact qa v1",
                statement="FactQA accuracy {{accuracy}}",
                scope=_SCOPE,
            )
        assert getattr(refused.value, "reason", None) == "invalid_claim_id"
        assert session.added == [], "refusal must leave no partial row"

    @pytest.mark.asyncio
    async def test_legitimate_id_registers(self, tmp_path) -> None:
        session = _RegisterSession()
        service = _service(session, tmp_path)
        record = await service.register(  # type: ignore[attr-defined]
            claim_id="factqa-v1.accuracy",
            statement="FactQA accuracy {{accuracy}}",
            scope=_SCOPE,
        )
        assert record.status is ClaimStatus.PLANNED
        assert len(session.added) == 1
        assert session.flush_calls == 1
