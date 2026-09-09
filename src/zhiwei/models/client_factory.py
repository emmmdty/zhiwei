"""ADR-001：models/ 内唯一的 AsyncClient 组装工厂。

三条结构约束（ADR-001「对 S3-T5 的最小实现骨架建议」）落在生产代码里的形态：

- 唯一的 ``AsyncClient`` 构造点在本模块，transport 实参是
  ``CaptureTransport(inner=真实 HTTP transport)``——capture 必须是最内层之下的
  唯一一层，其下只能是真实 HTTP transport；
- gate 必注入：classification gate（ADR-011 §4）+ max_wire_body_bytes gate
  （F-R2-12）由工厂链式组装，调用方无法经本工厂得到无门 egress；
- 以上由 ``tests/architecture/test_model_client_factory.py`` 以 AST 扫描钉死，
  防接线回退。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx2 as httpx

from zhiwei.models.contracts import EndpointProfile, ModelProfile
from zhiwei.models.presend import (
    CaptureTransport,
    GateFn,
    WireCapture,
    classification_gate,
    wire_size_gate,
)


@dataclass(frozen=True)
class BuiltModelClient:
    """一次 egress 组装的产物：gated client 与其 capture transport。

    transport 单独暴露供调用方读取 captures（manifest 落账的输入）。
    """

    client: httpx.AsyncClient
    transport: CaptureTransport


def _chain_gates(*gates: GateFn) -> GateFn:
    """按声明顺序依次执行 gate；任一抛出即拒发（inner 未被调用）。"""

    def gate(capture: WireCapture, body: bytes) -> None:
        for single in gates:
            single(capture, body)

    return gate


def build_model_client(
    *,
    endpoint: EndpointProfile,
    profile: ModelProfile,
    context_classification: str,
    inner: httpx.AsyncBaseTransport | None = None,
    timeout: httpx.Timeout | None = None,
) -> BuiltModelClient:
    """组装带完整 pre-send 门禁的模型 egress client。

    inner 缺省为真实 HTTP transport（连接重试 0——重试由 Runtime 上移为显式
    新 Attempt + 新 ContextManifest，见 ADR-001）；测试/评测注入 MockTransport
    即可离线运行，不构成第二套生产组装路径。timeout 缺省保持库默认；调用方
    面向 LLM 推理端点时必须显式声明（reasoning + 生成的端到端延迟常态超过
    库默认 5s read timeout）。
    """
    real_inner = inner if inner is not None else httpx.AsyncHTTPTransport(retries=0)
    transport = CaptureTransport(
        inner=real_inner,
        gate=_chain_gates(
            classification_gate(endpoint, context_classification),
            wire_size_gate(profile.max_wire_body_bytes),
        ),
    )
    # 单一构造点（架构测试钉死）：缺省时显式传库默认等价值
    # （httpx2 DEFAULT_TIMEOUT_CONFIG = Timeout(5.0)，未公开导出，故字面化）。
    client = httpx.AsyncClient(
        transport=transport,
        timeout=timeout if timeout is not None else httpx.Timeout(5.0),
    )
    return BuiltModelClient(client=client, transport=transport)
