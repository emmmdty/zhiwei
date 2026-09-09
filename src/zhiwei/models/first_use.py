"""S3 models: unverified endpoint 首次使用留痕（ADR-011 §6）。

经 env 或管理台引入的 endpoint 在首次使用时必须写 canonical event + audit：
base_url、trust tier、network zone、classification ceiling、声明人。不允许静默
换 endpoint。models 层不直接依赖 PG/FastAPI——写入器经 `EndpointFirstUseSink`
port 注入，生产组装见 persistence.model_first_use 与 workflows 活动层工厂。

「首次」判定不在本层做内存缓存：跨进程/跨 run 的去重必须由 sink 的事件流查重
承担（确定性 + 并发安全），resolver 每次解析到 unverified 档都如实上报，由
sink 决定是否为新记录。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from zhiwei.models.contracts import (
    ClassificationCeiling,
    EndpointProfile,
    NetworkZone,
    TrustTier,
)
from zhiwei.models.profiles import resolve_default_endpoint

FIRST_USE_EVENT_TYPE = "models.endpoint.registered"
FIRST_USE_PAYLOAD_SCHEMA_VERSION = 1


class EndpointFirstUseDeclaration(BaseModel):
    """一次 unverified endpoint 使用的事实快照（ADR-011 §6 全字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    trust_tier: TrustTier
    network_zone: NetworkZone
    classification_ceiling: ClassificationCeiling
    declared_by: str

    @classmethod
    def from_endpoint_profile(
        cls, profile: EndpointProfile, *, declared_by: str
    ) -> EndpointFirstUseDeclaration:
        return cls(
            base_url=profile.base_url,
            trust_tier=profile.trust_tier,
            network_zone=profile.network_zone,
            classification_ceiling=profile.classification_ceiling,
            declared_by=declared_by,
        )


class EndpointFirstUsePayload(BaseModel):
    """canonical event payload schema（schema registry 注册用，JSON 值域）。"""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    trust_tier: str
    network_zone: str
    classification_ceiling: str
    declared_by: str


def first_use_payload(declaration: EndpointFirstUseDeclaration) -> dict[str, str]:
    return {
        "base_url": declaration.base_url,
        "trust_tier": declaration.trust_tier.value,
        "network_zone": declaration.network_zone.value,
        "classification_ceiling": declaration.classification_ceiling.value,
        "declared_by": declaration.declared_by,
    }


def first_use_idempotency_key(declaration: EndpointFirstUseDeclaration) -> str:
    """per base_url 的确定性幂等键：同 endpoint 重试零新行（run 内幂等由 UoW 承担）。

    跨 run 去重不靠它——canonical event 的幂等键作用域是 (org, workspace, run)，
    跨 run 由 sink 的事件流查重 + advisory lock 判定。
    """
    digest = hashlib.sha256(declaration.base_url.encode("utf-8")).hexdigest()
    return f"{FIRST_USE_EVENT_TYPE}:sha256:{digest}"


@runtime_checkable
class EndpointFirstUseSink(Protocol):
    """unverified endpoint 留痕写入器 port（生产实现见 persistence.model_first_use）。

    返回 True 表示本次调用创建了一条记录（首次使用）；False 表示该 endpoint 在
    本组织已有记录。实现必须原子：canonical event + audit 同事务，且并发下不重。
    """

    async def record_first_use(
        self, declaration: EndpointFirstUseDeclaration, *, run_id: UUID
    ) -> bool: ...


class AuditedEndpointResolver:
    """模型请求路径的默认 endpoint 解析入口（ADR-011 §2 优先级 + §6 首次留痕）。

    env override（OPENAI_BASE_URL 等）优先于 endpoints.yaml 的 default_endpoint_id。
    解析结果落入 unverified 档时经 sink 留痕——sink 失败异常原样上抛，请求路径
    随之中止：不允许静默使用未经留痕的 unverified endpoint（fail closed）。
    """

    __slots__ = ("_declared_by", "_endpoints_path", "_env_overrides", "_sink")

    def __init__(
        self,
        sink: EndpointFirstUseSink,
        *,
        endpoints_path: Path,
        env_overrides: Mapping[str, str] | None = None,
        declared_by: str = "operator:env-override",
    ) -> None:
        self._sink = sink
        self._endpoints_path = endpoints_path
        self._env_overrides = env_overrides or {}
        self._declared_by = declared_by

    async def resolve_default(self, *, run_id: UUID) -> EndpointProfile:
        """Resolve the deployment default endpoint and audit unverified first use."""
        endpoint = resolve_default_endpoint(dict(self._env_overrides), self._endpoints_path)
        if endpoint.trust_tier == TrustTier.UNVERIFIED:
            declaration = EndpointFirstUseDeclaration.from_endpoint_profile(
                endpoint, declared_by=self._declared_by
            )
            await self._sink.record_first_use(declaration, run_id=run_id)
        return endpoint
