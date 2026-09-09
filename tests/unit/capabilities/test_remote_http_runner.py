"""F-R2-08/F-R9-13/T-P3.5：RemoteHTTPRunner 的网络语义。

- origin 校验复用 inspection 网络检查（ipaddress 库全段位，不再手工前缀匹配）；
- 连接前解析 IP 复检 + pin：全部解析结果落在禁用段即拒绝，解析失败 fail closed，
  解析产物（pinned IPs）随执行输出携带（真实连接期 pin 随 S11 真实 HTTP 接入）；
- gateway 默认 no_network=True 沙箱 × remote runner（要求出网）的结构性矛盾被
  测试显式钉住（处理策略=显式失败，不做静默掩盖；sandbox override 语义待设计方
  RED 冻结后另批实现）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from zhiwei.capabilities.runners.contracts import (
    RunnerInvocationRequest,
    RunnerKind,
    RunnerSpec,
)
from zhiwei.capabilities.runners.remote_http import RemoteHTTPRunner

_PUBLIC_IP = "93.184.216.34"


def _spec(endpoint_url: str) -> RunnerSpec:
    return RunnerSpec(
        id=uuid4(),
        name="remote-runner",
        kind=RunnerKind.REMOTE_HTTP,
        image_digest="sha256:" + "c" * 64,
        endpoint_url=endpoint_url,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _runner(
    endpoint_url: str,
    *,
    resolver: dict[str, list[str]] | None = None,
    allowed_origins: tuple[str, ...] = (),
) -> RemoteHTTPRunner:
    def _resolve(host: str) -> list[str]:
        if resolver is None:
            return [_PUBLIC_IP]
        if host not in resolver:
            raise OSError(f"resolution failed for {host}")
        return resolver[host]

    return RemoteHTTPRunner(
        _spec(endpoint_url),
        allowed_origins=allowed_origins,
        resolver=_resolve,
    )


def _request(endpoint_url: str, *, no_network: bool = True) -> RunnerInvocationRequest:
    return RunnerInvocationRequest(
        invocation_id=uuid4(),
        tool_name="remote-tool",
        tool_type="remote_http",
        input_args={},
        sandbox_spec={
            "image_digest": "sha256:" + "c" * 64,
            "non_root": True,
            "read_only_rootfs": True,
            "no_docker_socket": True,
            "no_network": no_network,
        },
        timeout_seconds=30,
        idempotency_key="k",
    )


class TestOriginValidation:
    """runner 复用 inspection 网络检查：全段位覆盖，不再手工前缀匹配。"""

    def test_https_enforced(self) -> None:
        runner = _runner("http://api.example.com/v1")
        assert any("HTTPS" in v or "scheme" in v for v in runner._validate_origin("http://api.example.com/v1"))

    def test_loopback_alias_bypass_blocked(self) -> None:
        runner = _runner("https://127.0.0.2/v1")
        assert runner._validate_origin("https://127.0.0.2/v1") != []

    def test_172_16_range_blocked(self) -> None:
        runner = _runner("https://172.16.1.1/v1")
        assert runner._validate_origin("https://172.16.1.1/v1") != []

    def test_ipv6_ula_blocked(self) -> None:
        runner = _runner("https://[fd00::1]/v1")
        assert runner._validate_origin("https://[fd00::1]/v1") != []

    def test_metadata_endpoint_blocked(self) -> None:
        runner = _runner("https://169.254.169.254/latest/meta-data")
        assert runner._validate_origin("https://169.254.169.254/latest/meta-data") != []

    def test_allowed_origins_enforced(self) -> None:
        runner = _runner(
            "https://api.example.com/v1",
            allowed_origins=("https://other.example.com",),
        )
        assert runner._validate_origin("https://api.example.com/v1") != []

    def test_public_endpoint_clean(self) -> None:
        runner = _runner("https://api.example.com/v1")
        assert runner._validate_origin("https://api.example.com/v1") == []


class TestDNSResolutionPin:
    """连接前解析 IP 复检：全部解析结果过禁用段检查，解析失败 fail closed。"""

    def test_resolved_metadata_ip_blocked(self) -> None:
        runner = _runner(
            "https://evil.example.com/v1",
            resolver={"evil.example.com": ["169.254.169.254"]},
        )
        pinned, violations = runner._resolve_and_pin("https://evil.example.com/v1")
        assert pinned == []
        assert violations != []

    def test_partial_bad_resolution_blocked(self) -> None:
        runner = _runner(
            "https://mixed.example.com/v1",
            resolver={"mixed.example.com": [_PUBLIC_IP, "10.0.0.1"]},
        )
        _, violations = runner._resolve_and_pin("https://mixed.example.com/v1")
        assert violations != []

    def test_resolution_failure_fails_closed(self) -> None:
        runner = _runner(
            "https://unknown.example.com/v1",
            resolver={},
        )
        pinned, violations = runner._resolve_and_pin("https://unknown.example.com/v1")
        assert pinned == []
        assert violations != []

    def test_clean_resolution_pins_all_ips(self) -> None:
        runner = _runner(
            "https://api.example.com/v1",
            resolver={"api.example.com": [_PUBLIC_IP, "93.184.216.35"]},
        )
        pinned, violations = runner._resolve_and_pin("https://api.example.com/v1")
        assert violations == []
        assert pinned == [_PUBLIC_IP, "93.184.216.35"]

    @pytest.mark.asyncio
    async def test_execute_carries_pinned_ips(self) -> None:
        runner = _runner(
            "https://api.example.com/v1",
            resolver={"api.example.com": [_PUBLIC_IP]},
        )
        response = await runner.execute(
            _request("https://api.example.com/v1", no_network=False)
        )
        assert response.status == "completed"
        assert response.output.get("pinned_ips") == [_PUBLIC_IP]

    @pytest.mark.asyncio
    async def test_execute_rejects_when_resolution_hits_forbidden_range(self) -> None:
        runner = _runner(
            "https://evil.example.com/v1",
            resolver={"evil.example.com": ["127.0.0.1"]},
        )
        response = await runner.execute(
            _request("https://evil.example.com/v1", no_network=False)
        )
        assert response.status == "failed"
        assert "forbidden" in (response.error or "") or "resolved" in (response.error or "")


class TestSandboxContradictionExplicit:
    """gateway 默认 no_network=True × remote runner 必须出网 → 显式失败（F-R9-13）。

    本测试钉住结构性矛盾的当前处理策略：诚实失败而非静默掩盖；sandbox override
    语义待设计方 RED 冻结后另批实现。
    """

    @pytest.mark.asyncio
    async def test_gateway_default_sandbox_fails_explicitly(self) -> None:
        runner = _runner("https://api.example.com/v1")
        response = await runner.execute(_request("https://api.example.com/v1"))
        assert response.status == "failed"
        assert "network" in (response.error or "")

    @pytest.mark.asyncio
    async def test_no_network_false_executes(self) -> None:
        runner = _runner("https://api.example.com/v1")
        response = await runner.execute(
            _request("https://api.example.com/v1", no_network=False)
        )
        assert response.status == "completed"
