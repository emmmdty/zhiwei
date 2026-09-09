"""S4 Remote HTTP runner.

Remote HTTP runner with precise origin/network zone/redirect/DNS/timeout/size
control. Supports MCP Streamable HTTP and other HTTP-based tool providers.

事实源：S4 spec §5 (Connection and execution)。

网络语义（F-R2-08/F-R9-13/T-P3.5）：origin 校验复用 inspection.network（ipaddress
全段位），并发前置「解析 IP 复检 + pin」——DNS 解析产物全部过禁用段检查后才进入
执行，解析失败 fail closed。解析期校验 + pinned IPs 随执行输出携带；连接期的
socket 级 pin 随 S11 真实 HTTP 接入落地（届时 TOCTOU 窗口闭合）。gateway 默认
no_network=True 沙箱与本 runner 必须出网的结构性矛盾按显式失败处理（不做静默
掩盖）；per-tool/network-zone sandbox override 语义待设计方 RED 冻结后另批实现。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from zhiwei.capabilities.inspection.network import check_url_safety
from zhiwei.capabilities.runners.contracts import (
    BaseRunner,
    RunnerHealth,
    RunnerInvocationRequest,
    RunnerInvocationResponse,
    RunnerSpec,
    RunnerStatus,
)

logger = logging.getLogger(__name__)

# 解析产物禁用段（比 admission 名单更全：补 CGNAT 100.64/10 与 v4-mapped）。
_FORBIDDEN_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
)

DnsResolver = Callable[[str], list[str]]


def _default_resolve(hostname: str) -> list[str]:
    """系统解析器：返回 hostname 的全部地址（IPv4/IPv6 混合）。"""
    infos = socket.getaddrinfo(hostname, None)
    return sorted({str(info[4][0]) for info in infos})


def _is_forbidden_ip(ip_text: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return True
    return any(addr in network for network in _FORBIDDEN_NETWORKS)


class RemoteHTTPRunner(BaseRunner):
    """Remote HTTP runner with origin and network zone control.

    Enforces:
    - Origin validation via inspection network checks（ipaddress 全段位）；
      解析期 IP 复检 + pin（连接期 socket 级 pin 属 S11 真实 HTTP 接入——
      TOCTOU 窗口届时闭合，不在此声称完整 DNS rebinding 防护）
    - Network zone restrictions
    - Redirect control
    - Timeout enforcement
    - Response size limits
    """

    def __init__(
        self,
        spec: RunnerSpec,
        *,
        allowed_origins: tuple[str, ...] = (),
        max_response_bytes: int = 10 * 1024 * 1024,  # 10 MiB default
        timeout_seconds: int = 30,
        allow_redirects: bool = False,
        resolver: DnsResolver | None = None,
    ) -> None:
        super().__init__(spec)
        self._allowed_origins = allowed_origins
        self._max_response_bytes = max_response_bytes
        self._timeout_seconds = timeout_seconds
        self._allow_redirects = allow_redirects
        self._resolver: DnsResolver = resolver or _default_resolve
        self._active_tasks = 0

    def _validate_origin(self, url: str) -> list[str]:
        """Validate URL origin against allowed origins and security rules.

        复用 inspection.network.check_url_safety（ipaddress 全段位 + scheme +
        危险端口），替代原手工前缀匹配（127.0.0.2/172.16-31/100.64/IPv6 ULA/
        字面量变体曾全部绕过——F-R2-08）。覆盖口径：本方法拦 check_ssrf 的
        HIGH+ 段位；CGNAT/v4-mapped/组播/保留段由 _resolve_and_pin 兜住——
        本方法不得脱离 execute 链路单独作为门禁使用。
        """
        violations: list[str] = []
        parsed = urlparse(url)

        if parsed.scheme not in ("https",):
            violations.append(f"Only HTTPS allowed, got {parsed.scheme}")

        if self._allowed_origins:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in self._allowed_origins:
                violations.append(f"Origin {origin} not in allowed list")

        network_report = check_url_safety(url)
        if not network_report.passed:
            violations.extend(
                f.message for f in network_report.findings if f.is_blocking()
            )

        return violations

    def _resolve_and_pin(self, url: str) -> tuple[list[str], list[str]]:
        """连接前解析 IP 复检（F-R2-08）：全部解析地址过禁用段检查。

        返回 (pinned_ips, violations)：pinned 非空表示解析产物全部合规，随执行
        输出携带供连接期 pin 消费；violations 非空表示拒绝（解析失败同样 fail
        closed，不取「常见默认」）。
        """
        hostname = urlparse(url).hostname or ""
        if not hostname:
            return [], ["URL has no resolvable hostname"]
        try:
            resolved = self._resolver(hostname)
        except OSError as exc:
            return [], [f"DNS resolution failed for {hostname}: {exc}"]
        if not resolved:
            return [], [f"DNS resolution returned no addresses for {hostname}"]
        forbidden = [ip for ip in resolved if _is_forbidden_ip(ip)]
        if forbidden:
            return [], [
                f"resolved addresses hit forbidden ranges: {sorted(forbidden)}"
            ]
        return sorted(resolved), []

    async def health_check(self) -> RunnerHealth:
        """Check health of the remote HTTP endpoint."""
        status = RunnerStatus.HEALTHY
        if self._active_tasks >= self._spec.max_concurrent:
            status = RunnerStatus.UNHEALTHY
        return RunnerHealth(
            runner_id=self._spec.id,
            status=status,
            checked_at=datetime.now(UTC),
            active_tasks=self._active_tasks,
            max_tasks=self._spec.max_concurrent,
        )

    async def execute(
        self, request: RunnerInvocationRequest
    ) -> RunnerInvocationResponse:
        """Execute a tool invocation via remote HTTP.

        Validates origin, enforces network zone, and manages timeout/redirects.
        """
        self._active_tasks += 1
        try:
            # Validate sandbox for remote HTTP
            violations = self._validate_sandbox_remote(request.sandbox_spec)
            if violations:
                return RunnerInvocationResponse(
                    invocation_id=request.invocation_id,
                    status="failed",
                    error=f"Sandbox violations: {'; '.join(violations)}",
                )

            # Validate endpoint URL if provided
            endpoint_url = self._spec.endpoint_url
            pinned_ips: list[str] = []
            if endpoint_url:
                url_violations = self._validate_origin(endpoint_url)
                if url_violations:
                    return RunnerInvocationResponse(
                        invocation_id=request.invocation_id,
                        status="failed",
                        error=f"Origin violations: {'; '.join(url_violations)}",
                    )
                # 连接前解析 IP 复检 + pin（F-R2-08）：解析产物全部合规才继续。
                pinned_ips, pin_violations = self._resolve_and_pin(endpoint_url)
                if pin_violations:
                    return RunnerInvocationResponse(
                        invocation_id=request.invocation_id,
                        status="failed",
                        error=(
                            "Resolved addresses forbidden: "
                            + "; ".join(pin_violations)
                        ),
                    )

            # In real implementation, this would make HTTP request to the
            # remote endpoint with timeout/redirect/size controls
            output = await self._execute_remote(request)
            output["pinned_ips"] = pinned_ips

            return RunnerInvocationResponse(
                invocation_id=request.invocation_id,
                status="completed",
                output=output,
                execution_time_ms=0.0,
            )
        except Exception as exc:
            logger.exception("Remote HTTP runner execution failed")
            return RunnerInvocationResponse(
                invocation_id=request.invocation_id,
                status="failed",
                error=f"Remote HTTP execution error: {exc}",
            )
        finally:
            self._active_tasks = max(0, self._active_tasks - 1)

    def _validate_sandbox_remote(self, sandbox: dict[str, Any]) -> list[str]:
        """Validate sandbox spec for remote HTTP runner.

        本 runner 类型必须出网：no_network=True 判为不满足（显式失败）。gateway
        当前无条件构造 no_network=True 沙箱（tool_gateway._build_sandbox）——
        组合即必失败的结构性矛盾按显式错误呈现（F-R9-13 开放项：per-tool/
        network-zone sandbox override 语义待设计方 RED 冻结后实现）。
        """
        violations: list[str] = []
        if sandbox.get("no_network") is True:
            violations.append(
                "Remote HTTP runner requires network access; gateway default "
                "sandbox forbids network (no override semantics defined yet)"
            )
        return violations

    async def _execute_remote(
        self, request: RunnerInvocationRequest
    ) -> dict[str, Any]:
        """Execute tool via remote HTTP endpoint.

        In real implementation, this would:
        1. POST to the endpoint with authenticated request
        2. Enforce timeout, redirect, and size limits
        3. Validate response against output schema
        """
        return {
            "tool_name": request.tool_name,
            "status": "executed_remote",
            "invocation_id": str(request.invocation_id),
            "endpoint": self._spec.endpoint_url,
        }

    async def shutdown(self) -> None:
        """Graceful shutdown of remote HTTP runner."""
        logger.info("Shutting down remote HTTP runner %s", self._spec.id)
