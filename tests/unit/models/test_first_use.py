"""S3 models: unverified endpoint 首次使用留痕的域层契约（ADR-011 §6）。

冻结安全契约（tests/security/model_egress/test_endpoint_trust.py）覆盖信任档与
门禁；本文件钉住留痕语义：resolver 只对 unverified 档上报、上报内容是 ADR-011
§6 全字段、每次解析如实上报（去重责任在 sink 的持久层）、sink 失败不静默。
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from zhiwei.models.contracts import TrustTier
from zhiwei.models.first_use import (
    AuditedEndpointResolver,
    EndpointFirstUseDeclaration,
    EndpointFirstUseSink,
    first_use_idempotency_key,
    first_use_payload,
)
from zhiwei.models.profiles import (
    EndpointRegistry,
    resolve_default_endpoint,
)

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

# loopback 端口 9（discard）只作为 URL 标识使用，测试不真正连接。
_UNREGISTERED_URL = "http://127.0.0.1:9/v1"

_ENV_OVERRIDE = {
    "OPENAI_BASE_URL": _UNREGISTERED_URL,
    "OPENAI_MODEL": "qwen3-32b",
    "OPENAI_API_KEY": "test-key",
}


class _FakeSink:
    """记录调用并按 base_url 模拟「已见集合」——生产去重在 sink 持久层。"""

    def __init__(self) -> None:
        self.declarations: list[EndpointFirstUseDeclaration] = []
        self.run_ids: list[UUID] = []
        self.results: list[bool] = []
        self._seen: set[str] = set()
        self.fail_next = False

    async def record_first_use(
        self, declaration: EndpointFirstUseDeclaration, *, run_id: UUID
    ) -> bool:
        if self.fail_next:
            raise RuntimeError("sink unavailable")
        self.declarations.append(declaration)
        self.run_ids.append(run_id)
        if declaration.base_url in self._seen:
            self.results.append(False)
            return False
        self._seen.add(declaration.base_url)
        self.results.append(True)
        return True


def _resolver(
    sink: _FakeSink,
    *,
    env_overrides: dict[str, str] | None = None,
    declared_by: str = "operator:env-override",
) -> AuditedEndpointResolver:
    return AuditedEndpointResolver(
        sink,
        endpoints_path=CONFIG_DIR / "providers" / "endpoints.yaml",
        env_overrides=env_overrides,
        declared_by=declared_by,
    )


class TestResolverAuditsUnverifiedUse:
    @pytest.mark.asyncio
    async def test_env_override_first_use_reports_full_declaration(self) -> None:
        sink = _FakeSink()
        run_id = uuid4()
        resolver = _resolver(sink, env_overrides=dict(_ENV_OVERRIDE))
        endpoint = await resolver.resolve_default(run_id=run_id)

        assert endpoint.base_url == _UNREGISTERED_URL
        assert len(sink.declarations) == 1
        declaration = sink.declarations[0]
        assert declaration.base_url == _UNREGISTERED_URL
        assert declaration.trust_tier == TrustTier.UNVERIFIED
        assert declaration.network_zone.value == "unknown"
        assert declaration.classification_ceiling.value == "public"
        assert declaration.declared_by == "operator:env-override"
        assert sink.run_ids == [run_id]
        assert sink.results == [True]

    @pytest.mark.asyncio
    async def test_reviewed_config_default_is_not_reported(self) -> None:
        sink = _FakeSink()
        resolver = _resolver(sink)
        default_id = EndpointRegistry.find_default_endpoint_id(
            CONFIG_DIR / "providers" / "endpoints.yaml"
        )
        endpoint = await resolver.resolve_default(run_id=uuid4())

        assert sink.declarations == []
        assert endpoint.id == default_id

    @pytest.mark.asyncio
    async def test_every_unverified_resolution_is_reported_to_sink(self) -> None:
        """resolver 不做内存去重：跨进程的「首次」判定必须由 sink 持久承担。"""
        sink = _FakeSink()
        resolver = _resolver(sink, env_overrides={"OPENAI_BASE_URL": _UNREGISTERED_URL})
        await resolver.resolve_default(run_id=uuid4())
        await resolver.resolve_default(run_id=uuid4())

        # sink 收到两次如实上报；「首次=True、后续=False」由其持久状态判定。
        assert sink.results == [True, False]
        assert len(sink.declarations) == 2

    @pytest.mark.asyncio
    async def test_sink_failure_aborts_resolution(self) -> None:
        """留痕失败异常上抛——不允许静默使用未留痕的 unverified endpoint。"""
        sink = _FakeSink()
        sink.fail_next = True
        resolver = _resolver(sink, env_overrides={"OPENAI_BASE_URL": _UNREGISTERED_URL})
        with pytest.raises(RuntimeError, match="sink unavailable"):
            await resolver.resolve_default(run_id=uuid4())


class TestDeclarationContract:
    def test_payload_carries_exactly_adr011_fields(self) -> None:
        declaration = EndpointFirstUseDeclaration.from_endpoint_profile(
            EndpointRegistry.create_floor_endpoint(_UNREGISTERED_URL),
            declared_by="operator:admin-console:u1",
        )
        assert first_use_payload(declaration) == {
            "base_url": _UNREGISTERED_URL,
            "trust_tier": "unverified",
            "network_zone": "unknown",
            "classification_ceiling": "public",
            "declared_by": "operator:admin-console:u1",
        }

    def test_idempotency_key_is_deterministic_per_base_url(self) -> None:
        def key_for(base_url: str, declared_by: str) -> str:
            return first_use_idempotency_key(
                EndpointFirstUseDeclaration.from_endpoint_profile(
                    EndpointRegistry.create_floor_endpoint(base_url),
                    declared_by=declared_by,
                )
            )

        assert key_for(_UNREGISTERED_URL, "x") == key_for(_UNREGISTERED_URL, "y")
        assert key_for(_UNREGISTERED_URL, "x") != key_for("http://127.0.0.1:9/v2", "x")

    def test_sink_protocol_is_runtime_checkable(self) -> None:
        assert isinstance(_FakeSink(), EndpointFirstUseSink)

    def test_resolve_default_endpoint_env_priority_unchanged(self) -> None:
        """resolver 复用既有解析器（ADR-011 §2 优先级由冻结测试另行锁定）。"""
        endpoint = resolve_default_endpoint(
            {"OPENAI_BASE_URL": _UNREGISTERED_URL},
            CONFIG_DIR / "providers" / "endpoints.yaml",
        )
        assert endpoint.trust_tier == TrustTier.UNVERIFIED
