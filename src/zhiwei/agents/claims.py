"""S9 Claim Registry：状态机、口径混写防线与模板填充。

specs/s9 §5 冻结语义（tests/contract/release/test_claim_registry_frozen.py）：
- 状态 planned/implemented/offline_verified/live_verified/retired 只能沿唯一
  方向升级；retired 是终态；跳级与回退一律拒绝；
- 证据升级只能由「已复核的 sealed artifact」驱动：evidence.seal_digest 必须
  与复核 digest 一致；live-production 口径的 claim 拒绝 fixture/offline 密封件
  （fixture/live 混写防线）；live_verified 只接受 live/shadow 模式证据；
- 模板变量只能由带 provenance 的 SealedValue 填充：裸字符串、非 sealed 来源、
  digest 与锚点不一致一律拒绝；statement 声明的变量缺失拒绝（不允许静默留白），
  显式 None 表示未绑定、保留 marker 交由 release checker 判定。

render_claim 的 digest 锚点解析：显式 verified_seal_digest（registry 复核后传入，
权威）优先，其次 claim 已绑定的 evidence.seal_digest；两者皆无（未绑定 claim 的
草稿渲染）时无法进行 digest 比对——服务层升级路径永远携带复核 digest，草稿渲染
不产生任何绑定结论（bound_value 只由 registry 服务落库）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.evals.runs import EvalFoundationService
from zhiwei.evals.sealing import SealVerificationError
from zhiwei.object_store.ports import ObjectStore
from zhiwei.persistence.models import ArtifactManifest, ClaimRegistryRow
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired

__all__ = [
    "ClaimAlreadyRegistered",
    "ClaimEvidence",
    "ClaimIdInvalid",
    "ClaimNotFound",
    "ClaimRecord",
    "ClaimRegistryService",
    "ClaimScope",
    "ClaimStatus",
    "ClaimUpgradeDenied",
    "SealedValue",
    "render_claim",
    "upgrade_claim",
]

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_TEMPLATE_VARIABLE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

# claim_id 字符集冻结（R2-A NEW-1）：小写字母数字开头结尾，内部仅点/连字符/
# 字母数字，最长 64。空格/大写/下划线一律拒绝——含空格的 id 曾把模板 marker
# 形态的伪数字走私过 release 表面，字符集在三层（模型/服务/端点）同规收口。
_CLAIM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}[a-z0-9]$")

# live-production 口径只认 live/shadow 密封件；offline 口径的升级只认 offline 模式
_LIVE_MODES = frozenset({"live", "shadow"})


class ClaimUpgradeDenied(RuntimeError):
    """claim 升级/填充被拒：跳级、回退、终态、证据缺失或口径混写。

    reason 是稳定机器码（服务层拒绝面的程序化判别用），消息是人类可读补充；
    缺省 None 保持既有 raise 点行为不变。
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class ClaimNotFound(LookupError):
    """目标 claim 不在显式租户作用域内（RLS 下跨租户同此语义）。"""


class ClaimAlreadyRegistered(ValueError):
    """同租户下 claim_id 重复注册（fail closed，不做静默幂等覆盖）。"""


class ClaimIdInvalid(ValueError):
    """claim_id 违反冻结字符集；reason 是稳定机器码，端点按其分支返回 422。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = "invalid_claim_id"


class ClaimStatus(StrEnum):
    """claim 公开状态机；retired 是唯一终态。"""

    PLANNED = "planned"
    IMPLEMENTED = "implemented"
    OFFLINE_VERIFIED = "offline_verified"
    LIVE_VERIFIED = "live_verified"
    RETIRED = "retired"


class ClaimScope(BaseModel):
    """claim 的口径边界：mode/model/version/date/corpus/environment 全部进比对。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str = Field(min_length=1)
    model: str = Field(min_length=1)
    version: str = Field(min_length=1)
    date: str = Field(min_length=1)
    corpus: str = Field(min_length=1)
    environment: str = Field(min_length=1)


class ClaimEvidence(BaseModel):
    """升级证据：指向一次已复核的 sealed eval run 及其密封 digest 与模式。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_run_id: UUID
    seal_digest: str
    artifact_manifest_id: UUID
    mode: str

    @field_validator("seal_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("seal_digest must be a lowercase sha256 digest")
        return value


class SealedValue(BaseModel):
    """模板变量的填充值：只能来自 sealed artifact（source 固定）并携带密封 digest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    source: str
    seal_digest: str

    @field_validator("seal_digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("seal_digest must be a lowercase sha256 digest")
        return value


class ClaimRecord(BaseModel):
    """claim 的域层投影：statement 模板 + 口径 + 状态 + 已绑定证据/绑定值。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    scope: ClaimScope
    status: ClaimStatus
    evidence: ClaimEvidence | None = None
    bound_value: str | None = None

    @field_validator("claim_id")
    @classmethod
    def _validate_claim_id(cls, value: str) -> str:
        # 模型层校验覆盖一切构造来源（API/服务层/行反序列化）——charset 不在
        # 端点单点把关，防止绕过 API 的写入路径重新打开走私向量。
        if _CLAIM_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "claim_id must match ^[a-z0-9][a-z0-9.-]{0,62}[a-z0-9]$ "
                "(lowercase alnum start/end; dot/dash/alnum inside; max 64)"
            )
        return value


# 手工升级边（无 artifact 参与）：登记实现、主动退役。任何随附证据都按
# 「未锚定证据」拒绝——证据只能走 evidence 边并由复核 digest 锚定。
_MANUAL_EDGES: frozenset[tuple[ClaimStatus, ClaimStatus]] = frozenset({
    (ClaimStatus.PLANNED, ClaimStatus.IMPLEMENTED),
    (ClaimStatus.PLANNED, ClaimStatus.RETIRED),
    (ClaimStatus.IMPLEMENTED, ClaimStatus.RETIRED),
    (ClaimStatus.OFFLINE_VERIFIED, ClaimStatus.RETIRED),
    (ClaimStatus.LIVE_VERIFIED, ClaimStatus.RETIRED),
})

# 证据升级边 → 允许的密封模式集合；未列组合（跳级/回退）一律拒绝。
_EVIDENCE_EDGES: Mapping[tuple[ClaimStatus, ClaimStatus], frozenset[str]] = {
    (ClaimStatus.IMPLEMENTED, ClaimStatus.OFFLINE_VERIFIED): frozenset({"offline"}),
    (ClaimStatus.OFFLINE_VERIFIED, ClaimStatus.LIVE_VERIFIED): _LIVE_MODES,
}

# bind_value 只接受 artifact-verified 态：绑定值必须能追溯到密封证据。
_BINDABLE_STATUSES = frozenset({ClaimStatus.OFFLINE_VERIFIED, ClaimStatus.LIVE_VERIFIED})


def upgrade_claim(
    record: ClaimRecord,
    evidence: ClaimEvidence | None,
    *,
    target: ClaimStatus,
    verified_seal_digest: str | None = None,
) -> ClaimRecord:
    """按冻结状态机升级 claim；所有拒绝路径抛 ClaimUpgradeDenied，不产生部分状态。"""
    if record.status is ClaimStatus.RETIRED:
        raise ClaimUpgradeDenied("retired claim is terminal")
    edge = (record.status, target)
    if edge in _EVIDENCE_EDGES:
        if evidence is None:
            raise ClaimUpgradeDenied(
                f"upgrade to {target.value} requires sealed artifact evidence"
            )
        # fixture/live 混写防线：live-production 口径拒绝一切非 live/shadow 密封件
        if record.scope.environment == "live-production" and evidence.mode not in _LIVE_MODES:
            raise ClaimUpgradeDenied(
                "live-production claim cannot be upgraded with offline evidence"
            )
        allowed_modes = _EVIDENCE_EDGES[edge]
        if evidence.mode not in allowed_modes:
            raise ClaimUpgradeDenied(
                f"upgrade to {target.value} requires "
                f"{sorted(allowed_modes)} mode evidence, got {evidence.mode!r}"
            )
        # 复核 digest 由服务层从密封件独立复算传入；不匹配即拒绝（不可逃逸）
        if verified_seal_digest is not None and evidence.seal_digest != verified_seal_digest:
            raise ClaimUpgradeDenied(
                "evidence seal digest does not match the independently verified artifact"
            )
        return record.model_copy(update={"status": target, "evidence": evidence})
    if edge in _MANUAL_EDGES:
        if evidence is not None or verified_seal_digest is not None:
            raise ClaimUpgradeDenied("manual upgrade does not accept unanchored evidence")
        return record.model_copy(update={"status": target})
    raise ClaimUpgradeDenied(
        f"claim upgrade {record.status.value} -> {target.value} is not allowed"
    )


def render_claim(
    record: ClaimRecord,
    values: Mapping[str, SealedValue | str | None],
    *,
    verified_seal_digest: str | None = None,
) -> str:
    """把 sealed 值填入 statement 模板；缺变量/裸值/错误来源/digest 不符一律拒绝。

    values 的类型面按冻结契约接受 str（调用方可能传裸值），运行时拒绝——旁路
    填充在值检查处拦截，而不是靠类型系统。

    锚点为 None（未绑定 claim 的草稿渲染）时无法比对 digest：本函数不产生绑定
    结论，registry 服务在落 bound_value 前永远携带复核后的密封 digest。
    """
    declared = _TEMPLATE_VARIABLE.findall(record.statement)
    missing = sorted({name for name in declared if name not in values})
    if missing:
        # fail closed：statement 声明的变量缺失时不允许静默留白
        raise ClaimUpgradeDenied(f"statement variables missing from values: {missing}")
    anchor = verified_seal_digest
    if anchor is None and record.evidence is not None:
        anchor = record.evidence.seal_digest

    def _fill(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values[name]
        if value is None:
            # 显式未绑定：保留 marker，交由 release checker 判定
            return match.group(0)
        if not isinstance(value, SealedValue):
            # 旁路填充拒绝：模板变量不接受无 provenance 的裸值
            raise ClaimUpgradeDenied("template variables only accept SealedValue provenance")
        if value.source != "sealed_artifact":
            raise ClaimUpgradeDenied(
                f"template values must come from sealed artifacts, got source {value.source!r}"
            )
        if anchor is not None and value.seal_digest != anchor:
            raise ClaimUpgradeDenied(
                "sealed value digest does not match the claim's verified seal"
            )
        return value.value

    return _TEMPLATE_VARIABLE.sub(_fill, record.statement)


class ClaimRegistryService:
    """Claim Registry 的 PostgreSQL 应用服务：注册、复核驱动的升级与租户内检索。"""

    def __init__(
        self,
        session: AsyncSession | None,
        context: TenantContext | None,
        store: ObjectStore | None,
    ) -> None:
        if session is None:
            raise TenantContextRequired("claim registry requires a database session")
        if context is None or context.workspace_id is None:
            raise TenantContextRequired("claim registry requires workspace context")
        if store is None:
            raise TenantContextRequired("claim registry requires an object store")
        self._session = session
        self._context = context
        # 复核必须从 object store 重新取件复算，不信任调用方转述的任何验证结论
        self._evals = EvalFoundationService(session, context, store)

    async def register(
        self, *, claim_id: str, statement: str, scope: ClaimScope
    ) -> ClaimRecord:
        # charset 先于一切会话 I/O：拒绝路径不触数据库（端点测试依赖该次序），
        # 且重复查重前就拒绝可避免给非法 id 做无意义的行锁/查询。
        if _CLAIM_ID_PATTERN.fullmatch(claim_id) is None:
            raise ClaimIdInvalid(
                f"claim_id {claim_id!r} violates the frozen charset "
                "^[a-z0-9][a-z0-9.-]{0,62}[a-z0-9]$"
            )
        existing = await self._session.scalar(
            select(ClaimRegistryRow.id).where(
                ClaimRegistryRow.organization_id == self._context.organization_id,
                ClaimRegistryRow.workspace_id == self._context.workspace_id,
                ClaimRegistryRow.claim_id == claim_id,
            )
        )
        if existing is not None:
            raise ClaimAlreadyRegistered(f"claim {claim_id!r} is already registered")
        record = ClaimRecord(
            claim_id=claim_id, statement=statement, scope=scope, status=ClaimStatus.PLANNED
        )
        now = utc_now()
        self._session.add(
            ClaimRegistryRow(
                id=new_id(),
                organization_id=self._context.organization_id,
                workspace_id=self._context.workspace_id,
                claim_id=record.claim_id,
                statement=record.statement,
                scope=record.scope.model_dump(mode="json"),
                status=record.status.value,
                bound_value=None,
                evidence=None,
                schema_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()
        return record

    async def get(self, claim_id: str) -> ClaimRecord:
        return self._record(await self._load(claim_id, for_update=False))

    async def list(self) -> list[ClaimRecord]:
        rows = (
            await self._session.scalars(
                select(ClaimRegistryRow)
                .where(
                    ClaimRegistryRow.organization_id == self._context.organization_id,
                    ClaimRegistryRow.workspace_id == self._context.workspace_id,
                )
                .order_by(ClaimRegistryRow.created_at, ClaimRegistryRow.id)
            )
        ).all()
        return [self._record(row) for row in rows]

    async def upgrade(
        self,
        claim_id: str,
        *,
        target: ClaimStatus,
        eval_run_id: UUID | None,
    ) -> ClaimRecord:
        """升级 claim：eval_run_id 提供时从 object store 复算密封件再驱动状态机。"""
        row = await self._load(claim_id, for_update=True)
        record = self._record(row)
        evidence: ClaimEvidence | None = None
        verified_seal_digest: str | None = None
        if eval_run_id is not None:
            artifact = await self._evals.verify_sealed(eval_run_id)
            seal_manifest = await self._session.scalar(
                select(ArtifactManifest).where(
                    ArtifactManifest.organization_id == self._context.organization_id,
                    ArtifactManifest.workspace_id == self._context.workspace_id,
                    ArtifactManifest.owner_resource_type == "eval_run",
                    ArtifactManifest.owner_resource_id == eval_run_id,
                )
            )
            if seal_manifest is None:
                raise SealVerificationError("seal manifest is missing")
            verified_seal_digest = seal_manifest.content_digest
            evidence = ClaimEvidence(
                eval_run_id=eval_run_id,
                seal_digest=verified_seal_digest,
                artifact_manifest_id=seal_manifest.id,
                mode=artifact.mode,
            )
        upgraded = upgrade_claim(
            record,
            evidence,
            target=target,
            verified_seal_digest=verified_seal_digest,
        )
        row.status = upgraded.status.value
        row.evidence = (
            upgraded.evidence.model_dump(mode="json") if upgraded.evidence is not None else None
        )
        row.updated_at = utc_now()
        await self._session.flush()
        return upgraded

    async def bind_value(self, claim_id: str, value: str, seal_digest: str) -> ClaimRecord:
        """把 render_claim 产出的绑定值落库（bound_value 的唯一服务入口）。

        fail closed：claim 必须在租户内、已到 verified 态、证据在场且证据
        seal_digest 与调用方提供的复核 digest 一致——绑定值不允许脱离密封证据
        单独写入（否则 release 表面数字可以绕过 artifact 直接落库）。落库走
        0015 迁移显式授权的 bound_value 列级 UPDATE 面。
        """
        if not isinstance(value, str) or not value.strip():
            raise ClaimUpgradeDenied(
                "bind_value requires a non-empty string value",
                reason="value_empty",
            )
        row = await self._load(claim_id, for_update=True)
        record = self._record(row)
        if record.status not in _BINDABLE_STATUSES:
            raise ClaimUpgradeDenied(
                f"claim status {record.status.value!r} is not artifact-verified; "
                "bound_value requires offline_verified or live_verified",
                reason="status_not_verified",
            )
        if record.evidence is None:
            raise ClaimUpgradeDenied(
                "verified claim has no bound evidence to anchor the value",
                reason="evidence_missing",
            )
        if record.evidence.seal_digest != seal_digest:
            raise ClaimUpgradeDenied(
                "seal digest does not match the claim's verified evidence",
                reason="seal_digest_mismatch",
            )
        row.bound_value = value
        row.updated_at = utc_now()
        await self._session.flush()
        return record.model_copy(update={"bound_value": value})

    async def _load(self, claim_id: str, *, for_update: bool) -> ClaimRegistryRow:
        statement = select(ClaimRegistryRow).where(
            ClaimRegistryRow.organization_id == self._context.organization_id,
            ClaimRegistryRow.workspace_id == self._context.workspace_id,
            ClaimRegistryRow.claim_id == claim_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            raise ClaimNotFound("claim is missing from tenant scope")
        return row

    @staticmethod
    def _record(row: ClaimRegistryRow) -> ClaimRecord:
        return ClaimRecord(
            claim_id=row.claim_id,
            statement=row.statement,
            scope=ClaimScope.model_validate(row.scope),
            status=ClaimStatus(row.status),
            evidence=(
                ClaimEvidence.model_validate(row.evidence) if row.evidence is not None else None
            ),
            bound_value=row.bound_value,
        )
